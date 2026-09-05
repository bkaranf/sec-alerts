from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from servicing_brief.evidence import (
    Evidence,
    derive_absolute_change,
    derive_rate_change,
    derive_standalone_quarter,
    parse_decimal,
)
from servicing_brief.extraction import extract_commentary, extract_financial_facts
from servicing_brief.models import Document
from servicing_brief.narrative import _validate_claims, generate_narrative
from servicing_brief.reporting import build_report


GENERIC_CONFIG = {"extraction": {"experimental_generic_numeric": True}}


def _fact(tmp_path, text: str, *, period: str = "2026-Q2", ticker: str = "TST", kind: str = "presentation", name: str = "deck") -> Document:
    path = tmp_path / f"{name}.html"
    path.write_text(text, encoding="utf-8")
    return Document(
        issuer="Test Bank", cik="1", title=name, kind=kind, source="ir", url=f"https://example.test/{name}",
        published="2026-08-01", period=period, path=str(path), content_hash=name, metadata={"ticker": ticker},
    )


def _evidence(*, value: str, metric: str = "servicing_fee_income", period: str = "2026-Q2", unit: str = "USD", scope: str = "servicing", definition: str = "servicing fee income", measure_type: str = "flow", ticker: str = "TST") -> Evidence:
    return Evidence(
        id=f"{ticker}-{metric}-{period}-{value}", document_id="doc", issuer="Test", ticker=ticker, metric=metric,
        value=value, raw_value=value, unit=unit, currency="USD" if unit.startswith("USD") else "", period=period,
        scope=scope, definition=definition, location="PDF page 2, line 4", excerpt=f"{metric}: {value}", source_url="https://example.test/source", measure_type=measure_type,
    )


def test_parse_decimal_preserves_scale_and_sign() -> None:
    parsed = parse_decimal("($1,200.40)")
    assert parsed == Decimal("-1200.40")
    assert format(parsed, "f") == "-1200.40"
    with pytest.raises(TypeError):
        parse_decimal(1.2)
    assert parse_decimal("N/A", allow_missing=True) is None


def test_extracts_servicing_values_units_signs_and_location(tmp_path) -> None:
    doc = _fact(tmp_path, """
    <h1>Second quarter 2026</h1>
    <table><tr><th>Metric</th><th>Q2 2026</th><th>Q1 2026</th></tr>
    <tr><td>Servicing fee income</td><td>$123.40</td><td>$110.00</td></tr>
    <tr><td>Servicing operating expense</td><td>($45.0)</td><td>($44.0)</td></tr>
    <tr><td>Loans serviced</td><td>1.2 million</td><td>1.1 million</td></tr>
    <tr><td>MSR carrying value</td><td>$2,000.0 million</td><td>$1,900.0 million</td></tr></table>
    <p>Servicing results improved due to lower operating expense.</p>
    """)
    facts = extract_financial_facts(doc, config=GENERIC_CONFIG)
    current = {(fact.metric, fact.period): fact for fact in facts}
    assert current[("servicing_fee_income", "2026-Q2")].value == "123.40"
    assert current[("servicing_operating_expense", "2026-Q2")].value == "-45.0"
    assert current[("servicing_operating_expense", "2026-Q2")].sign == "parentheses"
    assert current[("msr_carrying_value", "2026-Q2")].unit == "USD_millions"
    assert current[("loans_serviced", "2026-Q2")].unit == "loans_millions"
    assert current[("servicing_fee_income", "2026-Q2")].location.startswith("HTML table")
    assert [item.text for item in extract_commentary(doc)] == ["Servicing results improved due to lower operating expense."]


def test_excludes_production_and_bankwide_lines(tmp_path) -> None:
    doc = _fact(tmp_path, """
    <p>Mortgage production revenue $500.0 million.</p>
    <p>Total company revenue $900.0 million.</p>
    <p>Consolidated net income $100.0 million.</p>
    <p>Servicing fee income $23.40 million.</p>
    """)
    facts = extract_financial_facts(doc, config=GENERIC_CONFIG)
    assert [item.metric for item in facts] == ["servicing_fee_income"]
    assert all(item.scope not in {"bankwide", "mortgage_origination"} for item in facts)


def test_real_source_layout_without_verified_parser_fails_closed(tmp_path) -> None:
    doc = _fact(tmp_path, "<p>Servicing fee income $23.40 million.</p>", name="official")
    doc.url = "https://www.sec.gov/Archives/edgar/data/example.htm"
    assert extract_financial_facts(doc) == []


def test_derivations_require_compatible_period_scope_and_definition() -> None:
    current = _evidence(value="123.40")
    prior = _evidence(value="100.00", period="2026-Q1")
    change = derive_absolute_change(current, prior)
    assert change is not None and change.value == "23.40" and change.derivation == "absolute_change"
    bad_scope = _evidence(value="100.00", period="2026-Q1", scope="servicing_for_others")
    assert derive_absolute_change(current, bad_scope) is None
    other_issuer = _evidence(value="100.00", period="2026-Q1", ticker="PFSI")
    assert derive_absolute_change(current, other_issuer) is None
    rate_current = _evidence(value="5.20", metric="servicing_delinquency_rate", period="2026-Q2", unit="percent", scope="servicing", definition="servicing delinquency rate", measure_type="rate")
    rate_prior = _evidence(value="4.90", metric="servicing_delinquency_rate", period="2026-Q1", unit="percent", scope="servicing", definition="servicing delinquency rate", measure_type="rate")
    rate_change = derive_rate_change(rate_current, rate_prior, basis_points=True)
    assert rate_change is not None and rate_change.value == "30.00" and rate_change.unit == "basis_points"


