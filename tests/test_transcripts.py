"""Offline tests for transcript and prepared-remarks passage selection."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from servicing_brief.extraction import extract_financial_facts, extract_transcript_evidence, extract_transcript_passages
from servicing_brief.models import Document


def _document(tmp_path: Path, content: str, *, kind: str = "transcript", name: str = "call") -> Document:
    raw = content.encode("utf-8")
    path = tmp_path / f"{name}.html"
    path.write_bytes(raw)
    return Document(
        issuer="Test Bank",
        cik="0000000001",
        title="Second quarter 2026 earnings call",
        kind=kind,
        source="ir",
        url=f"https://example.test/{name}",
        published="2026-07-20T12:00:00+00:00",
        period="2026-Q2",
        path=str(path),
        content_hash=sha256(raw).hexdigest(),
        metadata={"ticker": "TST"},
    )


def _call_html() -> str:
    return """<!doctype html>
<html><body>
<h1>Second quarter 2026 earnings call transcript</h1>
<h2>Prepared Remarks</h2>
<p>[00:12:34] John Smith, Chief Executive Officer: Servicing profitability improved this quarter as prepayments declined.</p>
<p>Operator: Good morning and welcome to the earnings call.</p>
<h2>Question-and-Answer Session</h2>
<p>Jane Doe, Analyst: Can you discuss MSR hedge performance and servicing costs?</p>
<p>John Smith, Chief Executive Officer: We expect servicing advances to remain stable next quarter.</p>
<p>Safe harbor: Forward-looking statements involve risks and uncertainties.</p>
</body></html>"""


def test_transcript_passages_preserve_source_speaker_role_and_performance_outlook_context(tmp_path) -> None:
    document = _document(tmp_path, _call_html())
    passages = extract_transcript_passages(document, max_items=6)

    assert len(passages) == 3
    assert all(item.document_id == document.id for item in passages)
    assert all(item.ticker == "TST" and item.period == "2026-Q2" for item in passages)
    assert all(item.source_url == document.url and item.location.startswith("HTML text line") for item in passages)
    assert {item.speaker_role for item in passages} == {"management", "analyst"}
    assert any(item.context == "prepared_remarks" and item.statement_type == "reported_performance" and item.speaker.startswith("John Smith") for item in passages)
    assert any(item.context == "analyst_question" and item.statement_type == "other" and item.speaker.startswith("Jane Doe") for item in passages)
    outlook = next(item for item in passages if "expect servicing advances" in item.text)
    assert outlook.context == "management_answer"
    assert outlook.statement_type == "outlook"
    assert next(item for item in passages if "00:12:34" in item.text).timestamp == "00:12:34"
    assert next(item for item in passages if "00:12:34" in item.text).numeric_status == "none"
    assert "Operator:" not in " ".join(item.text for item in passages)
    assert "Safe harbor" not in " ".join(item.text for item in passages)
    assert [item.to_dict()["numeric_evidence_ids"] for item in passages] == [[], [], []]
    assert extract_transcript_evidence(document, max_items=6) == passages


def test_unbounded_transcript_inventory_keeps_late_relevant_passages_and_numeric_cap(tmp_path) -> None:
    lines = [
        f"John Smith, Chief Executive Officer: Servicing costs improved because funding costs fell in management update {index}."
        for index in range(13)
    ]
    late = "John Smith, Chief Executive Officer: Servicing advances improved because delinquency trends eased in the final management passage."
    content = (
        "<html><body><h1>Earnings call transcript</h1><h2>Prepared Remarks</h2>"
        + "".join(f"<p>{line}</p>" for line in [*lines, late])
        + "</body></html>"
    )
    document = _document(tmp_path, content, name="long-call")

    unbounded = extract_transcript_passages(document, max_items=None)
    capped = extract_transcript_passages(document, max_items=3)

    assert len(unbounded) > 12
    assert any(item.text == late for item in unbounded)
    assert len(capped) == 3
    assert extract_transcript_evidence(document, max_items=None) == unbounded


def test_prepared_remarks_can_be_speakerless_but_event_pages_and_webcasts_are_excluded(tmp_path) -> None:
    prepared = _document(
        tmp_path,
        "<html><body><h1>Prepared Remarks</h1><p>Servicing costs declined during the quarter because prepayments were lower.</p></body></html>",
        kind="prepared_remarks",
        name="remarks",
    )
    passages = extract_transcript_passages(prepared)
    assert len(passages) == 1
    assert passages[0].speaker_role == "management"
    assert passages[0].context == "prepared_remarks"
    assert passages[0].speaker == ""

    event = replace(prepared, kind="event")
    assert extract_transcript_passages(event) == []

    webcast = _document(
        tmp_path,
        "<html><body><h1>Earnings call</h1><a>Watch webcast</a><p>Servicing portfolio information is available in the presentation.</p></body></html>",
        kind="transcript",
        name="event-page",
    )
    assert extract_transcript_passages(webcast) == []


def test_transcript_numbers_remain_exact_source_excerpts_and_never_become_financial_facts(tmp_path) -> None:
    document = _document(
        tmp_path,
        """<html><body><h1>Earnings call transcript</h1><h2>Prepared Remarks</h2>
        <p>John Smith, Chief Executive Officer: Servicing portfolio was $731.00 billion, versus $720.00 billion in the prior quarter.</p>
        </body></html>""",
    )
    passages = extract_transcript_passages(document)
    assert len(passages) == 1
    passage = passages[0]
    assert passage.numeric_status == "source_excerpt_only"
    assert passage.numeric_evidence_ids == ()
    assert passage.text.endswith("prior quarter.")
    # Even the explicit generic test switch cannot promote conversational
    # figures into table evidence; callers must use the transcript record and
    # its source precision/context instead.
    assert extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}}) == []
