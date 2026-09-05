"""Strict, source-layout-aware rules for the two end-to-end proof issuers.

Rules recognize table headings, column periods, row definitions and stated
units. They never contain a reporting value. Unrecognized layouts return no
facts, allowing the caller to disclose a coverage limitation.
"""
from calendar import monthrange
from pathlib import Path
import re
from bs4 import BeautifulSoup
import pymupdf
from .evidence import Evidence, evidence_id, parse_decimal, decimal_string
from .extraction import _verify_archive


def _v(doc, key, default=""):
    return doc.get(key, default) if isinstance(doc, dict) else getattr(doc, key, default)


def _fact(doc, ticker, metric, raw, unit, period, scope, definition, location, excerpt, measure_type, notes=""):
    value = parse_decimal(raw, allow_missing=True)
    if value is None:
        return None
    match = re.fullmatch(r"(\d{4})-Q([1-4])", period)
    year, quarter = int(match[1]), int(match[2])
    end_month = quarter * 3
    end = f"{year}-{end_month:02d}-{monthrange(year, end_month)[1]}"
    start = f"{year}-{end_month - 2:02d}-01" if measure_type == "flow" else ""
    return Evidence(id=evidence_id(_v(doc, "id"), metric, raw, location, period), document_id=_v(doc, "id"),
                    issuer=_v(doc, "issuer"), ticker=ticker, metric=metric, value=decimal_string(value), raw_value=raw,
                    unit=unit, currency="USD" if unit.startswith("USD") else "", period=period, scope=scope,
                    definition=definition, location=location, excerpt=excerpt, source_url=_v(doc, "url"),
                    source_kind=_v(doc, "source"), source_title=_v(doc, "title"), document_kind=_v(doc, "kind"),
                    published=_v(doc, "published"), sign="parentheses" if raw.startswith("(") else "reported",
                    measure_type=measure_type, period_start=start, period_end=end, notes=notes)


_TFC_ROWS = {
    "Residential mortgage servicing income before MSR valuation": ("residential_servicing_income_before_msr_valuation", "servicing_residential", "flow", "Residential mortgage servicing income before MSR valuation; this is income, not servicing pretax profit or operating expense."),
    "Total residential mortgage servicing income": ("residential_servicing_income", "servicing_residential", "flow", "Total residential mortgage servicing income including net MSR valuation; excludes residential production and commercial mortgage income."),
    "Loans serviced for others": ("servicing_for_others_upb", "servicing_residential_for_others", "stock", "Residential mortgage loans serviced for others; unpaid principal balance per table footnote (1)."),
    "Bank-owned loans serviced": ("owned_servicing_upb", "servicing_residential_bank_owned", "stock", "Bank-owned residential mortgage loans serviced; unpaid principal balance per table footnote (1), not owned MSR portfolio."),
    "Total servicing portfolio": ("total_servicing_portfolio_upb", "servicing_residential_total", "stock", "Total residential mortgage servicing portfolio including loans serviced for others and bank-owned loans; unpaid principal balance per table footnote (1)."),
}
_NUMBER = re.compile(r"\(?-?\d[\d,]*(?:\.\d+)?\)?|[—–]")
_MONTH_QUARTER = {"March": 1, "June": 2, "Sept.": 3, "September": 3, "Dec.": 4, "December": 4}


def extract_tfc_table_text(doc, text, page_number):
    """Parse PyMuPDF sorted-layout text from the named five-quarter table."""
    heading = "Selected Mortgage Banking Information"
    if heading not in text or "Amounts reported are unpaid principal balance" not in text:
        return []
    if "Dollars in millions" not in text:
        return []
    lines = text.splitlines()
    dates = next((re.findall(r"\b(March|June|Sept\.|September|Dec\.|December)\s+(?:30|31)\b", line) for line in lines if len(re.findall(r"(?:30|31)\b", line)) >= 3), [])
    year_line = next((line for line in lines[:10] if len(re.findall(r"\b20\d{2}\b", line)) >= 3), "")
    years = re.findall(r"\b20\d{2}\b", year_line)
    if not dates or len(dates) != len(years):
        return []
    periods = [f"{year}-Q{_MONTH_QUARTER[month]}" for month, year in zip(dates, years)]
    if _v(doc, "period") not in ("", "unknown", periods[0]):
        return []
    result = []
    residential = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Residential mortgage servicing income:":
            residential = True
        if stripped.startswith("Commercial mortgage income"):
            residential = False
        row_definition = next(((label, spec) for label, spec in _TFC_ROWS.items() if stripped.startswith(label)), None)
        if residential and stripped.startswith("Net MSRs valuation"):
            row_definition = ("Net MSRs valuation", ("residential_msr_valuation", "servicing_residential", "flow", "Net MSRs valuation within residential mortgage servicing income; distinct from commercial servicing MSR valuation."))
        if not row_definition:
            continue
        label, (metric, scope, measure, definition) = row_definition
        raws = _NUMBER.findall(stripped[len(label):])
        if len(raws) != len(periods):
            continue
        excerpt = "\n".join([heading + " & Additional Information", "Dollars in millions; As of/For the Quarter Ended", " | ".join(periods), stripped, "(1) Amounts reported are unpaid principal balance."])
        for raw, period in zip(raws, periods):
            item = _fact(doc, "TFC", metric, raw, "USD_millions", period, scope, definition,
                         f"PDF page {page_number}, Selected Mortgage Banking Information, row '{label}', column {period}", excerpt, measure)
            if item:
                result.append(item)
    return result


