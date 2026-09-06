"""Focused checks for source-compatible PFSI headlines and table highlights."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader

from servicing_brief import reporting
from servicing_brief.evidence import Evidence
from servicing_brief.extraction import Commentary
from servicing_brief.models import Document


CONFIG = {
    "companies": [{"ticker": "PFSI", "name": "PennyMac Financial Services, Inc.", "cik": "1745916"}],
    "ai": {"enabled": False},
}
TEMPLATES = Path(__file__).resolve().parents[1] / "servicing_brief" / "templates"


def _document(tmp_path: Path, period: str, name: str) -> Document:
    content = f"PFSI {name} {period}".encode()
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{name}.html"
    path.write_bytes(content)
    return Document(
        issuer="PennyMac Financial Services, Inc.",
        cik="1745916",
        title=name,
        kind="release",
        source="sec",
        url=f"https://example.test/{name}",
        published="2026-07-29",
        period=period,
        path=str(path),
        content_hash=sha256(content).hexdigest(),
        metadata={"ticker": "PFSI"},
    )


def _fact(
    document: Document,
    metric: str,
    value: str,
    *,
    period: str,
    scope: str = "servicing",
    derivation: str = "",
) -> Evidence:
    definitions = {
        "servicing_pretax_income": "Servicing segment pretax income including valuation-related items.",
        "adjusted_servicing_result": "Servicing segment pretax income before valuation-related items.",
        "servicing_valuation_related_items": "Servicing segment valuation-related items.",
        "servicing_interest_expense": "Servicing segment interest expense in the issuer's non-GAAP presentation.",
        "total_servicing_portfolio_upb": "Total servicing portfolio unpaid principal balance.",
    }
    return Evidence(
        id=f"{document.id}-{metric}-{period}-{value}-{scope}",
        document_id=document.id,
        issuer=document.issuer,
        ticker="PFSI",
        metric=metric,
        value=value,
        raw_value=value,
        unit="USD_millions" if metric != "total_servicing_portfolio_upb" else "USD_billions",
        currency="USD",
        period=period,
        scope=scope,
        definition=definitions[metric],
        location=f"HTML table, {metric}, {period}",
        excerpt=f"{metric}: ${value}",
        source_url=document.url,
        source_kind=document.source,
        source_title=document.title,
        document_kind=document.kind,
        published=document.published,
        derivation=derivation,
        measure_type="stock" if metric == "total_servicing_portfolio_upb" else "flow",
    )


def _view(tmp_path: Path, prior_values: dict[str, str] | None, *, prior_scope: str = "servicing", interest_derivation: str = "") -> dict:
    current_document = _document(tmp_path, "2026-Q2", "current")
    prior_document = _document(tmp_path, "2026-Q1", "prior")
    current = [
        _fact(current_document, "servicing_pretax_income", "22", period="2026-Q2"),
        _fact(current_document, "servicing_interest_expense", "140", period="2026-Q2"),
        _fact(current_document, "total_servicing_portfolio_upb", "731", period="2026-Q2"),
    ]
    prior = []
    for metric, value in (prior_values or {}).items():
        prior.append(
            _fact(
                prior_document,
                metric,
                value,
                period="2026-Q1",
                scope=prior_scope,
                derivation=interest_derivation if metric == "servicing_interest_expense" else "",
            )
        )
    view, _changes = reporting._company_view(
        CONFIG,
        [current_document],
        current,
        prior,
        [],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [current_document.id]},
    )
    return view


def test_pfsi_headline_secondary_highlights_and_period_end_note_are_source_exact(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        {
            "servicing_pretax_income": "13",
            "servicing_interest_expense": "125",
            "total_servicing_portfolio_upb": "720",
        },
    )

    assert view["editorial_headline"] == "Servicing pretax income rose from Q1, while financing costs increased."
    assert view["highlight_note"] == "Versus Q1 2026: stronger pretax income and higher interest expense."
    assert sum(row["highlight"] == "green" for row in view["rows"]) == 1
    assert sum(row["highlight"] == "red" for row in view["rows"]) == 1
    assert any("Portfolio UPB is a period-end balance" in note for note in view["notes"])


def test_pfsi_opening_bridge_explains_measure_and_qualification(tmp_path: Path) -> None:
    current_document = _document(tmp_path, "2026-Q2", "current")
    prior_document = _document(tmp_path, "2026-Q1", "prior")
    current = [
        _fact(current_document, "adjusted_servicing_result", "99", period="2026-Q2"),
        _fact(current_document, "servicing_valuation_related_items", "-77", period="2026-Q2"),
        _fact(current_document, "servicing_pretax_income", "22", period="2026-Q2"),
        _fact(current_document, "servicing_interest_expense", "140", period="2026-Q2"),
        _fact(current_document, "total_servicing_portfolio_upb", "731", period="2026-Q2"),
    ]
    prior = [
        _fact(prior_document, "adjusted_servicing_result", "57", period="2026-Q1"),
        _fact(prior_document, "servicing_valuation_related_items", "-44", period="2026-Q1"),
        _fact(prior_document, "servicing_pretax_income", "13", period="2026-Q1"),
        _fact(prior_document, "servicing_interest_expense", "125", period="2026-Q1"),
        _fact(prior_document, "total_servicing_portfolio_upb", "720", period="2026-Q1"),
    ]
    view, _changes = reporting._company_view(
        CONFIG,
        [current_document],
        current,
        prior,
        [],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [current_document.id]},
    )

    assert view["executive_intro"]["text"] == (
        "Servicing income excluding valuation-related items rose to $99m from $57m in Q1 2026. "
        "Valuation-related items of ($77m) left reported servicing pretax income at $22m. "
        "The issuer-defined prevaluation measure includes mortgage servicing rights (MSR) "
        "cash-flow realization and financing expense. It is not cash earnings."
    )


def test_pfsi_revenue_explanation_names_supported_measure_and_keeps_citation(tmp_path: Path) -> None:
    current_document = _document(tmp_path, "2026-Q2", "current")
    commentary = Commentary(
        id="revenue-commentary",
        document_id=current_document.id,
        issuer=current_document.issuer,
        ticker="PFSI",
        period="2026-Q2",
        text=(
            "Servicing revenues excluding valuation-related items totaled $369 million, up from $316 million in the prior quarter. "
            "The increase from the prior quarter was primarily due to lower realization of MSR cash flows, reflecting lower prepayment speeds, "
            "and an increase in earnings on custodial deposits and other income due to higher average balances."
        ),
        location="HTML text line 392",
        source_url=current_document.url,
        source_title=current_document.title,
    )
    view, _changes = reporting._company_view(
        CONFIG,
        [current_document],
        [_fact(current_document, "servicing_pretax_income", "22", period="2026-Q2")],
        [],
        [commentary],
        baseline=True,
        coverage={"as_of": "2026-09-04T12:00:00+00:00", "new_document_ids": [current_document.id]},
    )

    assert view["explanations"][0]["summary"] == (
        "Management attributed higher servicing revenue excluding valuation-related items primarily to slower prepayments, "
        "which reduced MSR cash-flow realization, and higher average custodial balances, which lifted earnings on deposits and other income."
    )
    assert view["explanations"][0]["source_number"] == 1
    assert view["explanations"][0]["url"] == current_document.url
    assert view["explanations"][0]["location"] == "HTML text line 392"


def test_highlight_note_is_visible_in_both_reader_templates(tmp_path: Path) -> None:
    view = _view(
        tmp_path,
        {
            "servicing_pretax_income": "13",
            "servicing_interest_expense": "125",
            "total_servicing_portfolio_upb": "720",
        },
    )
    headline_main, separator, headline_secondary = view["editorial_headline"].partition(". ")
    context = {
        **view,
        "subject": "PFSI review",
        "headline_main": headline_main + ("." if separator else ""),
        "headline_secondary": headline_secondary,
        "table_citations": "",
        "selected_excerpts": [],
        "reviewed_context": [],
        "call_passages": [],
        "context_heading": "Portfolio and risk",
        "company_identity": {"ticker": "PFSI", "cik": "0001745916", "event": "2026-Q2", "kind": "earnings_brief"},
        "brand": {"verified": False, "primary_color": "#243f56", "logo_src": "", "logo_alt": "", "width": 0, "height": 0},
    }
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=False)
    html = env.get_template("brief.html.j2").render(**context)
    text = env.get_template("brief.txt.j2").render(**context)

    assert view["highlight_note"] in BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    assert view["highlight_note"] in text


@pytest.mark.parametrize(
    ("prior_values", "expected_headline"),
    [
        (None, "Servicing pretax income: $22m"),
        ({"servicing_pretax_income": "22", "servicing_interest_expense": "140"}, "Servicing pretax income held steady versus Q1."),
        ({"servicing_pretax_income": "30", "servicing_interest_expense": "160"}, "Servicing pretax income fell from Q1."),
    ],
)
def test_pfsi_missing_flat_and_reversed_comparisons_have_no_false_highlights(
    tmp_path: Path, prior_values: dict[str, str] | None, expected_headline: str
) -> None:
    view = _view(tmp_path, prior_values)

    assert view["editorial_headline"] == expected_headline
    assert view["highlight_note"] == ""
    assert all(row["highlight"] == "" for row in view["rows"])


def test_pfsi_incompatible_and_derived_interest_comparisons_do_not_claim_source_exact_change(tmp_path: Path) -> None:
    incompatible = _view(
        tmp_path / "incompatible",
        {"servicing_pretax_income": "13", "servicing_interest_expense": "125"},
        prior_scope="servicing_owned_msr",
    )
    assert incompatible["editorial_headline"] == "Servicing pretax income: $22m"
    assert incompatible["highlight_note"] == ""
    assert all(row["highlight"] == "" for row in incompatible["rows"])

    derived_interest = _view(
        tmp_path / "derived",
        {"servicing_pretax_income": "13", "servicing_interest_expense": "125"},
        interest_derivation="absolute_change",
    )
    assert derived_interest["editorial_headline"] == "Servicing pretax income rose from Q1."
    assert derived_interest["highlight_note"] == "Versus Q1 2026: stronger pretax income."
    assert [row["highlight"] for row in derived_interest["rows"]].count("red") == 0


def test_original_analysis_follows_questions_in_both_templates(tmp_path: Path) -> None:
    from servicing_brief.branding import apply_page_theme, brand_view, validate_page_theme
    view = _view(tmp_path, {"servicing_pretax_income": "13", "servicing_interest_expense": "125"})
    view.update(subject="PFSI review", company_identity={"ticker": "PFSI", "cik": "0001745916", "event": "2026-Q2"},
                brand=brand_view("PFSI", "PennyMac Financial Services"),
                ai_analysis=[{"title": "The cost of growth", "text": "Growth creates a financing question that the portfolio balance alone cannot answer.",
                              "sources": [{"number": 1, "source_url": "https://example.test/release", "location": "Servicing results"}]}])
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)
    html = apply_page_theme(env.get_template("brief.html.j2").render(**view), view["brand"]["theme"])
    text = env.get_template("brief.txt.j2").render(**view)
    headings = [h.get_text(strip=True) for h in BeautifulSoup(html, "html.parser").find_all("h2")]
    assert headings.index("Questions") < headings.index("AI Analysis") < headings.index("Sources")
    assert text.index("Questions") < text.index("AI Analysis") < text.index("Sources\n")
    assert "Investor questions" not in text
    validate_page_theme(html, "PFSI")
