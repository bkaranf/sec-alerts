"""Offline orchestration tests for one-company-per-event drafts.

The reporting and delivery doubles deliberately put issuer identity in every
fact and attachment.  That makes a cross-company mix-up fail at the boundary,
even though the source and transport layers are mocked.
"""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import sys
from types import ModuleType

import pytest

from servicing_brief.config import validate_config
from servicing_brief.models import Document, SourceResult
from servicing_brief.pipeline import run


@pytest.fixture
def company_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = validate_config(
        {
            "companies": [
                {"name": "Truist Financial Corporation", "ticker": "TFC", "cik": "92230", "enabled": True},
                {"name": "PennyMac Financial Services, Inc.", "ticker": "PFSI", "cik": "1745916", "enabled": True},
            ]
        },
        tmp_path,
    )
    calls: dict[str, list] = {"reports": [], "attachments": [], "sends": []}
    failing_ticker: str | None = None

    report_module = ModuleType("servicing_brief.reporting")
    delivery_module = ModuleType("servicing_brief.delivery")

    def ticker_for(documents: list[Document]) -> str:
        ciks = {doc.cik for doc in documents}
        assert len(ciks) == 1, f"a company draft mixed CIKs: {ciks}"
        return "TFC" if next(iter(ciks)) == "0000092230" else "PFSI"

    def build(config, documents, **kwargs):
        docs = list(documents)
        ticker = ticker_for(docs)
        comparison_docs = list(kwargs.get("previous_documents", []) or [])
        if comparison_docs:
            assert {doc.cik for doc in comparison_docs} == {docs[0].cik}, f"{ticker} comparison pool mixed peer CIKs"
        # The fixture report is intentionally issuer-specific.  Tests can then
        # inspect facts and attachments without depending on the production
        # report's HTML implementation.
        cik = docs[0].cik
        period = docs[0].period
        text_period = f"Q{period[-1]} {period[:4]}" if period.endswith(tuple(f"Q{i}" for i in range(1, 5))) else period.replace("-", " ")
        html = f"<body data-brief-company='{ticker}' data-brief-cik='{cik}' data-brief-event='{period}'><p>{ticker} draft</p></body>"
        text = f"Company: {ticker} ({ticker})\nEarnings period: {text_period}\n\n{ticker} draft"
        report = {
            "html": html,
            "text": text,
            "subject": f"{ticker} earnings",
            "evidence": [{"ticker": ticker, "document_id": doc.id} for doc in docs],
            "company_identity": {"ticker": ticker, "cik": cik, "event": period, "kind": "earnings_brief"},
            "company_reports": {ticker: {"html": html, "text": text}},
        }
        calls["reports"].append({"ticker": ticker, "documents": docs, "kwargs": kwargs, "report": report})
        return report

    def prepare(config, report, documents, output_dir):
        docs = list(documents)
        ticker = ticker_for(docs)
        if failing_ticker == ticker:
            raise OSError(f"fixture prepare failure for {ticker}")
        calls["attachments"].append({"ticker": ticker, "documents": docs, "report": report})
        path = Path(output_dir) / f"{ticker}-message.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"Subject: {ticker} earnings\n\n{ticker}", encoding="utf-8")
        return [path]

    def deliver(config, paths, db):
        calls["sends"].append(list(paths))
        return [{"status": "accepted"} for _path in paths]

    report_module.build_report = build
    delivery_module.prepare_messages = prepare
    delivery_module.deliver_messages = deliver
    monkeypatch.setitem(sys.modules, "servicing_brief.reporting", report_module)
    monkeypatch.setitem(sys.modules, "servicing_brief.delivery", delivery_module)

    def doc(ticker: str, name: str, *, kind: str = "release", period: str = "2026-Q2") -> Document:
        cik = "0000092230" if ticker == "TFC" else "0001745916"
        content = f"offline {ticker} {name} {kind} {period}".encode()
        path = tmp_path / f"{ticker}-{name}.html"
        path.write_bytes(content)
        return Document(
            issuer=ticker,
            cik=cik,
            title=f"{ticker} {name}",
            kind=kind,
            source="ir" if ticker == "TFC" else "sec",
            url=f"https://example.invalid/{ticker}/{name}",
            published="2026-07-29",
            period=period,
            path=str(path),
            content_hash=sha256(content).hexdigest(),
            accession=f"{ticker}-{name}",
            metadata={"ticker": ticker},
        )

    def set_failing_ticker(value: str | None) -> None:
        nonlocal failing_ticker
        failing_ticker = value

    return config, calls, doc, set_failing_ticker


def _calls_for(calls: dict[str, list], start: int) -> list[dict]:
    return calls["reports"][start:]


