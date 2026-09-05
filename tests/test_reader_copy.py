"""Display punctuation must not change financial signs or source addresses."""
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

from servicing_brief.reporting import _reader_copy, _amount
from servicing_brief.reader_content import assert_reader_content


def test_reader_punctuation_preserves_financial_values_urls_and_original_evidence():
    original = {
        "headline": "Earnings improved — funding costs climbed.",
        "rows": [{"current": "−$77m", "label": "Annual context — FY2025"}],
        "sources": [{"source_url": "https://example.test/original—document", "text": "Management — prepared remarks"}],
        "locations": [("Annual report — p. 21", "https://example.test/annual—report")],
    }
    preserved = deepcopy(original)
    displayed = _reader_copy(original)
    assert displayed["headline"] == "Earnings improved; funding costs climbed."
    assert displayed["rows"][0]["current"] == "−$77m"
    assert displayed["rows"][0]["label"] == "Annual context; FY2025"
    assert displayed["sources"][0]["source_url"] == original["sources"][0]["source_url"]
    assert displayed["locations"][0][1] == original["locations"][0][1]
    assert "—" not in displayed["sources"][0]["text"]
    assert original == preserved


def test_missing_value_is_explicit_and_citation_tooltip_is_reader_safe():
    assert _reader_copy(_amount(None)) == 'n/a'
    original = {'citations': '<a href="https://example.test/a—b" title="Annual report — p. 7">[1]</a>'}
    displayed = _reader_copy(original)
    assert 'href="https://example.test/a—b"' in displayed['citations']
    assert_reader_content(displayed['citations'], '')
    assert 'Annual report — p. 7' in original['citations']


def test_amount_uses_parentheses_for_negative_currency_and_preserves_decimal_value():
    fact = SimpleNamespace(value=Decimal("-77"), unit="USD_millions", currency="USD")

    assert _amount(fact) == "($77m)"
    assert fact.value == Decimal("-77")


def test_amount_uses_parentheses_for_negative_percent_and_basis_points():
    percent = SimpleNamespace(value="-4.25", unit="percent", currency="")
    basis_points = SimpleNamespace(value="-37", unit="basis_points", currency="")

    assert _amount(percent) == "(4.25%)"
    assert _amount(basis_points) == "(37 bps)"


def test_amount_leaves_positive_zero_and_missing_values_unchanged():
    positive = SimpleNamespace(value="77", unit="USD_millions", currency="USD")
    zero = SimpleNamespace(value="0", unit="USD_millions", currency="USD")

    assert _amount(positive) == "$77m"
    assert _amount(zero) == "$0m"
    assert _amount(None) == "n/a"
