"""Regression coverage for deliberate editorial omissions in the one-off renderer."""

from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).parents[1]
RENDER_PATH = ROOT / "output" / "five-company-review" / "render_email.py"
TEMPLATE_ROOT = RENDER_PATH.parent
spec = importlib.util.spec_from_file_location("five_company_render_email", RENDER_PATH)
assert spec and spec.loader
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)


def _raw_review(*, with_chart: bool = False) -> dict:
    metrics = [
        {"label": "Servicing balance", "current": "C$120m", "previous": "C$100m", "previous_label": "Q2 2026", "sources": ["1"], "highlight": ""},
        {"label": "Non-comparable balance", "current": "C$999m", "previous": "C$888m", "previous_label": "Q2 2026", "sources": ["1"], "highlight": ""},
        {"label": "Early arrears", "current": "C$40m", "previous": "C$35m", "previous_label": "Q2 2026", "sources": ["1"], "highlight": ""},
    ]
    review = {
        "ticker": "TST",
        "cik": "0000123456",
        "name": "Test issuer",
        "period": "Q3 2026",
        "call_date": "2026-08-01",
        "headline": "Mortgage balances increased",
        "summary": "Reported mortgage balances rose during the quarter.",
        "summary_sources": ["1"],
        "metrics": metrics,
        "analysis": [],
        "call_note": "",
        "call_note_sources": [],
        "investor_question": "How will the issuer manage mortgage risk?",
        "sources": [{"id": "1", "label": "Quarterly report", "url": "https://issuer.example/report"}],
        "chart": {
            "title": "Servicing balance",
            "unit": "C$m",
            "sources": ["1"],
            "points": [
                {"period": "Q2 2026", "value": "100", "display": "$100m"},
                {"period": "Q3 2026", "value": "120", "display": "$120m"},
            ],
        } if with_chart else None,
    }
    return review


def _overlay(*, rows: list[int], excluded_metrics: list[dict] | None = None, with_chart: bool = False, insights: list[dict] | None = None) -> dict:
    return {
        "version": 1,
        "metric_groups": [{
            "title": "Mortgage figures",
            "previous_label": "Q2 2026",
            "current_label": "Q3 2026",
            "rows": [{"metric_index": index, "unit": "C$m"} for index in rows],
        }],
        "excluded_metrics": excluded_metrics or [],
        "insights": [] if insights is None else insights,
        "chart": {"omit": True, "reason": "The comparable series adds no distinct reader conclusion."} if with_chart else None,
    }


