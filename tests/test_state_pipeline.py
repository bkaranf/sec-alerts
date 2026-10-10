from pathlib import Path
from dataclasses import replace
from hashlib import sha256
import json
import sqlite3
from servicing_brief.models import Document, SourceResult
from servicing_brief.state import State
from servicing_brief.pipeline import event_key, select_updates
from servicing_brief.config import validate_config, ConfigError
import pytest


def document(tmp_path, *, kind="release", period="2026-Q2", url="https://issuer.example/release", content=b"original", accession=""):
    path = tmp_path / (sha256(content).hexdigest() + ".html")
    path.write_bytes(content)
    return Document(issuer="TFC", cik="0000092230", title="Earnings", kind=kind, source="ir", url=url,
                    published="2026-07-20", period=period, path=str(path), content_hash=sha256(content).hexdigest(), accession=accession)


def test_sec_and_ir_group_by_financial_period(tmp_path):
    release = document(tmp_path)
    filing = document(tmp_path, kind="10-Q", url="https://www.sec.gov/example", accession="123")
    filing.source = "sec"
    assert event_key(release) == event_key(filing)
    wrong = document(tmp_path, kind="presentation", period="2026-Q1")
    assert event_key(wrong) != event_key(release)


def test_baseline_does_not_flood_and_later_materials_are_updates(tmp_path):
    previous = document(tmp_path, period="2026-Q1", content=b"prior")
    release = document(tmp_path)
    selected, historical = select_updates([previous, release], set(), set())
    assert selected == [release] and historical == [previous]
    deck = document(tmp_path, kind="presentation", content=b"deck")
    filing = document(tmp_path, kind="10-Q", content=b"quarterly")
    amended = document(tmp_path, kind="10-Q/A", content=b"restated")
    selected, _ = select_updates([release, deck, filing, amended], {release.id}, {release.cik})
    assert selected == [deck, filing, amended]


