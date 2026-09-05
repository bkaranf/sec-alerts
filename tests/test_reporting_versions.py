"""Changed issuer URLs must not resolve financial corrections by hash order."""
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief.models import Document
from servicing_brief.reporting import build_report


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("removed", [False, True])
def test_latest_url_revision_drives_display_and_keeps_original_evidence(tmp_path, reverse, removed):
    raw = (Path(__file__).parent / "fixtures/pfsi_q2_2026_servicing_table.html").read_bytes()
    docs = []
    for name, content, retrieved in (
        ("old", raw.replace(b">22<", b">23<"), "2026-07-30T00:00:00+00:00"),
        ("new", raw.replace(b">22<", b">N/A<") if removed else raw, "2026-07-31T00:00:00+00:00"),
    ):
        path = tmp_path / f"{name}.html"
        path.write_bytes(content)
        docs.append(Document(
            "PennyMac", "0001745916", "Q2 earnings", "release", "ir",
            "https://example.test/release", "2026-07-29", "2026-Q2",
            str(path), sha256(content).hexdigest(), retrieved=retrieved,
            metadata={"ticker": "PFSI"},
        ))
    config = {"companies": [{"ticker": "PFSI", "name": "PennyMac", "cik": "1745916"}],
              "ai": {"enabled": False}, "_storage": str(tmp_path)}
    old, new = docs
    report = build_report(config, list(reversed(docs)) if reverse else docs)

    if removed:
        assert "$23m" not in report["html"]
        old_evidence_ids = {fact["id"] for fact in report["evidence"] if fact["document_id"] == old.id}
        assert all(bar["evidence_id"] not in old_evidence_ids for bar in report["chart"]["bars"])
    else:
        assert next(bar["raw_value"] for bar in report["chart"]["bars"] if bar["current"]) == "22"
    current_facts = [fact for fact in report["evidence"]
                     if fact["metric"] == "servicing_pretax_income" and fact["period"] == "2026-Q2"]
    assert {fact["document_id"] for fact in current_facts} == ({old.id} if removed else {old.id, new.id})
    assert {fact["value"] for fact in current_facts} == ({"23"} if removed else {"22", "23"})


def test_version_selection_does_not_merge_other_urls_or_events():
    from servicing_brief.reporting import _latest_document_versions

    base = {"id": "old", "cik": "0001745916", "source": "ir", "url": "https://example.test/release",
            "period": "2026-Q2", "retrieved": "2026-07-30T00:00:00Z"}
    newer = {**base, "id": "new", "retrieved": "2026-07-31T00:00:00+00:00"}
    other_url = {**base, "id": "other", "url": "https://example.test/supplement"}
    other_event = {**base, "id": "prior", "period": "2026-Q1"}
    undated = {**base, "id": "undated", "retrieved": ""}
    assert _latest_document_versions([base, newer, other_url, other_event]) == [newer, other_url, other_event]
    # Without observation evidence, retain conflicts for the existing resolver/review.
    assert _latest_document_versions([undated, newer]) == [undated, newer]
    duplicate = {**newer, "id": base["id"]}
    assert _latest_document_versions([base, duplicate]) == [duplicate]
    tied = {**newer, "id": "same-time-other-version"}
    assert _latest_document_versions([base, newer, tied]) == [newer, tied]


@pytest.mark.parametrize("source", ["ir", "sec"])
def test_event_date_rejects_corrupt_archive_and_its_metadata_fallback(tmp_path, source):
    from servicing_brief.reporting import _event_date

    path = tmp_path / "release.html"
    raw = b"<p>WESTLAKE VILLAGE, Calif. - July 29, 2026 - Company reports earnings.</p>"
    path.write_bytes(raw)
    document = Document(
        "PennyMac", "0001745916", "Q2 earnings", "release", source,
        "https://example.test/release", "2026-07-29", "2026-Q2",
        str(path), sha256(raw).hexdigest(),
        metadata={"publication_date_source": "issuer_content", "ticker": "PFSI"},
    )
    assert _event_date([document])["date"] == "2026-07-29"
    path.write_bytes(raw.replace(b"July 29, 2026", b"January 1, 2000"))
    assert _event_date([document])["date"] == ""
    config = {"companies": [{"ticker": "PFSI", "name": "PennyMac", "cik": "1745916"}],
              "ai": {"enabled": False}, "_storage": str(tmp_path)}
    report = build_report(config, [document])
    assert report["event_date_evidence"]["date"] == ""
    assert "2000" not in report["html"]
    assert report["extraction_errors"][0]["error"] == "ArchiveIntegrityError"
