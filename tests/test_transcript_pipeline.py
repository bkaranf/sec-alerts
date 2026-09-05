"""Offline orchestration coverage for later earnings-call transcripts.

These synthetic fixtures exercise the same :class:`Document` contract and
archive hash checks used by collected sources. Real transcript collection and
coverage limits are recorded separately in data/transcript-coverage.json.
"""

from __future__ import annotations

from hashlib import sha256
from dataclasses import replace
import json
from pathlib import Path
import pytest

from servicing_brief.config import validate_config
from servicing_brief.extraction import extract_financial_facts, extract_transcript_passages
from servicing_brief.models import Document, SourceResult
from servicing_brief.pipeline import run
from servicing_brief.reporting import build_report


def _config(tmp_path: Path) -> dict:
    return validate_config(
        {
            "companies": [
                {"ticker": "TST", "name": "Test Bank", "cik": "1", "enabled": True},
            ],
            # The release fixture uses the primitive row matcher.  Transcript
            # values must remain source excerpts even when this switch is on.
            "extraction": {"experimental_generic_numeric": True},
            "ai": {"enabled": False},
            "email": {"recipient": "reviewer@example.com", "sender": "sender@example.com"},
        },
        tmp_path,
    )


def _document(
    tmp_path: Path,
    *,
    name: str,
    kind: str,
    period: str = "2026-Q2",
    content: str,
    published: str = "2026-07-20T12:00:00+00:00",
) -> Document:
    raw = content.encode("utf-8")
    path = tmp_path / f"{name}.html"
    path.write_bytes(raw)
    return Document(
        issuer="Test Bank",
        cik="0000000001",
        title=f"Second quarter 2026 {kind}",
        kind=kind,
        source="ir",
        url=f"https://example.test/{name}",
        published=published,
        period=period,
        path=str(path),
        content_hash=sha256(raw).hexdigest(),
        accession=f"TST-{name}",
        metadata={"ticker": "TST"},
    )


def _release(tmp_path: Path) -> Document:
    return _document(
        tmp_path,
        name="q2-release",
        kind="release",
        content="""<!doctype html>
<html><body>
<h1>Second quarter 2026 results</h1>
<table>
  <tr><th>Metric</th><th>Q2 2026</th><th>Q1 2026</th></tr>
  <tr><td>Servicing fee income</td><td>$123.40 million</td><td>$110.00 million</td></tr>
</table>
</body></html>""",
    )


def _transcript(tmp_path: Path, *, name: str = "q2-transcript") -> Document:
    return _document(
        tmp_path,
        name=name,
        kind="transcript",
        content="""<!doctype html>
<html><body>
<h1>Second quarter 2026 earnings call transcript</h1>
<h2>Prepared Remarks</h2>
<p>John Smith, Chief Executive Officer: Servicing profitability improved this quarter as prepayments declined.</p>
<p>John Smith, Chief Executive Officer: Servicing portfolio was $731.00 billion, versus $720.00 billion in the prior quarter.</p>
<h2>Question-and-Answer Session</h2>
<p>Jane Doe, Analyst: Can you discuss MSR hedge performance and servicing costs?</p>
<p>John Smith, Chief Executive Officer: We expect servicing advances to remain stable next quarter.</p>
</body></html>""",
    )


def _report_json(response: dict) -> dict:
    assert len(response["reports"]) == 1
    output = Path(response["reports"][0]["output"])
    return json.loads((output / "report.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("coverage,blocked", [
    ({"errors": [{"source": "ir", "ticker": "TST", "blocked": True, "status_code": 403}]}, True),
    ({"pending": [{"source": "ir", "ticker": "TST", "reason": "deferred after issuer IR access block"}]}, True),
    ({"errors": [{"source": "ir", "ticker": "OTHER", "blocked": True, "status_code": 403}]}, False),
    ({}, False),
])
def test_transcript_availability_distinguishes_own_blocked_access_from_not_found(tmp_path, coverage, blocked):
    report = build_report(_config(tmp_path), [_release(tmp_path)], baseline=True, coverage=coverage)
    expected = (
        "Full call transcript not located; issuer IR access was blocked."
        if blocked
        else "Full call transcript not located in available materials."
    )
    assert expected in report["text"]
    assert ("access was blocked" in report["text"]) is blocked
    assert all(report["coverage"][key] == value for key, value in coverage.items())
    assert "transcript was not published" not in report["text"]


