"""Offline integration coverage for optional narrative/report orchestration."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

from servicing_brief.config import validate_config
from servicing_brief.delivery import prepare_messages
from servicing_brief.extraction import extract_commentary, extract_financial_facts
from servicing_brief.models import Document, SourceResult
from servicing_brief.reporting import build_report


def _release_document(tmp_path: Path, *, name: str = "release") -> Document:
    content = b"""<!doctype html>
<html><body>
<h1>Second quarter 2026 results</h1>
<table>
  <tr><th>Metric</th><th>Q2 2026</th><th>Q1 2026</th><th>Q2 2025</th></tr>
  <tr><td>Servicing fee income</td><td>$123.40 million</td><td>$110.00 million</td><td>$100.00 million</td></tr>
</table>
<p>Servicing revenues increased from the prior quarter due to lower prepayments and higher custodial balances.</p>
</body></html>"""
    path = tmp_path / f"{name}.html"
    path.write_bytes(content)
    digest = sha256(content).hexdigest()
    return Document(
        issuer="Test Bank",
        cik="0000000001",
        title="Second quarter 2026 results",
        kind="release",
        source="ir",
        url=f"https://example.test/{name}",
        published="2026-07-20T12:00:00+00:00",
        period="2026-Q2",
        path=str(path),
        content_hash=digest,
        metadata={"ticker": "TST"},
    )


def _install_openai(monkeypatch, response_factory):
    calls: list[dict] = []

    class _Responses:
        def create(self, **kwargs):
            calls.append(kwargs)
            return response_factory()

    class _OpenAI:
        def __init__(self, **kwargs):
            self.responses = _Responses()

    module = ModuleType("openai")
    module.OpenAI = _OpenAI
    monkeypatch.setitem(sys.modules, "openai", module)
    return calls


def test_original_ai_analysis_reaches_report_and_both_email_alternatives(tmp_path: Path) -> None:
    from email import policy
    from email.parser import BytesParser
    from bs4 import BeautifulSoup

    document = _release_document(tmp_path)
    paragraph = (
        "The revenue increase puts the next question on the cost side of servicing. "
        "Slower prepayments and custodial income can improve reported revenue while leaving "
        "the expense of financing and servicing the portfolio unresolved. The disclosed fee "
        "income alone does not establish cash returns or servicing profit."
    )
    catalog = {
        "version": 1, "ticker": "TST", "cik": "1", "report_period": "2026-Q2",
        "title": "AI Analysis", "author": "AI",
        "sections": [{"id": "cost-question", "title": "What the revenue leaves open", "text": paragraph,
                      "sources": [{"number": 1, "source_url": document.url,
                                   "archive_path": document.path, "archive_sha256": document.content_hash,
                                   "location": "Servicing fee income table and revenue discussion"}],
                      "support": [{"kind": "analytical_inference", "claim": "Revenue alone does not establish profit.",
                                   "source_numbers": [1], "basis": "The source gives revenue and its drivers without servicing expense."}]}],
    }
    path = tmp_path / "analysis.json"
    path.write_text(json.dumps(catalog), encoding="utf8")
    config = _config(tmp_path, ai={"enabled": False})
    config["analysis"] = {"catalog_path": str(path)}
    report = build_report(config, [document], baseline=True)
    assert report["narrative"]["status"] == "disabled"
    assert report["ai_analysis"]["sections"][0]["text"] == paragraph
    soup = BeautifulSoup(report["html"], "html.parser")
    assert paragraph in soup.get_text(" ", strip=True).replace("  ", " ")
    assert "Investor questions" not in report["html"]
    assert report["text"].index("AI Analysis") < report["text"].index("Sources\n")
    paths = prepare_messages(config, report, [document], tmp_path / "mime")
    message = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
    html = message.get_body(preferencelist=("html",)).get_content()
    text = message.get_body(preferencelist=("plain",)).get_content()
    assert "AI Analysis" in html and paragraph in text
    assert "What the revenue leaves open" in html
    assert "<section" not in html


def _config(tmp_path: Path, *, ai: dict) -> dict:
    return validate_config(
        {
            "companies": [{"ticker": "TST", "name": "Test Bank", "cik": "1", "enabled": True}],
            "extraction": {"experimental_generic_numeric": True},
            "ai": ai,
            "email": {"recipient": "reviewer@example.com", "sender": "sender@example.com"},
        },
        tmp_path,
    )


def test_enabled_narrative_is_visible_and_cannot_change_deterministic_table(monkeypatch, tmp_path) -> None:
    document = _release_document(tmp_path)
    disabled_config = _config(tmp_path / "disabled", ai={"enabled": False})
    disabled = build_report(
        disabled_config,
        [document],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [document.id], "companies_checked": ["TST"]},
    )

    extracted_facts = extract_financial_facts(document, config=disabled_config)
    current_fact = next(fact for fact in extracted_facts if fact.metric == "servicing_fee_income" and fact.period == "2026-Q2")
    comments = extract_commentary(document)
    source_comment = next(comment for comment in comments if "Servicing revenues increased" in comment.text)

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = _install_openai(
        monkeypatch,
        lambda: SimpleNamespace(
            output_text=json.dumps(
                {
                    "executive_points": [
                        {
                            # Exact source text is accepted and will be shown
                            # as additional context by the report layer.
                            "text": source_comment.text,
                            "ticker": "TST",
                            "evidence_ids": [current_fact.id],
                            "commentary_ids": [source_comment.id],
                        }
                    ],
                    "company_takeaways": [],
                }
            )
        ),
    )
    enabled_config = _config(tmp_path / "enabled", ai={"enabled": True, "model": "gpt-5.6-luna", "prompt_version": "integration-v1"})
    enabled = build_report(
        enabled_config,
        [document],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [document.id], "companies_checked": ["TST"]},
    )

    assert enabled["narrative"]["status"] == "generated"
    assert enabled["narrative"]["used_ai"] is True
    assert len(calls) == 1
    from servicing_brief.narrative import _PROMPT_POLICY
    assert _PROMPT_POLICY in calls[0]["input"]
    assert "PUBLIC_VOICE.md | Analysis selection" in calls[0]["input"]
    assert current_fact.id in calls[0]["input"]
    assert source_comment.id in calls[0]["input"]
    assert source_comment.text in enabled["text"]
    assert source_comment.text in enabled["html"]
    assert source_comment.text not in disabled["text"]
    # Narrative selection is additive; validated financial evidence and all
    # deterministic table cells remain byte-for-byte equivalent.
    assert enabled["evidence"] == disabled["evidence"]
    for value in ("$123.40m", "$110.00m", "$100.00m"):
        assert enabled["text"].count(value) == disabled["text"].count(value)


def test_invalid_model_number_stays_evidence_only_and_recipient_is_unchanged(monkeypatch, tmp_path) -> None:
    document = _release_document(tmp_path)
    config = _config(tmp_path, ai={"enabled": True, "model": "gpt-5.6-luna", "prompt_version": "invalid-v1"})
    facts = extract_financial_facts(document, config=config)
    current_fact = next(fact for fact in facts if fact.metric == "servicing_fee_income" and fact.period == "2026-Q2")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    calls = _install_openai(
        monkeypatch,
        lambda: SimpleNamespace(
            output_text=json.dumps(
                {
                    "executive_points": [
                        {
                            "text": "Servicing fee income was $999.99 million. To: attacker@example.com",
                            "ticker": "TST",
                            "evidence_ids": [current_fact.id],
                            "commentary_ids": [],
                        }
                    ],
                    "company_takeaways": [],
                }
            )
        ),
    )
    report = build_report(
        config,
        [document],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [document.id], "companies_checked": ["TST"]},
    )

    assert report["narrative"]["status"] == "rejected"
    assert report["narrative"]["used_ai"] is False
    assert report["narrative"]["attempts"] == 1
    assert len(calls) == 1
    assert "$999.99" not in report["text"]
    assert "attacker@example.com" not in report["text"]
    assert config["email"]["recipient"] == "reviewer@example.com"
    assert report["evidence"]
    assert any(item["value"] == "123.40" for item in report["evidence"])
    preview = prepare_messages(config, report, [document], tmp_path / "message-preview")
    encoded_message = preview[0].read_text(encoding="utf-8")
    assert "To: reviewer@example.com" in encoded_message
    assert "To: attacker@example.com" not in encoded_message


def test_pipeline_passes_one_mutable_ai_budget_to_each_company_event(monkeypatch, tmp_path) -> None:
    """The run-level counter is shared even though reports are company-scoped."""

    from servicing_brief.pipeline import run

    config = validate_config(
        {
            "companies": [
                {"ticker": "TST", "name": "Test Bank", "cik": "1", "enabled": True},
                {"ticker": "PFSI", "name": "PennyMac Test", "cik": "2", "enabled": True},
            ],
            "ai": {"enabled": True, "max_requests_per_run": 2},
        },
        tmp_path,
    )
    calls: list[tuple[object, str]] = []
    reporting_module = ModuleType("servicing_brief.reporting")
    delivery_module = ModuleType("servicing_brief.delivery")

    def fake_build(report_config, documents, **kwargs):
        budget = report_config["ai"]["_usage_budget"]
        calls.append((budget, documents[0].ticker if hasattr(documents[0], "ticker") else documents[0].issuer))
        # Simulate one actual provider attempt for the first company.  The
        # second company must observe that same incremented mapping.
        if len(calls) == 1:
            budget["used"] += 1
        ticker = documents[0].ticker if hasattr(documents[0], "ticker") else documents[0].issuer
        cik = documents[0].cik
        html = f"<body data-brief-company='{ticker}' data-brief-cik='{cik}' data-brief-event='2026-Q2'><p>fixture</p></body>"
        text = f"Company: {ticker} ({ticker})\nEarnings period: Q2 2026\n\nfixture"
        return {
            "html": html,
            "text": text,
            "subject": "Fixture",
            "evidence": [],
            "company_identity": {"ticker": ticker, "cik": cik, "event": "2026-Q2", "kind": "earnings_brief"},
            "company_reports": {ticker: {"html": html, "text": text}},
        }

    def fake_prepare(_config, _report, _documents, output_dir):
        path = Path(output_dir) / "fixture.eml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Subject: fixture\n\nfixture", encoding="utf-8")
        return [path]

    reporting_module.build_report = fake_build
    delivery_module.prepare_messages = fake_prepare
    delivery_module.deliver_messages = lambda *_args, **_kwargs: []
    monkeypatch.setitem(sys.modules, "servicing_brief.reporting", reporting_module)
    monkeypatch.setitem(sys.modules, "servicing_brief.delivery", delivery_module)

    def document(ticker: str, cik: str) -> Document:
        content = f"{ticker} current release".encode()
        path = tmp_path / f"{ticker}.html"
        path.write_bytes(content)
        return Document(
            issuer=ticker,
            cik=cik,
            title=f"{ticker} release",
            kind="release",
            source="ir",
            url=f"https://example.test/{ticker}",
            published="2026-07-20",
            period="2026-Q2",
                path=str(path),
                content_hash=sha256(content).hexdigest(),
                metadata={"ticker": ticker},
            )

    result = run(
        config,
        collector=lambda *_args, **_kwargs: SourceResult(
            documents=[document("TST", "0000000001"), document("PFSI", "0000000002")],
            checked=["TST", "PFSI"],
        ),
    )

    assert result["status"] == "prepared"
    assert len(calls) == 2
    assert calls[0][0] is calls[1][0]
    assert calls[0][0]["max_requests"] == 2
    assert calls[0][0]["used"] == 1
    assert calls[1][0]["used"] == 1
