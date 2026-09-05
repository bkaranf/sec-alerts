"""Offline orchestration tests; source/report/transport fixtures are explicitly synthetic."""
import json
import sys
from types import ModuleType
from pathlib import Path
from hashlib import sha256
from filelock import FileLock
import pytest
from servicing_brief.config import validate_config
from servicing_brief.models import Document, SourceResult
from servicing_brief.pipeline import run
from servicing_brief.reader_value import inventory_html
from servicing_brief.reader_value_release import text_review_html
from servicing_brief.state import State


def _reader_review(html: str) -> dict:
    items = []
    for block in inventory_html(html):
        utility = block["kind"] in {"metadata", "reference", "heading", "headline"}
        items.append(
            {
                "id": block["id"],
                "verdict": "keep",
                "reader_need": "Readers need this specific mortgage servicing result for quarterly context.",
                "incremental_value": "It adds a concrete source-backed detail about current servicing performance.",
                "inclusion_logic": "Keep this block because it supports the mortgage servicing review.",
                "scope": "mortgage_servicing",
                "priority": "utility" if utility else "primary",
                "relevance_bridge": "",
                "reason": "It is specific, sourced, and useful for this audience.",
                "inventory_text": block["text"],
            }
        )
    return {
        "version": 1,
        "priority": "P0",
        "html_sha256": sha256(html.encode("utf-8")).hexdigest(),
        "reviewer": "synthetic test reviewer",
        "objective": "mortgage_servicing",
        "items": items,
    }


def _write_valid_reader_review(config: dict) -> None:
    """Approve both synthetic alternatives through the production gate."""
    state = State(Path(config["_storage"]) / "state.sqlite3")
    try:
        for row in state.outstanding():
            output = Path(row["output_dir"])
            html = (output / "briefing.html").read_text(encoding="utf-8")
            text = (output / "briefing.txt").read_text(encoding="utf-8")
            (output / "reader-value-review.json").write_text(json.dumps(_reader_review(html), indent=2), encoding="utf-8")
            (output / "reader-value-text-review.json").write_text(
                json.dumps(_reader_review(text_review_html(text)), indent=2), encoding="utf-8"
            )
    finally:
        state.close()


@pytest.fixture
def fixture_pipeline(tmp_path, monkeypatch):
    config = validate_config({"companies": [{"name": "Fixture issuer", "ticker": "FIX", "cik": "123", "enabled": True}]}, tmp_path)
    calls = {"reports": [], "attachments": [], "sends": []}
    report_module = ModuleType("servicing_brief.reporting")
    delivery_module = ModuleType("servicing_brief.delivery")
    def build(config, documents, **kwargs):
        calls["reports"].append((documents, kwargs))
        html = "<body data-brief-company='FIX' data-brief-cik='0000000123' data-brief-event='2026-Q2'><p>Offline mortgage servicing results improved during the quarter.</p></body>"
        text = "Company: Fixture issuer (FIX)\nEarnings period: Q2 2026\n\nOffline mortgage servicing results improved during the quarter."
        return {
            "html": html,
            "text": text,
            "subject": "Fixture",
            "evidence": [],
            "company_identity": {"ticker": "FIX", "cik": "0000000123", "event": "2026-Q2", "kind": "earnings_brief"},
            "company_reports": {"FIX": {"html": html, "text": text}},
        }
    def prepare(config, report, documents, output_dir):
        calls["attachments"].append(documents)
        path = Path(output_dir) / "message.eml"
        path.write_text("Subject: Offline fixture\n\nTest fixture", encoding="utf-8")
        return [path]
    def deliver(config, paths, db):
        calls["sends"].append(paths)
        return [{"status": "accepted"} for p in paths]
    report_module.build_report = build
    delivery_module.prepare_messages = prepare
    delivery_module.deliver_messages = deliver
    monkeypatch.setitem(sys.modules, "servicing_brief.reporting", report_module)
    monkeypatch.setitem(sys.modules, "servicing_brief.delivery", delivery_module)
    def doc(name, kind="release"):
        content = f"synthetic test fixture {name}".encode()
        path = tmp_path / f"{name}.html"
        path.write_bytes(content)
        return Document("FIX", "0000000123", name, kind, "ir", f"https://example.invalid/{name}", "2026-07-20", "2026-Q2", str(path), sha256(content).hexdigest(), metadata={"ticker": "FIX"})
    return config, calls, doc


