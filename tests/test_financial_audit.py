"""Focused offline regressions for financial evidence integrity and safety."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from servicing_brief.evidence import Evidence, derive_absolute_change, parse_decimal
from servicing_brief.extraction import (
    ArchiveIntegrityError,
    extract_commentary,
    extract_financial_facts,
)
from servicing_brief.issuer_tables import extract_issuer_tables
from servicing_brief.models import Document
from servicing_brief.narrative import _validate_claims


def _document(tmp_path: Path, content: bytes, *, name: str = "source") -> Document:
    path = tmp_path / f"{name}.html"
    path.write_bytes(content)
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
        content_hash=sha256(content).hexdigest(),
        metadata={"ticker": "TST"},
    )


def _fact(
    *,
    value: str = "10",
    period: str = "2026-Q2",
    definition: str = "servicing fee income",
    status: str = "supported",
) -> Evidence:
    return Evidence(
        id=f"fact-{value}-{period}-{status}",
        document_id="doc",
        issuer="Test Bank",
        ticker="TST",
        metric="servicing_fee_income",
        value=value,
        raw_value=value,
        unit="USD_millions",
        currency="USD",
        period=period,
        scope="servicing",
        definition=definition,
        location="HTML text line 1",
        excerpt=f"Servicing fee income was ${value} million.",
        source_url="https://example.test/source",
        source_kind="ir",
        source_title="Second quarter 2026 results",
        document_kind="release",
        measure_type="flow",
        status=status,
    )


def test_unicode_minus_missing_marker_is_missing_and_never_numeric() -> None:
    assert parse_decimal("−", allow_missing=True) is None
    with pytest.raises(ValueError, match="missing financial value"):
        parse_decimal("−")


def test_monetary_row_keeps_explicit_scale_when_context_mentions_rate(tmp_path: Path) -> None:
    raw = b"<p>Dollars in millions; rate context. Servicing fee income $123.4</p>"
    document = _document(tmp_path, raw)
    facts = extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}})
    income = next(fact for fact in facts if fact.metric == "servicing_fee_income")
    assert income.unit == "USD_millions"
    assert income.value == "123.4"


def test_unrelated_neighboring_amount_cannot_supply_a_money_scale(tmp_path: Path) -> None:
    raw = b"<p>Portfolio $2 billion.</p><p>Servicing fee income $123.</p>"
    document = _document(tmp_path, raw, name="neighbor-scale")
    facts = extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}})
    income = next(fact for fact in facts if fact.metric == "servicing_fee_income")
    assert income.unit == "USD"


def test_neighboring_rate_row_cannot_relabel_a_fee_amount(tmp_path: Path) -> None:
    raw = b"<p>Dollars in millions</p><p>Servicing fee rate for others 2.5%</p><p>Servicing fee income for others $12</p>"
    document = _document(tmp_path, raw, name="neighbor-rate")
    facts = extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}})
    fee_facts = [fact for fact in facts if fact.metric == "servicing_fee_income"]
    assert len(fee_facts) == 1
    assert fee_facts[0].unit == "USD_millions"
    assert not any(fact.metric == "servicing_fee_rate" and fact.raw_value == "$12" for fact in facts)


def test_changed_archived_bytes_are_rejected_before_evidence_or_commentary(tmp_path: Path) -> None:
    raw = b"<p>Servicing fee income $123.4 million.</p>"
    document = _document(tmp_path, raw)
    Path(document.path).write_bytes(b"<p>Servicing fee income $999.9 million.</p>")

    with pytest.raises(ArchiveIntegrityError, match="hash does not match"):
        extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}})
    with pytest.raises(ArchiveIntegrityError, match="hash does not match"):
        extract_commentary(document)


def test_direct_issuer_table_extraction_also_checks_archive_hash(tmp_path: Path) -> None:
    raw = b"<html><body>Servicing Segment Highlights</body></html>"
    document = _document(tmp_path, raw, name="issuer-table")
    document.cik = "1745916"
    Path(document.path).write_bytes(b"<html><body>replacement</body></html>")

    with pytest.raises(ArchiveIntegrityError, match="hash does not match"):
        extract_issuer_tables(document)


def test_population_qualifiers_block_including_excluding_comparison() -> None:
    current = _fact(definition="servicing portfolio including loans held for sale")
    prior = _fact(period="2026-Q1", definition="servicing portfolio excluding loans held for sale")
    assert derive_absolute_change(current, prior) is None

    equivalent_wording = _fact(period="2026-Q1", definition="servicing portfolio includes loans held for sale")
    assert derive_absolute_change(current, equivalent_wording) is not None

    noun_mismatch = _fact(period="2026-Q1", definition="servicing portfolio with exclusion of loans held for sale")
    inclusion = _fact(definition="servicing portfolio with inclusion of loans held for sale")
    assert derive_absolute_change(inclusion, noun_mismatch) is None


def test_narrative_cannot_promote_non_supported_evidence() -> None:
    fact = _fact(status="rejected")
    result = _validate_claims(
        {
            "executive_points": [{"text": fact.excerpt, "evidence_ids": [fact.id]}],
            "company_takeaways": [],
        },
        [fact],
    )
    assert not result[1]
    assert result[2] == "claim cites unsupported evidence"


def test_narrative_requires_servicing_scope_in_the_source_excerpt() -> None:
    fact = _fact()
    bankwide_quote = "Total company revenue was $10 million."
    result = _validate_claims(
        {
            "executive_points": [{"text": bankwide_quote, "evidence_ids": [fact.id]}],
            "company_takeaways": [],
        },
        [replace(fact, excerpt=bankwide_quote)],
    )
    assert not result[1]
    assert result[2] == "claim cites an unsupported or mismatched business scope"


def test_metadata_status_and_id_cannot_override_source_integrity(tmp_path: Path) -> None:
    raw = b"<p>Servicing charges recorded: $12.30 million.</p>"
    document = _document(tmp_path, raw, name="metadata")
    document.metadata["facts"] = [{
        "id": "caller-controlled-id",
        "metric": "servicing_fee_income",
        "value": "$12.30 million",
        "unit": "USD_millions",
        "scope": "servicing",
        "location": "HTML text line 1",
        "excerpt": "Servicing charges recorded: $12.30 million.",
        "status": "unsupported",
    }]
    config = {"extraction": {"experimental_generic_numeric": True}}
    assert extract_financial_facts(document, config=config) == []

    document.metadata["facts"][0]["status"] = "supported"
    facts = extract_financial_facts(document, config=config)
    assert len(facts) == 1
    assert facts[0].id != "caller-controlled-id"


def test_standalone_metadata_fixture_can_supply_quoted_evidence() -> None:
    document = Document(
        issuer="Test Bank",
        cik="0000000001",
        title="Second quarter 2026 results",
        kind="release",
        source="ir",
        url="https://example.test/metadata-only",
        published="2026-07-20T12:00:00+00:00",
        period="2026-Q2",
        path="",
        content_hash="",
        metadata={
            "ticker": "TST",
            "text": "Servicing fee income was $12.30 million.",
            "facts": [{
                "metric": "servicing_fee_income",
                "value": "$12.30 million",
                "unit": "USD_millions",
                "scope": "servicing",
                "location": "text line 1",
                "excerpt": "Servicing fee income was $12.30 million.",
            }],
        },
    )
    facts = extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}})
    assert len(facts) == 1
    assert facts[0].value == "12.30"


def test_metadata_scope_must_be_visible_in_the_archived_quote(tmp_path: Path) -> None:
    raw = b"<p>Total company revenue was $12.30 million.</p>"
    document = _document(tmp_path, raw, name="wrong-scope")
    document.metadata["facts"] = [{
        "metric": "servicing_fee_income",
        "value": "$12.30 million",
        "unit": "USD_millions",
        "scope": "servicing",
        "location": "HTML text line 1",
        "excerpt": "Total company revenue was $12.30 million.",
    }]
    assert extract_financial_facts(document, config={"extraction": {"experimental_generic_numeric": True}}) == []