def _render(company: dict) -> str:
    env = Environment(loader=FileSystemLoader([TEMPLATE_ROOT, Path(__file__).resolve().parents[1] / 'servicing_brief/templates']), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    return renderer._render_html(env, [company], combined=False, universe_note="", attachment_note="", full_document=False)


def test_documented_metric_omission_passes_without_mutating_canonical_data() -> None:
    raw = _raw_review()
    before = sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
    company = renderer._review(
        raw,
        "TST",
        False,
        _overlay(
            rows=[0, 2],
            excluded_metrics=[{"metric_index": 1, "reason": "Outside the comparable mortgage servicing scope."}],
        ),
        1,
    )

    assert [row["metric_index"] for row in company["metric_groups"][0]["rows"]] == [0, 2]
    assert company["excluded_metrics"] == [{"metric_index": 1, "reason": "Outside the comparable mortgage servicing scope."}]
    assert sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest() == before


def test_excluded_numeric_value_is_never_rendered() -> None:
    raw = _raw_review()
    company = renderer._review(
        raw,
        "TST",
        False,
        _overlay(
            rows=[0, 2],
            excluded_metrics=[{"metric_index": 1, "reason": "Outside the comparable mortgage servicing scope."}],
        ),
        1,
    )
    soup = BeautifulSoup(_render(company), "html.parser")

    assert soup.select_one('tr[data-metric-index="0"]') is not None
    assert soup.select_one('tr[data-metric-index="2"]') is not None
    assert soup.select_one('tr[data-metric-index="1"]') is None
    assert "999" not in soup.get_text(" ", strip=True)


def test_undocumented_metric_loss_fails_closed() -> None:
    with pytest.raises(ValueError, match="explicitly exclude|cover every canonical"):
        renderer._review(_raw_review(), "TST", False, _overlay(rows=[0, 2]), 1)


def test_excluded_metric_cannot_also_be_visible() -> None:
    with pytest.raises(ValueError, match="both visible and excluded"):
        renderer._review(
            _raw_review(),
            "TST",
            False,
            _overlay(
                rows=[0, 1, 2],
                excluded_metrics=[{"metric_index": 1, "reason": "Outside the comparable mortgage servicing scope."}],
            ),
            1,
        )


def test_missing_metric_omission_reason_fails_closed() -> None:
    with pytest.raises(ValueError, match="meaningful reason"):
        renderer._review(
            _raw_review(),
            "TST",
            False,
            _overlay(rows=[0, 2], excluded_metrics=[{"metric_index": 1, "reason": ""}]),
            1,
        )


def test_chart_omission_is_internal_and_has_no_visible_note() -> None:
    company = renderer._review(
        _raw_review(with_chart=True),
        "TST",
        False,
        _overlay(rows=[0, 1, 2], with_chart=True),
        1,
    )
    html = _render(company)

    assert company["chart"] is None
    assert "distinct reader conclusion" in company["chart_note"]
    assert BeautifulSoup(html, "html.parser").select_one(".earnings-chart") is None
    assert "distinct reader conclusion" not in html


def test_standalone_text_starts_with_publisher_identity_and_issue_date() -> None:
    company = renderer._review(_raw_review(), "TST", False, _overlay(rows=[0, 1, 2]), 1)

    text = renderer._render_text([company], combined=False, universe_note="", attachment_note="")

    assert text.startswith("The Servicing Brief\nSeptember 5, 2026\n\n")
    assert text.count("The Servicing Brief") == 1


def test_unused_source_can_be_omitted_but_cited_source_cannot() -> None:
    raw = _raw_review()
    raw["sources"].append({"id": "2", "label": "Unused annual document", "url": "https://issuer.example/annual"})
    overlay = _overlay(rows=[0, 1, 2])
    overlay["excluded_sources"] = [{"source_id": "2", "reason": "No retained claim requires this annual document."}]
    company = renderer._review(raw, "TST", False, overlay, 1)
    assert [s["id"] for s in company["sources"]] == ["1"]
    assert "Unused annual document" not in _render(company)
    assert len(raw["sources"]) == 2
    overlay["excluded_sources"][0]["source_id"] = "1"
    with pytest.raises(ValueError, match="still supports visible content"):
        renderer._review(raw, "TST", False, overlay, 1)


@pytest.mark.parametrize('value,unit', [('C$120m','%'),('0.51%','C$m'),('$120m','C$m'),('C$120m','US$ millions'),('C$120','C$m')])
def test_editorial_unit_cannot_change_currency_or_measure(value, unit):
    with pytest.raises(ValueError):
        renderer._numeric_display(value, unit)


def test_explicit_same_currency_rescaling_preserves_value():
    assert renderer._numeric_display('C$1.25bn','C$m') == '1,250'
    assert renderer._numeric_display('C$1250m','C$bn') == '1.25'


def test_negative_dollar_and_cad_scalars_use_accounting_parentheses():
    assert renderer._numeric_display('-$120m') == '($120m)'
    assert renderer._numeric_display('C$-120m') == '(C$120m)'
    assert renderer._numeric_display('-C$120m') == '(C$120m)'
    assert renderer._numeric_display('-$10m to -$5m') == '-$10m to -$5m'
    assert renderer._financial_display('-$10m to -$5m') == '($10m) to ($5m)'
    assert renderer._financial_display('Servicing income fell -$120m; well-known risks remain.') == 'Servicing income fell ($120m); well-known risks remain.'
    assert renderer._financial_display('Source https://example.test/C$-77m.pdf') == 'Source https://example.test/C$-77m.pdf'
    assert renderer._financial_display('Report - 2026') == 'Report - 2026'


def test_negative_percentage_display_keeps_positive_and_zero_forms_unchanged():
    assert renderer._numeric_display('-12.5%') == '(12.5%)'
    assert renderer._numeric_display('-12.5%', '%') == '(12.5%)'
    assert renderer._numeric_display('-12 bps') == '(12 bps)'
    assert renderer._numeric_display('12.5%', '%') == '12.5%'
    assert renderer._numeric_display('0%', '%') == '0%'
    assert renderer._numeric_display('-0%') == '0%'
    assert renderer._numeric_display('-0') == '0'


def test_negative_same_currency_rescaling_keeps_target_scale():
    assert renderer._numeric_display('-C$1.25bn', 'C$m') == '(1,250)'
    assert renderer._numeric_display('C$-1250m', 'C$bn') == '(1.25)'
    assert renderer._numeric_display('C$-0m', 'C$bn') == '0'


def test_negative_chart_display_preserves_signed_geometry_and_accessibility():
    raw_chart = {
        'title': 'Servicing income',
        'unit': 'C$m',
        'sources': ['1'],
        'points': [
            {'period': 'Q2 2026', 'value': '-10', 'display': 'C$-10m'},
            {'period': 'Q3 2026', 'value': '20', 'display': 'C$20m'},
        ],
    }
    chart, note = renderer._chart(raw_chart, None, 'TST', {'1': '1'})

    assert not note
    assert chart is not None
    negative, positive = chart['points']
    assert negative['value'] == '-10'
    assert negative['negative'] is True
    assert negative['height'] == 26
    assert negative['display'] == '(10)'
    assert negative['aria_display'] == '(C$10m)'
    assert positive['value'] == '20'
    assert positive['negative'] is False
    assert positive['height'] == 52
    assert positive['aria_display'] == 'C$20m'
    assert 'Q2 2026: (C$10m)' in chart['aria_label']
    assert 'Q3 2026: C$20m' in chart['aria_label']

    zero_chart, zero_note = renderer._chart(
        {
            'title': 'Zero point',
            'unit': 'C$m',
            'sources': ['1'],
            'points': [
                {'period': 'Q2 2026', 'value': '0', 'display': 'C$-0m'},
                {'period': 'Q3 2026', 'value': '20', 'display': 'C$20m'},
            ],
        },
        None,
        'TST',
        {'1': '1'},
    )
    assert not zero_note
    assert zero_chart is not None
    assert zero_chart['points'][0]['aria_display'] == 'C$0m'
    assert '-10m' not in chart['aria_label']


def test_negative_summary_table_and_plain_text_accessibility_use_accounting_display():
    raw = _raw_review(with_chart=True)
    raw['summary'] = 'Reported servicing income declined by -$45m.'
    raw['metrics'][0]['current'] = 'C$-120m'
    raw['metrics'][0]['previous'] = 'C$-100m'
    raw['chart']['unit'] = 'C$m'
    raw['chart']['points'] = [
        {'period': 'Q2 2026', 'value': '-10', 'display': 'C$-10m'},
        {'period': 'Q3 2026', 'value': '20', 'display': 'C$20m'},
    ]
    company = renderer._review(raw, 'TST', False, _overlay(rows=[0, 1, 2]), 1)
    html = _render(company)
    text = renderer._render_text([company], combined=False, universe_note='', attachment_note='')
    soup = BeautifulSoup(html, 'html.parser')

    assert '($45m)' in soup.get_text(' ', strip=True)
    assert '(100)' in soup.get_text(' ', strip=True)
    assert '(120)' in soup.get_text(' ', strip=True)
    assert soup.select_one('.earnings-chart')['aria-label'].find('(C$10m)') >= 0
    assert soup.select_one('td.previous-value')['aria-label'].endswith('(100)')
    assert soup.select_one('td.current-value')['aria-label'].endswith('(120)')
    assert 'C$-10m' not in html
    assert '-$45m' not in html
    assert '($45m)' in text
    assert '(100)' in text
    assert 'C$-10m' not in text