def test_ytd_standalone_only_allows_adjacent_same_year_flow() -> None:
    q2_ytd = _evidence(value="300", period="2026-YTD-Q2")
    q1_ytd = _evidence(value="100", period="2026-YTD-Q1")
    result = derive_standalone_quarter(q2_ytd, q1_ytd, quarter_period="2026-Q2")
    assert result is not None and result.value == "200"
    stock = _evidence(value="300", metric="msr_carrying_value", period="2026-YTD-Q2", measure_type="stock", definition="MSR carrying value")
    stock_prior = _evidence(value="100", metric="msr_carrying_value", period="2026-YTD-Q1", measure_type="stock", definition="MSR carrying value")
    assert derive_standalone_quarter(stock, stock_prior, quarter_period="2026-Q2") is None
    cross_year = _evidence(value="100", period="2025-YTD-Q1")
    assert derive_standalone_quarter(q2_ytd, cross_year, quarter_period="2026-Q2") is None
    non_adjacent = _evidence(value="100", period="2026-YTD-Q1")
    assert derive_standalone_quarter(_evidence(value="300", period="2026-YTD-Q3"), non_adjacent, quarter_period="2026-Q3") is None


def test_metadata_facts_require_source_excerpt_and_location(tmp_path) -> None:
    path = tmp_path / "source.html"
    path.write_text("<p>No source figure is present.</p>", encoding="utf-8")
    doc = Document(issuer="Test", cik="1", title="source", kind="release", source="ir", url="https://example.test/source", published="2026-08-01", period="2026-Q2", path=str(path), content_hash="x", metadata={"ticker": "TST", "facts": [{"metric": "servicing_fee_income", "value": "$12.30 million", "unit": "USD_millions", "scope": "servicing"}]})
    assert extract_financial_facts(doc) == []
    path.write_text("<p>Servicing fee income $12.30 million.</p>", encoding="utf-8")
    doc.metadata["facts"][0].update({"location": "HTML text line 1", "excerpt": "Servicing fee income $12.30 million."})
    facts = extract_financial_facts(doc, config=GENERIC_CONFIG)
    assert len(facts) == 1 and facts[0].value == "12.30"
    # A valid number elsewhere in the archive cannot be paired with an
    # unrelated metadata quote/definition.
    doc.metadata["facts"][0].update({"value": "$999.99 million", "excerpt": "Servicing fee income did not change.", "location": "HTML text line 1"})
    retained = extract_financial_facts(doc, config=GENERIC_CONFIG)
    assert not any(fact.value == "999.99" for fact in retained)


def test_report_has_exact_source_links_prior_comparison_and_generation_time(tmp_path) -> None:
    current = _fact(tmp_path, "<table><tr><th>Metric</th><th>Q2 2026</th></tr><tr><td>Servicing fee income</td><td>$123.40</td></tr></table>", name="current")
    prior = _fact(tmp_path, "<p>Servicing fee income $100.00.</p>", period="2026-Q1", name="prior")
    report = build_report({"companies": [{"ticker": "TST", "name": "Test Bank", "cik": "1"}], "extraction": {"experimental_generic_numeric": True}}, [current], previous_documents=[prior], coverage={"as_of": "2026-09-04T02:00:00+00:00", "new_document_ids": [current.id], "companies_checked": ["TST"]})
    # ``coverage.as_of`` is the source-check timestamp.  Report generation is
    # recorded independently so a later offline rendering cannot impersonate a
    # fresh source check.
    assert report["coverage"]["as_of"] == "2026-09-04T02:00:00+00:00"
    assert report["generated_at"] and report["generated_at"] != report["coverage"]["as_of"]
    assert "2026-09-03" in report["subject"]  # America/New_York conversion
    assert "$123.40" in report["company_reports"]["TST"]["text"]
    assert "$100.00" in report["company_reports"]["TST"]["text"]
    assert "https://example.test/current" in report["html"]
    assert "bankwide" not in report["text"].lower()


def test_narrative_disabled_without_key_and_untrusted_claim_rejected(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fact = _evidence(value="10.00")
    disabled = generate_narrative([fact], ai_config={"enabled": True})
    assert disabled.status == "disabled" and not disabled.used_ai
    text, claims, error = _validate_claims({"executive_points": [{"text": "Fee income was $999.00", "evidence_ids": [fact.id]}], "company_takeaways": []}, [fact])
    assert not claims and error


def test_narrative_rejects_wrong_metric_scope_and_causal_paraphrase() -> None:
    fact = _evidence(value="10.00")
    other_metric = _evidence(value="10.00", metric="servicing_operating_expense", definition="servicing operating expense")
    wrong_metric = _validate_claims(
        {"executive_points": [{"text": fact.excerpt, "evidence_ids": [other_metric.id]}], "company_takeaways": []},
        [fact, other_metric],
    )
    assert not wrong_metric[1] and wrong_metric[2]
    wrong_scope = replace(fact, id="wrong-scope", scope="servicing_for_others")
    wrong_scope_result = _validate_claims(
        {"executive_points": [{"text": fact.excerpt, "evidence_ids": [wrong_scope.id]}], "company_takeaways": []},
        [wrong_scope],
    )
    # A source excerpt cannot be used to silently relabel the population.
    assert not wrong_scope_result[1] and wrong_scope_result[2]
    causal = _validate_claims(
        {"executive_points": [{"text": f"{fact.excerpt} because costs fell", "evidence_ids": [fact.id]}], "company_takeaways": []},
        [fact],
    )
    assert not causal[1] and causal[2]