def test_document_versions_reports_and_failures_persist(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    revised = document(tmp_path, content=b"corrected")
    state.ingest(SourceResult(documents=[original, original, revised], errors=[{"issuer": "PFSI", "status": "inaccessible"}], checkpoints={"TFC:ir": "2026-09-04"}))
    assert len(state.documents()) == 2
    state.save_report("report", {"text": "original"}, [original], tmp_path, True)
    state.set_report_status("report", "accepted")
    assert original.id in state.covered_ids() and revised.id not in state.covered_ids()
    assert state.outstanding() == []
    assert state.status()["recent_failures"][0]["issuer"] == "PFSI"
    state.close()
    reopened = State(tmp_path / "state.sqlite3")
    assert len(reopened.documents()) == 2
    assert reopened.checkpoints() == {"TFC:ir": "2026-09-04"}
    reopened.close()


def test_config_no_secret_no_duplicate_entity_no_recipient_inference(tmp_path):
    config = validate_config({"companies": [{"ticker": "TFC", "name": "Truist", "cik": "92230", "enabled": True}]}, tmp_path)
    assert config["companies"][0]["cik"] == "0000092230"
    assert "recipient" not in config["email"]
    with pytest.raises(ConfigError):
        validate_config({"email": {"password": "fake-secret"}}, tmp_path)
    with pytest.raises(ConfigError):
        validate_config({"email": {"recipient": "one@example.com,two@example.com"}}, tmp_path)
    with pytest.raises(ConfigError):
        validate_config({"companies": config["companies"] * 2}, tmp_path)


def test_withdrawn_unsent_draft_is_preserved_but_not_queued_or_covered(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    doc = document(tmp_path)
    state.ingest(SourceResult(documents=[doc]))
    state.save_report("bad-preview", {"text": "original preview"}, [doc], tmp_path, True)
    state.withdraw_unsent_report("bad-preview", "Source period metadata needs correction")
    assert state.outstanding() == []
    assert doc.id not in state.covered_ids()
    assert doc.cik not in state.baseline_ciks()
    assert state.status()["briefings"][0]["status"] == "withdrawn"
    assert state.report_documents("bad-preview")[0].id == doc.id
    state.save_report("accepted", {"text": "accepted version"}, [doc], tmp_path, True)
    state.set_report_status("accepted", "accepted")
    with pytest.raises(ValueError):
        state.withdraw_unsent_report("accepted", "Cannot alter accepted history")
    state.close()


def test_identical_document_at_new_url_is_covered_but_revision_is_new(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    first = document(tmp_path)
    alias = document(tmp_path, url="https://issuer.example/moved-release")
    revision = document(tmp_path, content=b"corrected financial release")
    state.ingest(SourceResult(documents=[first, alias, revision]))
    state.save_report("report", {"text": "original"}, [first], tmp_path, True)
    assert len(state.documents()) == 3  # both authoritative URL records remain
    assert {first.id, alias.id} <= state.covered_ids()
    assert revision.id not in state.covered_ids()
    state.close()


def test_late_historical_backfill_is_quiet_but_correction_remains_visible(tmp_path):
    latest = document(tmp_path, content=b"current")
    old = document(tmp_path, period="2026-Q1", content=b"old", url="https://issuer.example/q1")
    old.published = "2026-04-20"
    amended = document(tmp_path, kind="10-Q/A", period="2026-Q1", content=b"amended", url="https://issuer.example/amended")
    revised = document(tmp_path, period="2026-Q1", content=b"changed bytes", url=old.url)
    selected, historical = select_updates([latest, old, amended], {latest.id}, {latest.cik})
    assert selected == [amended]
    assert historical == [old]
    selected, _ = select_updates([latest, old, revised], {latest.id, old.id}, {latest.cik})
    assert selected == [revised]


def test_corrupt_archive_does_not_rollback_other_issuers(tmp_path):
    good = document(tmp_path, content=b"valid")
    corrupt = document(tmp_path, content=b"bad", url="https://issuer.example/corrupt")
    corrupt.issuer, corrupt.cik = "PFSI", "0001745916"
    Path(corrupt.path).write_bytes(b"changed after collection")
    result = SourceResult(documents=[good, corrupt], checkpoints={"TFC:ir": "2026-09-04", "PFSI:ir": "2026-09-04"})
    state = State(tmp_path / "state.sqlite3")
    state.ingest(result)
    assert [d.id for d in state.documents()] == [good.id]
    assert state.checkpoints() == {"TFC:ir": "2026-09-04"}
    assert state.pending()[0]["status"] == "archive_integrity_failed"
    assert result.errors[0]["document"] == corrupt.id
    state.close()


def test_reacquisition_repairs_archive_and_asset_paths_without_rewriting_evidence(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    old_asset = tmp_path / "original-slide.jpg"
    old_asset.write_bytes(b"original slide bytes")
    original.metadata = {"reviewed": True, "assets": [{
        "document": "slide.jpg", "path": str(old_asset),
        "content_hash": sha256(old_asset.read_bytes()).hexdigest(),
    }]}
    state.ingest(SourceResult(documents=[original]))
    state.save_report("reviewed", {"context_documents": [original.to_dict()]}, [original], tmp_path, True)
    state.set_report_status("reviewed", "accepted")
    first_seen = state.db.execute("SELECT first_seen FROM documents").fetchone()[0]
    saved_payload = state.db.execute("SELECT payload FROM briefings").fetchone()[0]

    recovered_path = tmp_path / "recovered.html"
    Path(original.path).rename(recovered_path)
    recovered_asset = tmp_path / "recovered-slide.jpg"
    old_asset.rename(recovered_asset)
    recovered = replace(original, path=str(recovered_path), retrieved="2099-01-01T00:00:00+00:00",
                        period="2099-Q1", metadata={"assets": [{**original.metadata["assets"][0], "path": str(recovered_asset)}]})
    state.ingest(SourceResult(documents=[recovered], checkpoints={"TFC:ir": "2026-10-09"}))

    restored = state.documents()[0]
    assert restored.path == str(recovered_path)
    assert restored.metadata["assets"][0]["path"] == str(recovered_asset)
    assert restored.period == original.period
    assert restored.retrieved == original.retrieved
    assert restored.metadata["reviewed"] is True
    assert restored.id in state.covered_ids()
    assert state.outstanding() == []
    assert state.db.execute("SELECT first_seen FROM documents").fetchone()[0] == first_seen
    assert state.db.execute("SELECT payload FROM briefings").fetchone()[0] == saved_payload
    snapshot = json.loads(saved_payload)["context_documents"][0]
    resolved = state.restore_snapshot_paths(snapshot)
    assert resolved["path"] == str(recovered_path)
    assert resolved["metadata"]["assets"][0]["path"] == str(recovered_asset)
    assert resolved["retrieved"] == original.retrieved
    assert snapshot["path"] == original.path
    state.close()


def test_reacquisition_rejects_corrupt_replacement_and_keeps_pending_work(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    state.ingest(SourceResult(documents=[original]))
    Path(original.path).unlink()
    corrupt = tmp_path / "corrupt.html"
    corrupt.write_bytes(b"not the original disclosure")
    state.ingest(SourceResult(documents=[replace(original, path=str(corrupt))], checkpoints={"TFC:ir": "2026-10-09"}))
    assert state.documents()[0].path == original.path
    assert state.checkpoints() == {}
    assert state.pending()[0]["status"] == "archive_integrity_failed"
    state.close()


def test_saving_an_existing_report_cannot_silently_change_coverage(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    other = document(tmp_path, content=b"new disclosure")
    state.ingest(SourceResult(documents=[original, other]))
    state.save_report("existing", {"text": "original"}, [original], tmp_path, True)
    with pytest.raises(sqlite3.IntegrityError):
        state.save_report("existing", {"text": "replacement"}, [other], tmp_path, True)
    assert state.covered_ids() == {original.id}
    assert json.loads(state.outstanding()[0]["payload"])["text"] == "original"
    state.close()


@pytest.mark.parametrize("changed", [{}, {"url": "https://other.example/disclosure"},
                                     {"cik": "0000000002"}, {"content_hash": "0" * 64}])
def test_path_repair_keeps_intact_original_and_rejects_changed_provenance(tmp_path, changed):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    replacement = tmp_path / "replacement.html"
    replacement.write_bytes(Path(original.path).read_bytes())
    recovered = replace(original, path=str(replacement), **changed)
    state.ingest(SourceResult(documents=[recovered]))
    if changed:
        Path(original.path).unlink()
    snapshot = original.to_dict()
    assert state.restore_snapshot_paths(snapshot) == snapshot
    state.close()


def test_reacquisition_does_not_replace_an_asset_with_corrupt_bytes(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    original = document(tmp_path)
    original.metadata = {"assets": [{"document": "slide.jpg", "path": str(tmp_path / "missing.jpg"),
                                    "content_hash": sha256(b"expected image").hexdigest()}]}
    # Legacy releases could admit a wrapper with unverified declared assets.
    # Preserve that history, but no new admission may repeat the mistake.
    state.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (original.id, original.cik, original.period,
                     original.content_hash, json.dumps(original.to_dict()), original.retrieved))
    state.db.commit()
    state.close()
    state = State(tmp_path / "state.sqlite3")
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"other image")
    recovered = replace(original, metadata={"assets": [{**original.metadata["assets"][0], "path": str(corrupt)}]})
    state.ingest(SourceResult(documents=[recovered]))
    assert state.documents()[0].metadata == original.metadata
    state.close()