def test_same_check_creates_isolated_draft_and_attachment_per_issuer(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    tfc = doc("TFC", "q2-release")
    pfsi = doc("PFSI", "q2-release")

    response = run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"]),
    )

    assert response["status"] == "prepared"
    assert {call["ticker"] for call in calls["reports"]} == {"TFC", "PFSI"}
    assert len(calls["reports"]) == 2
    assert len(calls["attachments"]) == 2
    for call in calls["reports"]:
        assert {doc.cik for doc in call["documents"]} == {"0000092230" if call["ticker"] == "TFC" else "0001745916"}
        assert all(fact["ticker"] == call["ticker"] for fact in call["report"]["evidence"])
    for attachment in calls["attachments"]:
        assert {item.cik for item in attachment["documents"]} == {"0000092230" if attachment["ticker"] == "TFC" else "0001745916"}


def test_later_issuer_gets_its_own_baseline(company_pipeline):
    config, calls, _doc, _set_failure = company_pipeline
    tfc = _doc("TFC", "q2-release")
    pfsi = _doc("PFSI", "q2-release")

    first = run(config, collector=lambda *args, **kwargs: SourceResult(documents=[tfc], checked=["TFC"]))
    assert first["status"] == "prepared"
    assert calls["reports"][-1]["ticker"] == "TFC"
    assert calls["reports"][-1]["kwargs"].get("baseline") is True

    start = len(calls["reports"])
    second = run(config, collector=lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"]))
    assert second["status"] == "prepared"
    later = _calls_for(calls, start)
    assert len(later) == 1
    assert later[0]["ticker"] == "PFSI"
    assert later[0]["kwargs"].get("baseline") is True
    assert calls["attachments"][-1]["ticker"] == "PFSI"


def test_repeated_unchanged_check_prepares_no_company_draft(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    tfc = doc("TFC", "q2-release")
    pfsi = doc("PFSI", "q2-release")
    collect = lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"])

    assert run(config, bootstrap=True, collector=collect)["status"] == "prepared"
    report_count = len(calls["reports"])
    attachment_count = len(calls["attachments"])
    response = run(config, collector=collect)

    assert response["status"] == "no_new_disclosures"
    assert len(calls["reports"]) == report_count
    assert len(calls["attachments"]) == attachment_count


def test_late_ten_q_followup_is_limited_to_affected_issuer(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    tfc = doc("TFC", "q2-release")
    pfsi = doc("PFSI", "q2-release")
    late_10q = doc("PFSI", "q2-10q", kind="10-Q")
    initial = lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"])

    run(config, bootstrap=True, collector=initial)
    start_reports = len(calls["reports"])
    start_attachments = len(calls["attachments"])
    response = run(
        config,
        collector=lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi, late_10q], checked=["TFC", "PFSI"]),
    )

    assert response["status"] == "prepared"
    followups = _calls_for(calls, start_reports)
    assert len(followups) == 1
    assert followups[0]["ticker"] == "PFSI"
    assert {item.cik for item in followups[0]["documents"]} == {"0001745916"}
    assert all(item.cik != "0000092230" for item in followups[0]["documents"])
    new_attachments = calls["attachments"][start_attachments:]
    assert len(new_attachments) == 1
    assert new_attachments[0]["ticker"] == "PFSI"
    assert late_10q.id in {item.id for item in new_attachments[0]["documents"]}


def test_prepare_failure_for_one_issuer_keeps_other_draft_useful(company_pipeline):
    config, calls, doc, set_failure = company_pipeline
    tfc = doc("TFC", "q2-release")
    pfsi = doc("PFSI", "q2-release")
    set_failure("TFC")

    # Preparation is per issuer: a disk/packaging failure for TFC must not
    # discard PFSI's already-rendered draft or make the run appear successful
    # for both companies.
    response = run(
        config,
        bootstrap=True,
        collector=lambda *args, **kwargs: SourceResult(documents=[tfc, pfsi], checked=["TFC", "PFSI"]),
    )

    assert response["status"] in {"prepared", "partial_failure", "delivery_incomplete"}
    assert any(call["ticker"] == "PFSI" for call in calls["reports"])
    assert any(attachment["ticker"] == "PFSI" for attachment in calls["attachments"])
    assert all(attachment["ticker"] != "TFC" for attachment in calls["attachments"])
    rendered_tickers = {call["ticker"] for call in calls["reports"]}
    assert rendered_tickers == {"TFC", "PFSI"}


def test_prior_period_correction_and_new_release_remain_distinct_events(company_pipeline):
    config, calls, doc, _set_failure = company_pipeline
    previous = doc("PFSI", "q1-release", period="2026-Q1")
    run(config, collector=lambda *a, **kw: SourceResult(documents=[previous]))
    corrected = doc("PFSI", "q1-corrected", kind="10-Q/A", period="2026-Q1")
    latest = doc("PFSI", "q2-release", period="2026-Q2")
    start = len(calls["reports"])
    response = run(config, collector=lambda *a, **kw: SourceResult(documents=[previous, corrected, latest]))

    assert len(response["reports"]) == 2
    new_calls = calls["reports"][start:]
    assert {tuple(sorted({d.period for d in call["documents"]})) for call in new_calls} == {("2026-Q1",), ("2026-Q2",)}
    assert all(call["kwargs"]["baseline"] is False for call in new_calls)