def test_blocked_ir_limit_is_scoped_to_pfsi_and_does_not_contaminate_other_issuer(tmp_path: Path) -> None:
    config = validate_config(
        {
            "companies": [
                {"ticker": "TST", "name": "Test Bank", "cik": "1", "enabled": True},
                {"ticker": "PFSI", "name": "PennyMac Financial Services, Inc.", "cik": "1745916", "enabled": True},
            ],
            "extraction": {"experimental_generic_numeric": True},
            "ai": {"enabled": False},
            "email": {"recipient": "reviewer@example.com", "sender": "sender@example.com"},
        },
        tmp_path,
    )
    test_release = _release(tmp_path)
    pfsi_release = replace(
        test_release,
        issuer="PennyMac Financial Services, Inc.",
        cik="0001745916",
        metadata={"ticker": "PFSI"},
    )
    coverage = {
        "errors": [
            {"source": "ir", "ticker": "PFSI", "blocked": True, "status_code": 403},
            {"source": "ir", "ticker": "OTHER", "blocked": True, "status_code": 403},
        ]
    }

    pfsi = build_report(config, [pfsi_release], baseline=True, coverage=coverage)
    test_bank = build_report(config, [test_release], baseline=True, coverage=coverage)

    blocked_note = "Full call transcript not located; issuer IR access was blocked."
    generic_note = "Full call transcript not located in available materials."
    assert blocked_note in pfsi["text"]
    assert generic_note in test_bank["text"]
    assert blocked_note not in test_bank["text"]
    assert "access was blocked" not in test_bank["text"]


def test_later_transcript_creates_one_issuer_update_with_period_speaker_and_source_context(tmp_path: Path) -> None:
    """A same-period transcript is a useful supporting update for its issuer."""

    config = _config(tmp_path)
    release = _release(tmp_path)
    transcript = _transcript(tmp_path)

    first = run(
        config,
        bootstrap=True,
        collector=lambda *_args, **_kwargs: SourceResult(documents=[release], checked=["TST"]),
    )
    assert first["status"] == "prepared"

    second = run(
        config,
        collector=lambda *_args, **_kwargs: SourceResult(
            documents=[release, transcript],
            checked=["TST"],
        ),
    )
    assert second["status"] == "prepared"
    assert len(second["reports"]) == 1
    assert second["reports"][0]["issuer"] == "Test Bank"
    assert second["reports"][0]["cik"] == "0000000001"
    assert second["reports"][0]["baseline"] is False

    report = _report_json(second)
    assert report["coverage"]["new_document_ids"] == [transcript.id]
    assert all(item["document_id"] != transcript.id for item in report["evidence"])
    assert all(item.get("value") not in {"731.00", "720.00"} for item in report["evidence"])
    passages = report["transcript_passages"]
    assert passages
    assert {item["document_id"] for item in passages} == {transcript.id}
    assert {item["period"] for item in passages} == {"2026-Q2"}
    assert {item["speaker_role"] for item in passages} == {"management", "analyst"}
    assert any(
        item["context"] == "prepared_remarks"
        and item["statement_type"] == "reported_performance"
        and item["speaker"].startswith("John Smith")
        for item in passages
    )
    assert any(
        item["context"] == "analyst_question"
        and item["speaker"].startswith("Jane Doe")
        for item in passages
    )
    outlook = next(item for item in passages if "expect servicing advances" in item["text"])
    assert outlook["context"] == "management_answer"
    assert outlook["statement_type"] == "outlook"
    assert outlook["source_url"] == transcript.url
    assert outlook["location"].startswith("HTML text line")


def test_repeated_unchanged_transcript_is_suppressed_and_conversational_numbers_stay_out_of_facts(tmp_path: Path) -> None:
    """Rediscovery does not create an alert, and call figures are excerpt-only."""

    config = _config(tmp_path)
    release = _release(tmp_path)
    transcript = _transcript(tmp_path)
    collect = lambda *_args, **_kwargs: SourceResult(
        documents=[release, transcript],
        checked=["TST"],
    )

    first = run(config, bootstrap=True, collector=collect)
    assert first["status"] == "prepared"
    report_count = len(first["reports"])

    second = run(config, collector=collect)
    assert second["status"] == "no_new_disclosures"
    assert second["reports"] == []
    assert len(second["reports"]) == 0

    # The explicit experimental switch applies to primitive release parsing,
    # never to a transcript.  The passage itself keeps exact source text and
    # records why its number is not financial evidence.
    facts = extract_financial_facts(transcript, config=config)
    assert facts == []
    passages = extract_transcript_passages(transcript)
    numeric = next(item for item in passages if "$731.00" in item.text)
    assert numeric.numeric_status == "source_excerpt_only"
    assert numeric.numeric_evidence_ids == ()
    assert "$731.00" in numeric.text
    assert "$720.00" in numeric.text
    assert report_count == 1
