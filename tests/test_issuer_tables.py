"""Real official-source excerpts, replayed offline; no invented reporting values."""
from pathlib import Path
from servicing_brief.issuer_tables import extract_tfc_table_text, extract_pfsi_html

FIXTURES = Path(__file__).parent / "fixtures"


def source(ticker, period="2026-Q2"):
    return {"id": ticker + "-real-fixture", "issuer": ticker, "period": period,
            "url": "https://www.sec.gov/Archives/edgar/data/1745916/000110465926088174/tm2621541d1_ex99-1.htm" if ticker == "PFSI" else "https://ir.truist.com/earnings",
            "source": "sec" if ticker == "PFSI" else "ir", "kind": "release", "title": "Q2 2026 earnings"}


def test_tfc_source_columns_scope_and_upb_units():
    facts = extract_tfc_table_text(source("TFC"), (FIXTURES / "tfc_q2_2026_mortgage_table.txt").read_text(encoding="utf-8"), 21)
    current = {f.metric: f for f in facts if f.period == "2026-Q2"}
    assert {k: f.value for k, f in current.items()} == {
        "residential_servicing_income_before_msr_valuation": "70", "residential_msr_valuation": "5",
        "residential_servicing_income": "75", "servicing_for_others_upb": "240764",
        "owned_servicing_upb": "57894", "total_servicing_portfolio_upb": "298658"}
    assert len(facts) == 30
    assert current["servicing_for_others_upb"].unit == "USD_millions"
    assert current["servicing_for_others_upb"].measure_type == "stock"
    assert all("servicing" in f.scope and "PDF page 21" in f.location for f in facts)
    assert not any("pretax" in f.metric or "expense" in f.metric or f.value == "116" for f in facts)
    prior = {f.period: f.value for f in facts if f.metric == "residential_servicing_income"}
    assert prior["2026-Q1"] == "91" and prior["2025-Q2"] == "73"


def test_pfsi_real_table_preserves_parentheses_and_column_periods():
    facts = extract_pfsi_html(source("PFSI"), (FIXTURES / "pfsi_q2_2026_servicing_table.html").read_bytes())
    current = {f.metric: f for f in facts if f.period == "2026-Q2"}
    assert len(facts) == 39
    assert current["servicing_pretax_income"].value == "22"
    assert current["adjusted_servicing_result"].value == "99"
    assert current["msr_cash_flow_realization"].value == "-323"
    assert current["msr_hedge_result"].value == "-187"
    assert current["servicing_valuation_related_items"].value == "-77"
    assert current["servicing_operating_expense"].value == "76"
    assert current["servicing_fee_income"].value == "536"
    assert current["total_servicing_portfolio_upb"].value == "731"
    assert current["total_servicing_portfolio_upb"].unit == "USD_billions"
    assert current["owned_msr_portfolio"].value == "488" and current["subservicing_portfolio"].value == "235"
    prior = {f.period: f.value for f in facts if f.metric == "servicing_pretax_income"}
    assert prior == {"2026-Q2": "22", "2026-Q1": "13", "2025-Q2": "54"}
    assert all(f.source_url and f.excerpt and f.definition for f in facts)


def test_wrong_quarter_and_unrecognized_units_fail_closed():
    tfc = (FIXTURES / "tfc_q2_2026_mortgage_table.txt").read_text(encoding="utf-8")
    pfsi = (FIXTURES / "pfsi_q2_2026_servicing_table.html").read_bytes()
    assert extract_tfc_table_text(source("TFC", "2026-Q3"), tfc, 21) == []
    assert extract_pfsi_html(source("PFSI", "2026-Q3"), pfsi) == []
    assert extract_tfc_table_text(source("TFC"), tfc.replace("Dollars in millions", "unrecognized scale"), 21) == []
    assert extract_pfsi_html(source("PFSI"), pfsi.replace(b"Profitability (in millions)", b"Profitability (unknown scale)")) == []
