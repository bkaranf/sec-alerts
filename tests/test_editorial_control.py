"""Offline tests for the source-insight editorial quality gate."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from servicing_brief.editorial_control import validate_source_insights


def _record(tmp_path: Path, published_text: str, *, availability: str = "available") -> tuple[dict, dict[str, str]]:
    source = tmp_path / "official-source.pdf"
    source.write_bytes(b"official issuer source excerpt for the reviewed call")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    rendered_text = published_text if availability == "available" else ""
    record = {
        "ticker": "TST",
        "availability": availability,
        "disposition": "insight_added" if availability == "available" else "unavailable",
        "source_id": "3",
        "source_url": "https://issuer.example/q3-call.pdf",
        "source_path": str(source),
        "sha256": digest,
        "location": "Official Q3 call PDF, page 7, prepared remarks",
        "evidence_excerpt": "Management expected stable margins as mortgage competition increased.",
        "insight_type": "outlook" if availability == "available" else "",
        "published_text": rendered_text,
        "why_it_matters": "It gives investors a concrete next-quarter margin outlook to monitor." if availability == "available" else "",
        "documented_reason_for_no_increment": "" if availability == "available" else "The official archive had no transcript or speaker-attributed call remark.",
    }
    return record, {"TST": rendered_text}


def test_availability_only_rbc_call_note_is_rejected(tmp_path: Path) -> None:
    published = "Official management comments were reviewed. No full Q&A transcript was available."
    record, calls = _record(tmp_path, published)

    with pytest.raises(ValueError, match="purely procedural"):
        validate_source_insights({"version": 1, "companies": [record]}, calls)


def test_source_backed_outlook_passes_with_human_rationale(tmp_path: Path) -> None:
    published = "In prepared remarks, management expected stable margins, with mortgage competition as an offset."
    record, calls = _record(tmp_path, published)

    report = validate_source_insights({"version": 1, "companies": [record]}, calls)

    assert report["valid"] is True
    assert report["human_judgment_required"] is True
    assert report["dispositions"] == {"insight_added": 1}
    assert report["records"][0]["sha256"] == record["sha256"]


def test_missing_excerpt_and_changed_hash_fail_closed(tmp_path: Path) -> None:
    published = "Management expected stable margins, with mortgage competition as an offset."
    record, calls = _record(tmp_path, published)

    missing_excerpt = deepcopy(record)
    missing_excerpt["evidence_excerpt"] = ""
    with pytest.raises(ValueError, match="evidence_excerpt is required"):
        validate_source_insights({"version": 1, "companies": [missing_excerpt]}, calls)

    changed_hash = deepcopy(record)
    changed_hash["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="source hash does not match"):
        validate_source_insights({"version": 1, "companies": [changed_hash]}, calls)


def test_unavailable_transcript_passes_without_inventing_a_call_finding(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "", availability="unavailable")

    report = validate_source_insights({"version": 1, "companies": [record]}, calls)

    assert report["valid"] is True
    assert report["dispositions"] == {"unavailable": 1}
    assert report["records"][0]["availability"] == "unavailable"


def test_empty_no_material_increment_is_allowed_with_internal_reason(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "Management discussed broad strategy without a material servicing change.")
    record["disposition"] = "no_material_incremental_insight"
    record["published_text"] = ""
    record["why_it_matters"] = ""
    record["documented_reason_for_no_increment"] = "The remarks added no material servicing result, outlook, or operating change."
    calls["TST"] = ""

    report = validate_source_insights({"version": 1, "companies": [record]}, calls)

    assert report["valid"] is True
    assert report["dispositions"] == {"no_material_incremental_insight": 1}


def test_control_and_published_company_sets_must_match(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "Management expected stable margins with mortgage competition as an offset.")
    calls["OTHER"] = ""

    with pytest.raises(ValueError, match="ticker set must match"):
        validate_source_insights({"version": 1, "companies": [record]}, calls)


def _document_review(*, decisions: list[str] | None = None, scope: str = "Canadian Banking mortgage operations") -> dict:
    return {
        "scope": scope,
        "passages": [
            {
                "location": "Q3 call PDF, page 7",
                "evidence_excerpt": "Management expected stable margins as mortgage competition increased.",
                "decision": decision,
                "reader_value_reason": "This passage gives investors a concrete operating signal to monitor.",
            }
            for decision in (decisions or ["include"])
        ],
    }


def test_version_2_available_document_review_validates_include_and_exclude_adjudication(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "Management expected stable margins, with mortgage competition as an offset.")
    record["document_review"] = _document_review(decisions=["include", "exclude"])

    report = validate_source_insights({"version": 2, "companies": [record]}, calls)

    assert report["valid"] is True
    assert report["records"][0]["sha256"] == record["sha256"]


def test_version_2_no_material_disposition_requires_all_passages_excluded(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "", availability="available")
    record.update(
        disposition="no_material_incremental_insight",
        published_text="",
        why_it_matters="",
        documented_reason_for_no_increment="The reviewed passages added no material servicing result or operating change.",
        document_review=_document_review(decisions=["exclude", "exclude"]),
    )
    calls["TST"] = ""

    report = validate_source_insights({"version": 2, "companies": [record]}, calls)

    assert report["valid"] is True
    assert report["dispositions"] == {"no_material_incremental_insight": 1}


def test_version_2_available_document_requires_nonempty_review(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "Management expected stable margins, with mortgage competition as an offset.")
    record["document_review"] = {}

    with pytest.raises(ValueError, match="document_review.scope is required"):
        validate_source_insights({"version": 2, "companies": [record]}, calls)

    record["document_review"] = {"scope": "mortgage operations", "passages": []}
    with pytest.raises(ValueError, match="passages must be a non-empty list"):
        validate_source_insights({"version": 2, "companies": [record]}, calls)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("location", "", "location is required"),
        ("evidence_excerpt", "", "evidence_excerpt is required"),
        ("reader_value_reason", "", "reader_value_reason is required"),
        ("decision", "review", "decision must be include or exclude"),
    ],
)
def test_version_2_document_passage_fields_are_resolved(tmp_path: Path, field: str, value: str, message: str) -> None:
    record, calls = _record(tmp_path, "Management expected stable margins, with mortgage competition as an offset.")
    review = _document_review()
    review["passages"][0][field] = value
    record["document_review"] = review

    with pytest.raises(ValueError, match=message):
        validate_source_insights({"version": 2, "companies": [record]}, calls)


@pytest.mark.parametrize(
    ("disposition", "decisions", "message"),
    [
        (
            "no_material_incremental_insight",
            ["include"],
            "requires every document passage to be excluded",
        ),
        ("insight_added", ["exclude"], "requires at least one included document passage"),
    ],
)
def test_version_2_document_adjudication_must_match_disposition(
    tmp_path: Path, disposition: str, decisions: list[str], message: str
) -> None:
    published = "Management expected stable margins, with mortgage competition as an offset."
    record, calls = _record(tmp_path, published)
    record["disposition"] = disposition
    if disposition == "no_material_incremental_insight":
        record["published_text"] = ""
        calls["TST"] = ""
        record["why_it_matters"] = ""
        record["documented_reason_for_no_increment"] = "The reviewed passages added no material servicing result or operating change."
    record["document_review"] = _document_review(decisions=decisions)

    with pytest.raises(ValueError, match=message):
        validate_source_insights({"version": 2, "companies": [record]}, calls)


def test_version_2_unavailable_material_need_not_have_document_passages(tmp_path: Path) -> None:
    record, calls = _record(tmp_path, "", availability="unavailable")

    report = validate_source_insights({"version": 2, "companies": [record]}, calls)

    assert report["valid"] is True


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize(
    ("availability", "disposition"),
    [("available", "no_material_incremental_insight"), ("unavailable", "unavailable")],
)
def test_non_insight_dispositions_require_empty_published_text(
    tmp_path: Path, version: int, availability: str, disposition: str
) -> None:
    record, calls = _record(tmp_path, "", availability=availability)
    record["disposition"] = disposition
    record["published_text"] = "A rendered finding must not accompany this disposition."
    calls["TST"] = record["published_text"]
    if disposition == "no_material_incremental_insight":
        record["documented_reason_for_no_increment"] = (
            "The reviewed passages added no material servicing result or operating change."
        )
        if version == 2:
            record["document_review"] = _document_review(decisions=["exclude"])

    with pytest.raises(ValueError, match="published_text must be empty"):
        validate_source_insights({"version": version, "companies": [record]}, calls)