def test_preview_then_send_and_acknowledged_duplicate_suppressed(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    release = doc("release")
    collect = lambda *a, **k: SourceResult(documents=[release], checked=["FIX"])
    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"
    _write_valid_reader_review(config)
    assert run(config, collector=collect)["status"] == "no_new_disclosures"
    assert len(calls["reports"]) == 1 and calls["sends"] == []
    assert run(config, send=True, collector=collect)["status"] == "provider_accepted"
    assert run(config, send=True, collector=collect)["status"] == "no_new_disclosures"
    assert len(calls["sends"]) == 1


def test_changed_plain_text_payload_blocks_send_even_when_html_is_unchanged(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    release = doc("release")
    collect = lambda *a, **k: SourceResult(documents=[release], checked=["FIX"])
    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"
    _write_valid_reader_review(config)

    state = State(Path(config["_storage"]) / "state.sqlite3")
    row = state.outstanding()[0]
    output = Path(row["output_dir"])
    payload = json.loads(row["payload"])
    original_html = (output / "briefing.html").read_bytes()
    payload["text"] += "\nPlain-text alternative changed after review."
    state.db.execute("UPDATE briefings SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    state.db.commit()
    state.close()
    (output / "briefing.txt").write_text(payload["text"], encoding="utf-8")

    held = run(config, send=True, collector=collect)

    assert held["status"] == "delivery_incomplete"
    assert held["delivery"] and held["delivery"][0]["status"] == "editorial_hold"
    assert held["delivery_errors"] == []
    assert calls["sends"] == []
    assert (output / "briefing.html").read_bytes() == original_html
    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert len(state.outstanding()) == 1
    state.close()

    _write_valid_reader_review(config)
    retried = run(config, send=True, collector=collect)
    assert retried["status"] == "provider_accepted"
    assert len(calls["sends"]) == 1


def test_missing_reader_review_holds_draft_and_allows_retry_after_review(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    release = doc("release")
    collect = lambda *a, **k: SourceResult(documents=[release], checked=["FIX"])
    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"

    held = run(config, send=True, collector=collect)

    assert held["status"] == "delivery_incomplete"
    assert held["delivery"] and held["delivery"][0]["status"] == "editorial_hold"
    assert held["delivery_errors"] == []
    assert calls["sends"] == []
    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert len(state.outstanding()) == 1
    state.close()

    _write_valid_reader_review(config)
    retried = run(config, send=True, collector=collect)
    assert retried["status"] == "provider_accepted"
    assert len(calls["sends"]) == 1


def test_stale_reader_review_holds_draft_and_remains_retryable(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    release = doc("release")
    collect = lambda *a, **k: SourceResult(documents=[release], checked=["FIX"])
    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"
    _write_valid_reader_review(config)

    state = State(Path(config["_storage"]) / "state.sqlite3")
    output = Path(state.outstanding()[0]["output_dir"])
    state.close()
    review_path = output / "reader-value-review.json"
    review = json.loads(review_path.read_text(encoding="utf-8"))
    review["html_sha256"] = "0" * 64
    review_path.write_text(json.dumps(review, indent=2), encoding="utf-8")

    held = run(config, send=True, collector=collect)

    assert held["status"] == "delivery_incomplete"
    assert held["delivery"] and held["delivery"][0]["status"] == "editorial_hold"
    assert held["delivery_errors"] == []
    assert calls["sends"] == []
    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert len(state.outstanding()) == 1
    state.close()

    _write_valid_reader_review(config)
    retried = run(config, send=True, collector=collect)
    assert retried["status"] == "provider_accepted"
    assert len(calls["sends"]) == 1


def test_later_deck_then_ten_q_each_updates_current_event(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    release, deck, filing = doc("release"), doc("deck", "presentation"), doc("filing", "10-Q")
    run(config, collector=lambda *a, **k: SourceResult(documents=[release]))
    run(config, collector=lambda *a, **k: SourceResult(documents=[release, deck]))
    assert {d.id for d in calls["reports"][-1][0]} == {release.id, deck.id}
    assert [d.id for d in calls["attachments"][-1]] == [deck.id]
    run(config, collector=lambda *a, **k: SourceResult(documents=[filing]))
    assert [d.id for d in calls["attachments"][-1]] == [filing.id]
    assert calls["reports"][-1][1]["baseline"] is False


def test_source_failure_does_not_prevent_other_coverage(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    response = run(config, collector=lambda *a, **k: SourceResult(documents=[doc("good")], errors=[{"issuer": "OTHER", "status": "blocked"}]))
    assert response["reports"] and response["sources_failed"]


def test_failed_collection_is_not_reported_as_no_new_disclosures(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    response = run(config, collector=lambda *a, **k: SourceResult(errors=[{"issuer": "FIX", "status": "blocked"}]))
    assert response["status"] == "collection_incomplete"
    assert not calls["sends"]


def test_overlapping_run_does_not_collect_or_send(fixture_pipeline):
    config, calls, doc = fixture_pipeline
    storage = Path(config["_storage"])
    storage.mkdir()
    with FileLock(str(storage / "run.lock")):
        response = run(config, send=True, collector=lambda *a, **k: pytest.fail("must not collect"))
    assert response["status"] == "overlap_skipped"
    assert not calls["reports"] and not calls["sends"]


def test_prepare_failure_remains_retryable(fixture_pipeline, monkeypatch):
    config, calls, doc = fixture_pipeline
    module = sys.modules["servicing_brief.delivery"]
    original_prepare = module.prepare_messages
    monkeypatch.setattr(module, "prepare_messages", lambda *a, **k: (_ for _ in ()).throw(OSError("fixture disk failure")))
    collect = lambda *a, **k: SourceResult(documents=[doc("release")])
    with pytest.raises(OSError):
        run(config, collector=collect)
    state = State(Path(config["_storage"]) / "state.sqlite3")
    assert len(state.documents()) == 1 and not state.covered_ids()
    state.close()
    monkeypatch.setattr(module, "prepare_messages", original_prepare)
    assert run(config, collector=collect)["reports"]
