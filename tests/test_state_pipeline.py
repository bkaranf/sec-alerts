from pathlib import Path
from hashlib import sha256
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