_PFSI_ROWS = {
    "Total UPB ($ in billions, at period end)": ("total_servicing_portfolio_upb", "USD_billions", "servicing_total", "stock", "Total servicing UPB including owned servicing, subservicing and loans held for sale; period-end unpaid principal balance."),
    "Owned servicing": ("owned_msr_portfolio", "USD_billions", "servicing_owned_msr", "stock", "Owned servicing portfolio unpaid principal balance; excludes subservicing and loans held for sale."),
    "Subservicing": ("subservicing_portfolio", "USD_billions", "servicing_subservicing", "stock", "Subservicing portfolio unpaid principal balance; excludes owned servicing and loans held for sale."),
    "Loan servicing fees": ("servicing_fee_income", "USD_millions", "servicing", "flow", "Loan servicing fees within the servicing segment; excludes production revenue."),
    "Operating expenses": ("servicing_operating_expense", "USD_millions", "servicing", "flow", "Servicing segment operating expenses in the issuer's non-GAAP presentation; excludes separately listed payoff, credit and interest expenses."),
    "Interest expense": ("servicing_interest_expense", "USD_millions", "servicing", "flow", "Servicing segment interest expense in the issuer's non-GAAP presentation."),
    "Pretax income": ("servicing_pretax_income", "USD_millions", "servicing", "flow", "Servicing segment pretax income including valuation-related items; distinct from companywide net income."),
    "Pretax income excluding valuation-related items": ("adjusted_servicing_result", "USD_millions", "servicing", "flow", "Reported non-GAAP servicing segment pretax income excluding valuation-related items; see issuer reconciliations, not equivalent to GAAP pretax income."),
    "Valuation-related items": ("servicing_valuation_related_items", "USD_millions", "servicing", "flow", "Reported servicing valuation-related items in the non-GAAP segment presentation, including MSR, hedge and active-loan provision effects."),
    "MSR fair value changes": ("msr_fair_value_change", "USD_millions", "servicing", "flow", "Servicing MSR fair value changes in the issuer's non-GAAP presentation, excluding separately listed hedging results."),
    "Hedging results (4)": ("msr_hedge_result", "USD_millions", "servicing", "flow", "Servicing hedging results; table footnote (4) includes principal-only stripped MBS valuation-related accretion changes included in GAAP net interest income."),
    "Realization of mortgage servicing rights (MSR) cash flows": ("msr_cash_flow_realization", "USD_millions", "servicing", "flow", "Realization of MSR cash flows as signed in the issuer's servicing presentation; distinct from MSR fair value changes."),
    "Expenses excluding valuation-related items": ("servicing_expenses_excluding_valuation", "USD_millions", "servicing", "flow", "Reported non-GAAP servicing expenses excluding valuation-related items, including operating, payoff, credit and interest expenses."),
}


def extract_pfsi_html(doc, raw):
    soup = BeautifulSoup(raw, "html.parser")
    result = []
    for table_number, table in enumerate(soup.find_all("table"), 1):
        table_text = table.get_text(" ", strip=True)
        if not all(value in table_text for value in ("Servicing portfolio", "Loan servicing fees", "Pretax income excluding valuation-related items", "Profitability (in millions)")):
            continue
        rows = table.find_all("tr")
        header_cells = None
        for row in rows[:4]:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
            if len([c for c in cells if re.fullmatch(r"[1-4]Q\d{2}", c)]) == 3:
                header_cells = cells
                break
        if not header_cells:
            continue
        positions = [(i, f"20{c[2:]}-Q{c[0]}", c) for i, c in enumerate(header_cells) if re.fullmatch(r"[1-4]Q\d{2}", c)]
        if _v(doc, "period") not in ("", "unknown", positions[0][1]):
            continue
        for row_number, row in enumerate(rows, 1):
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["td", "th"], recursive=False)]
            if not cells or cells[0] not in _PFSI_ROWS or len(cells) != len(header_cells):
                continue
            label = cells[0]
            metric, unit, scope, measure, definition = _PFSI_ROWS[label]
            excerpt = "Servicing Segment Highlights; " + table_text[:table_text.index("Servicing portfolio")] + " | " + " | ".join(c for c in cells if c)
            for i, period, source_header in positions:
                value_text = cells[i]
                if value_text.startswith("(") and not value_text.endswith(")") and i + 1 < len(cells) and ")" in cells[i + 1]:
                    value_text += ")"
                if not re.fullmatch(r"\(?-?\d[\d,]*(?:\.\d+)?\)?|[—–-]", value_text):
                    continue
                item = _fact(doc, "PFSI", metric, value_text, unit, period, scope, definition,
                             f"HTML table {table_number}, Servicing Segment Highlights, row {row_number} '{label}', column {source_header}", excerpt, measure,
                             "Issuer marks the profitability presentation non-GAAP; refer to the full release's reconciliation and footnotes." if measure == "flow" else "")
                if item:
                    result.append(item)
    return result


def extract_issuer_tables(document):
    """Return recognized facts from the actual archive; no metadata fact injection."""
    raw_path = _v(document, "path", "")
    path = Path(str(raw_path)) if raw_path else None
    _verify_archive(document, path if path and path.exists() and path.is_file() else None)
    if path is None:
        return []
    cik = str(_v(document, "cik")).lstrip("0")
    if cik == "92230" and path.suffix.lower() == ".pdf":
        with pymupdf.open(path) as pdf:
            return [fact for i, page in enumerate(pdf) for fact in extract_tfc_table_text(document, page.get_text(sort=True), i + 1)]
    if cik == "1745916" and path.suffix.lower() in {".html", ".htm"}:
        return extract_pfsi_html(document, path.read_bytes())
    return []
