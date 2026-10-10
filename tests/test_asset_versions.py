"""Immutable source bundles, legacy history, and asset-only follow-up alerts."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest

from servicing_brief.document_copies import html_bundle
from servicing_brief.models import Document, SourceResult
from servicing_brief.pipeline import run, select_updates
from servicing_brief.reporting import _latest_document_versions
from servicing_brief.state import State
from test_company_events import company_pipeline  # noqa: F401


def asset(tmp_path, name, data):
    path = tmp_path / (sha256(data).hexdigest() + ".jpg")
    path.write_bytes(data)
    return {"document": name, "path": str(path), "content_hash": sha256(data).hexdigest()}


def deck(tmp_path, assets=(), *, url="https://issuer.example/deck", stamp="2026-07-30T00:00:00Z"):
    raw = b'<html><img src="a.jpg"><img src="b.jpg"></html>'
    path = tmp_path / "deck.html"
    path.write_bytes(raw)
    return Document("TFC", "0000092230", "Q2 earnings presentation", "presentation", "ir", url,
                    "2026-07-29", "2026-Q2", str(path), sha256(raw).hexdigest(), retrieved=stamp,
                    metadata={"ticker": "TFC", "document": "deck.html", "assets": list(assets)})


def ingest(state, *docs):
    result = SourceResult(documents=list(docs), checkpoints={"TFC:ir": "2026-10-09"})
    state.ingest(result)
    return result


def test_legacy_ids_and_draft_delivery_history_survive_bundle_migration(tmp_path):
    state_path = tmp_path / "state.sqlite3"
    state = State(state_path)
    old = deck(tmp_path, [asset(tmp_path, "a.jpg", b"a"), asset(tmp_path, "b.jpg", b"b")])
    old.id = sha256(f"{old.cik}|{old.url}|{old.content_hash}".encode()).hexdigest()[:24]
    old_id = old.id  # Old formula used only the wrapper hash.
    old.metadata["reviewed"] = "unchanged editorial provenance"
    state.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (old.id, old.cik, old.period,
                     old.content_hash, json.dumps(old.to_dict()), old.retrieved))
    state.db.execute("CREATE TABLE delivery_messages(id TEXT, status TEXT, attempts INTEGER)")
    state.db.execute("INSERT INTO delivery_messages VALUES('message','accepted',1)")
    state.db.commit()
    for report_id, status in [("accepted", "accepted"), ("pending", "pending"), ("prepared", "prepared")]:
        state.save_report(report_id, {"context_documents": [old.to_dict()], "text": report_id}, [old], tmp_path / report_id, True)
        state.set_report_status(report_id, status)
    tables = ("documents", "briefings", "briefing_documents", "delivery_messages")
    before = {table: [tuple(row) for row in state.db.execute(f"SELECT * FROM {table}")] for table in tables}
    state.close()

    state = State(state_path)
    reordered = replace(old, id="", retrieved="2026-10-09T00:00:00Z",
                        metadata={"assets": list(reversed(old.metadata["assets"]))})
    result = ingest(state, reordered)
    assert result.documents[0].id == old_id
    assert result.documents[0].retrieved == old.retrieved
    for table in tables:
        assert [tuple(row) for row in state.db.execute(f"SELECT * FROM {table}")] == before[table]
    assert state.covered_ids() == {old_id}
    state.close()


def test_partial_recovery_merges_without_downgrading_or_mutating_old_versions(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    a = asset(tmp_path, "a.jpg", b"a")
    b = asset(tmp_path, "b.jpg", b"b")
    first = ingest(state, deck(tmp_path, [a])).documents[0]
    state.save_report("original", {"text": "first draft"}, [first], tmp_path, True)
    original_payload = state.db.execute("SELECT payload FROM documents WHERE id=?", (first.id,)).fetchone()[0]
    recovered = ingest(state, deck(tmp_path, [b], stamp="2026-08-01T00:00:00Z")).documents[0]
    assert recovered.id != first.id and recovered.content_hash == first.content_hash
    assert recovered.metadata["assets"] == [a, b]
    assert state.db.execute("SELECT payload FROM documents WHERE id=?", (first.id,)).fetchone()[0] == original_payload
    assert state.report_documents("original")[0].metadata["assets"] == [a]
    assert recovered.id not in state.covered_ids()
    state.save_report("recovery", {"text": "complete images"}, [recovered], tmp_path, False)

    # Rediscovery may return an empty list, either subset, or reordered assets.
    for assets in ([], [a], [b], [b, a]):
        ingest(state, deck(tmp_path, assets, stamp="2026-10-09T00:00:00Z"))
    assert len(state.documents()) == 2
    assert _latest_document_versions(state.documents())[0].id == recovered.id
    assert select_updates(state.documents(), state.covered_ids(), state.baseline_ciks()) == ([], [])
    state.close()


def test_changed_image_keeps_old_bundle_bytes_and_rejects_stale_subset_rollback(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    a, b = asset(tmp_path, "a.jpg", b"a"), asset(tmp_path, "b.jpg", b"b")
    original = ingest(state, deck(tmp_path, [a, b])).documents[0]
    original_zip = html_bundle(original, Path(original.path).read_bytes())[1]
    state.save_report("pending", {"text": "original"}, [original], tmp_path, True)
    corrected_a = asset(tmp_path, "a.jpg", b"corrected a")
    changed = ingest(state, deck(tmp_path, [corrected_a], stamp="2026-08-01T00:00:00Z")).documents[0]
    assert changed.id != original.id
    assert changed.metadata["assets"] == [corrected_a, b]
    new_zip = html_bundle(changed, Path(changed.path).read_bytes())[1]
    assert new_zip != original_zip
    assert html_bundle(state.report_documents("pending")[0], Path(original.path).read_bytes())[1] == original_zip
    # A known historical bundle must not merge old a over the corrected a.
    ingest(state, deck(tmp_path, [a, b], stamp="2026-10-09T00:00:00Z"))
    # Even a previously unseen subset of known old images cannot roll back the
    # newest bundle. It can still repair a missing old image for that old draft.
    recovered_path = tmp_path / "recovered-old-a.jpg"
    Path(a["path"]).rename(recovered_path)
    ingest(state, deck(tmp_path, [{**a, "path": str(recovered_path)}], stamp="2026-10-10T00:00:00Z"))
    assert len(state.documents()) == 2
    assert _latest_document_versions(state.documents())[0].id == changed.id
    repaired = state.report_documents("pending")[0]
    assert repaired.metadata["assets"][0]["path"] == str(recovered_path)
    assert html_bundle(repaired, Path(original.path).read_bytes())[1] == original_zip
    state.close()


def test_full_bundle_coverage_aliases_urls_but_not_changed_images_or_other_issuers(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    a = asset(tmp_path, "a.jpg", b"a")
    first, alias, changed, other = ingest(state,
        deck(tmp_path, [a]), deck(tmp_path, [a], url="https://issuer.example/moved"),
        deck(tmp_path, [asset(tmp_path, "a.jpg", b"changed")], stamp="2026-08-01T00:00:00Z"),
        replace(deck(tmp_path, [a]), cik="0000000001")).documents
    state.save_report("original", {}, [first], tmp_path, True)
    assert state.covered_ids() == {first.id, alias.id}
    assert changed.id not in state.covered_ids() and other.id not in state.covered_ids()
    state.close()


@pytest.mark.parametrize("invalid", ["missing", "corrupt", "hash", "name", "duplicate"])
def test_invalid_assets_do_not_poison_versions_or_advance_checkpoints(tmp_path, invalid):
    state = State(tmp_path / "state.sqlite3")
    a = asset(tmp_path, "a.jpg", b"a")
    good = ingest(state, deck(tmp_path, [a])).documents[0]
    state.db.execute("DELETE FROM source_checkpoints")
    state.db.commit()
    bad = asset(tmp_path, "b.jpg", b"b")
    if invalid == "missing":
        bad["path"] = str(tmp_path / "missing.jpg")
    elif invalid == "corrupt":
        Path(bad["path"]).write_bytes(b"corrupt")
    elif invalid == "hash":
        bad["content_hash"] = "invalid"
    elif invalid == "name":
        bad["document"] = "../bad.jpg"
    assets = [bad, {**bad, "content_hash": "0" * 64}] if invalid == "duplicate" else [bad]
    result = ingest(state, deck(tmp_path, assets))
    assert result.documents == []
    assert [doc.id for doc in state.documents()] == [good.id]
    assert state.checkpoints() == {}
    assert state.pending()[0]["status"] == "archive_integrity_failed"
    state.close()


def test_missing_reused_image_prevents_new_merged_version(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    a = asset(tmp_path, "a.jpg", b"a")
    original = ingest(state, deck(tmp_path, [a])).documents[0]
    Path(a["path"]).unlink()
    result = ingest(state, deck(tmp_path, [asset(tmp_path, "b.jpg", b"b")]))
    assert result.documents == []
    assert [doc.id for doc in state.documents()] == [original.id]
    assert state.pending()
    state.close()


def test_asset_only_old_quarter_followup_survives_failed_preparation_and_restart(company_pipeline, tmp_path):
    config, calls, make_doc, fail = company_pipeline
    original = deck(tmp_path)
    collect = lambda *args, **kwargs: SourceResult(documents=[original])
    assert run(config, collector=collect)["status"] == "prepared"
    q3 = make_doc("TFC", "q3-release", period="2026-Q3")
    q3.published = "2026-10-09"
    assert run(config, collector=lambda *a, **k: SourceResult(documents=[q3]))["status"] == "prepared"
    recovered = deck(tmp_path, [asset(tmp_path, "a.jpg", b"a"), asset(tmp_path, "b.jpg", b"b")],
                     stamp="2026-10-09T00:00:00Z")
    fail("TFC")
    with pytest.raises(OSError, match="fixture prepare failure"):
        run(config, collector=lambda *a, **k: SourceResult(documents=[recovered]))
    fail(None)
    assert run(config, offline=True)["status"] == "prepared"
    assert calls["attachments"][-1]["documents"][0].metadata["assets"] == recovered.metadata["assets"]
    assert calls["attachments"][-1]["documents"][0].period == "2026-Q2"
    assert run(config, collector=lambda *a, **k: SourceResult(documents=[recovered]))["status"] == "no_new_disclosures"
    assert run(config, offline=True)["status"] == "no_new_disclosures"
    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert len(state.status()["briefings"]) == 3
    assert len(state.documents()) == 3
    state.close()
    assert calls["sends"] == []


def test_document_identity_includes_images_before_state_admission(tmp_path):
    original = deck(tmp_path, [asset(tmp_path, "a.jpg", b"a")])
    changed = deck(tmp_path, [asset(tmp_path, "a.jpg", b"changed")])
    assert original.content_hash == changed.content_hash
    assert original.id != changed.id
    assert replace(changed, id=original.id).id == original.id  # Saved IDs are immutable.


def test_uppercase_hash_reacquisition_repairs_paths_without_new_version(tmp_path):
    state = State(tmp_path / "state.sqlite3")
    a = asset(tmp_path, "a.jpg", b"a")
    original = ingest(state, deck(tmp_path, [a])).documents[0]
    moved = tmp_path / "moved-a.jpg"
    Path(a["path"]).rename(moved)
    recovered = replace(original, content_hash=original.content_hash.upper(),
                        metadata={"assets": [{**a, "content_hash": a["content_hash"].upper(), "path": str(moved)}]})
    result = ingest(state, recovered)
    assert result.documents[0].id == original.id
    assert result.documents[0].metadata["assets"][0]["path"] == str(moved)
    assert result.documents[0].metadata["assets"][0]["content_hash"] == a["content_hash"]
    assert len(state.documents()) == 1
    state.close()


def test_invalid_legacy_manifest_is_isolated_and_can_recover_under_new_id(tmp_path):
    path = tmp_path / "state.sqlite3"
    state = State(path)
    old = deck(tmp_path)
    old.metadata["assets"] = None
    state.db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?)", (old.id, old.cik, old.period,
                     old.content_hash, json.dumps(old.to_dict()), old.retrieved))
    state.db.commit()
    state.save_report("legacy", {"text": "historical draft"}, [old], tmp_path, True)
    state.close()
    state = State(path)
    recovered = ingest(state, deck(tmp_path)).documents[0]
    assert recovered.id != old.id
    assert recovered.id not in state.covered_ids()
    assert state.report_documents("legacy")[0].metadata["assets"] is None
    assert ingest(state, deck(tmp_path)).documents[0].id == recovered.id
    assert len(state.documents()) == 2
    state.close()
