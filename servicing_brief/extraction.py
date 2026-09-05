"""Conservative extraction of servicing facts from archived source documents.

The extractor is intentionally pattern based.  It only emits a fact when the
source line contains an explicit servicing, MSR, or servicing portfolio label and
an unambiguous source value.  This keeps consolidated bank figures and mortgage
production metrics out of the servicing brief.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
import html as html_lib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from bs4 import BeautifulSoup

from .evidence import (
    Evidence,
    currency_for_unit,
    decimal_string,
    evidence_id,
    normalise_unit,
    parse_source_number,
    scope_for,
)


@dataclass(frozen=True)
class SourceSpan:
    text: str
    location: str
    index: int = 0


@dataclass(frozen=True)
class Commentary:
    id: str
    document_id: str
    issuer: str
    ticker: str
    period: str
    text: str
    location: str
    source_url: str
    source_title: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "issuer": self.issuer,
            "ticker": self.ticker,
            "period": self.period,
            "text": self.text,
            "location": self.location,
            "source_url": self.source_url,
            "source_title": self.source_title,
        }


@dataclass(frozen=True)
class TranscriptPassage:
    """A source-only, servicing-relevant passage from an earnings call.

    Transcript passages are deliberately separate from :class:`Evidence`.
    Numeric text remains an exact source excerpt unless a future caller supplies
    an independently validated financial fact; this helper never turns a call
    transcript into a broad numeric table.
    """

    id: str
    document_id: str
    issuer: str
    ticker: str
    period: str
    text: str
    location: str
    source_url: str
    source_title: str = ""
    speaker: str = ""
    speaker_role: str = "unknown"  # management or analyst
    context: str = "unknown"  # prepared_remarks, management_answer, analyst_question
    statement_type: str = "other"  # reported_performance, outlook, or other
    numeric_status: str = "none"  # none or source_excerpt_only
    numeric_evidence_ids: tuple[str, ...] = ()
    timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "document_id": self.document_id,
            "issuer": self.issuer,
            "ticker": self.ticker,
            "period": self.period,
            "text": self.text,
            "location": self.location,
            "source_url": self.source_url,
            "source_title": self.source_title,
            "speaker": self.speaker,
            "speaker_role": self.speaker_role,
            "context": self.context,
            "statement_type": self.statement_type,
            "numeric_status": self.numeric_status,
            "numeric_evidence_ids": list(self.numeric_evidence_ids),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class MetricSpec:
    metric: str
    pattern: re.Pattern[str]
    expected: str
    definition: str


def _rx(value: str) -> re.Pattern[str]:
    return re.compile(value, re.I)


# Longer and narrower patterns should be considered before generic servicing
# labels.  The report uses these names as stable keys across issuers.
METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("servicing_for_others_upb", _rx(r"servicing\s+for\s+(?:others|third\s+part(?:y|ies))"), "money", "unpaid principal balance serviced for others"),
    MetricSpec("servicing_for_others_upb", _rx(r"loans\s+serviced\s+for\s+others"), "money", "unpaid principal balance of loans serviced for others"),
    MetricSpec("owned_servicing_upb", _rx(r"(?:bank[- ]owned|owned)\s+loans\s+serviced"), "money", "unpaid principal balance of bank-owned loans serviced"),
    MetricSpec("total_servicing_portfolio_upb", _rx(r"total\s+servicing\s+portfolio"), "money", "total servicing portfolio unpaid principal balance"),
    MetricSpec("subservicing_portfolio", _rx(r"subservic(?:ing|ed|er|ers?)[^\n|;:]{0,35}(?:portfolio|UPB|unpaid\s+principal|loans?|accounts?)|(?:portfolio|UPB|unpaid\s+principal|loans?|accounts?)[^\n|;:]{0,35}subservic(?:ing|ed|er|ers?)"), "portfolio", "subservicing portfolio or population"),
    MetricSpec("msr_carrying_value", _rx(r"(?:MSR|mortgage\s+servicing\s+rights)[^\n|;:]{0,45}(?:carrying|book)\s+value|(?:carrying|book)\s+value[^\n|;:]{0,45}(?:MSR|mortgage\s+servicing\s+rights)"), "money", "carrying or book value of mortgage servicing rights"),
    MetricSpec("owned_msr_portfolio", _rx(r"owned[^\n|;:]{0,30}(?:MSR|mortgage\s+servicing\s+rights)|(?:MSR|mortgage\s+servicing\s+rights)[^\n|;:]{0,30}owned|MSR[^\n|;:]{0,25}portfolio"), "portfolio", "owned mortgage servicing rights portfolio"),
    MetricSpec("msr_fair_value_change", _rx(r"(?:MSR|mortgage\s+servicing\s+rights)[^\n|;:]{0,45}fair\s+value[^\n|;:]{0,25}(?:change|gain|loss)|fair\s+value[^\n|;:]{0,45}(?:MSR|mortgage\s+servicing\s+rights)[^\n|;:]{0,25}(?:change|gain|loss)|net\s+MSR[s]?\s+valuation"), "money", "change in mortgage servicing rights fair value or net valuation"),
    MetricSpec("msr_amortization", _rx(r"(?:MSR|mortgage\s+servicing\s+rights)[^\n|;:]{0,45}amort(?:ization|isation)|amort(?:ization|isation)[^\n|;:]{0,45}(?:MSR|mortgage\s+servicing\s+rights)"), "money", "mortgage servicing rights amortization"),
    MetricSpec("msr_hedge_result", _rx(r"(?:MSR|mortgage\s+servicing\s+rights|servicing)[^\n|;:]{0,45}(?:hedg(?:e|ing)|hedge\s+result)|(?:hedg(?:e|ing)|hedge\s+result)[^\n|;:]{0,45}(?:MSR|mortgage\s+servicing\s+rights|servicing)"), "money", "hedge result associated with mortgage servicing rights or servicing"),
    MetricSpec("servicing_fee_income", _rx(r"(?:mortgage\s+)?servicing\s+(?:fee\s+)?(?:income|revenue)|fee\s+income[^\n|;:]{0,25}servicing"), "money", "servicing fee income or servicing revenue"),
    MetricSpec("servicing_operating_expense", _rx(r"(?:mortgage\s+)?servicing\s+(?:operating\s+)?expenses?|expenses?[^\n|;:]{0,30}servicing"), "money", "operating expense attributable to servicing"),
    MetricSpec("servicing_pretax_income", _rx(r"(?:servicing|mortgage\s+servicing)[^\n|;:]{0,35}(?:pre[- ]?tax|pretax)\s+(?:income|profit)|(?:pre[- ]?tax|pretax)\s+(?:income|profit)[^\n|;:]{0,35}servicing"), "money", "pretax income or profit attributable to servicing"),
    MetricSpec("adjusted_servicing_result", _rx(r"adjusted\s+(?:mortgage\s+)?servicing\s+(?:income|profit|result|earnings|pretax)"), "money", "issuer-reported adjusted servicing result"),
    MetricSpec("servicing_cost_per_loan", _rx(r"(?:servicing|mortgage\s+servicing)[^\n|;:]{0,30}cost\s+per\s+(?:loan|account)|cost\s+per\s+(?:loan|account)[^\n|;:]{0,30}servicing"), "per_loan", "reported servicing cost per loan or account"),
    MetricSpec("servicing_upb", _rx(r"(?:mortgage\s+)?servicing[^\n|;:]{0,35}(?:UPB|unpaid\s+principal|principal\s+balance)|(?:UPB|unpaid\s+principal|principal\s+balance)[^\n|;:]{0,35}servicing"), "money", "servicing portfolio unpaid principal balance"),
    MetricSpec("loans_serviced", _rx(r"(?:loans?|accounts?)\s+(?:being\s+)?serviced|serviced\s+(?:loans?|accounts?)|(?:mortgage\s+)?servicing\s+(?:portfolio|population)\s*(?:count|loans?|accounts?)"), "count", "number of loans or accounts serviced"),
    MetricSpec("servicing_advances", _rx(r"(?:servicing|MSR|mortgage\s+servicing)[^\n|;:]{0,25}advances?(?:\s+receivable|\s+funding)?|advance\s+receivables?[^\n|;:]{0,25}(?:servicing|MSR)"), "money", "servicing advances or advances receivable"),
    MetricSpec("servicing_liquidity", _rx(r"(?:servicing|mortgage\s+servicing)[^\n|;:]{0,35}liquidity|liquidity[^\n|;:]{0,35}(?:servicing|MSR)"), "money", "liquidity specifically identified for servicing or MSR"),
    MetricSpec("servicing_delinquency_rate", _rx(r"(?:servicing|serviced|mortgage\s+servicing)[^\n|;:]{0,35}(?:delinquen(?:cy|t)|30\s*\+\s*days|90\s*\+\s*days|default)"), "rate", "delinquency or default rate in a servicing population"),
    MetricSpec("servicing_fee_rate", _rx(r"(?:weighted[- ]average\s+)?servicing\s+fee[^\n|;:]{0,35}(?:mortgage|loans?|serviced|others)"), "rate", "weighted-average servicing fee rate for the stated population"),
    MetricSpec("serviced_loans_coupon_rate", _rx(r"(?:weighted[- ]average\s+)?coupon\s+rate[^\n|;:]{0,35}(?:mortgage|loans?|serviced|others)"), "rate", "weighted-average coupon rate for the stated serviced population"),
    MetricSpec("servicing_foreclosure_count", _rx(r"(?:servicing|serviced|mortgage\s+servicing)[^\n|;:]{0,35}foreclosure"), "count", "foreclosure activity in a servicing population"),
    MetricSpec("servicing_forbearance_count", _rx(r"(?:servicing|serviced|mortgage\s+servicing)[^\n|;:]{0,35}forbearance"), "count", "forbearance activity in a servicing population"),
)


_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:\(\s*[+$€£]?\s*[0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?\s*\)|"
    r"[+\-\u2010\u2011\u2012\u2013\u2014\u2212]?\s*[+$€£]?\s*[0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)"
    r"\s*(?:%|bps?|basis\s+points?|million|billion|thousand|mm|bn)?",
    re.I,
)
_PERIOD_Q_RE = re.compile(r"\b(?:Q|quarter\s*)?\s*([1-4])\s*(?:Q|quarter)?[\s,/-]*(20\d{2})\b|\bQ([1-4])\s*['’]?(\d{2})\b|\b(first|second|third|fourth)\s+quarter\s+(20\d{2})\b", re.I)
_YEAR_Q_RE = re.compile(r"\b(20\d{2})\s*[-/]?\s*Q([1-4])\b", re.I)
_DATE_RE = re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+([0-9]{1,2}),?\s+(20\d{2})\b", re.I)
_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}


def _doc_value(document: Any, key: str, default: Any = "") -> Any:
    if isinstance(document, Mapping):
        return document.get(key, default)
    return getattr(document, key, default)


def _metadata(document: Any) -> dict[str, Any]:
    value = _doc_value(document, "metadata", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _ticker_for(document: Any, config: Mapping[str, Any] | None) -> str:
    metadata = _metadata(document)
    for key in ("ticker", "symbol"):
        if metadata.get(key):
            return str(metadata[key]).upper()
    if config:
        cik = str(_doc_value(document, "cik", "")).lstrip("0") or "0"
        issuer = str(_doc_value(document, "issuer", "")).lower()
        for company in config.get("companies", []) or []:
            if not isinstance(company, Mapping):
                continue
            ccik = str(company.get("cik", "")).lstrip("0") or "0"
            if (cik and ccik == cik) or (company.get("name") and str(company["name"]).lower() in issuer):
                return str(company.get("ticker", "")).upper()
    return str(_doc_value(document, "ticker", "")).upper()


def _text_from_metadata(document: Any) -> str:
    metadata = _metadata(document)
    for key in ("text", "extracted_text", "content", "body"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


_TRANSCRIPT_KINDS = {
    "transcript",
    "prepared_remarks",
    "prepared_remark",
    "earnings_call_transcript",
    "earnings_transcript",
    "call_transcript",
}


def is_transcript_document(document: Any) -> bool:
    """Return true only for a source explicitly classified as transcript text."""

    kind = re.sub(r"[\s-]+", "_", str(_doc_value(document, "kind", "")).strip().lower())
    return kind in _TRANSCRIPT_KINDS


def _path_for(document: Any) -> Path | None:
    raw = _doc_value(document, "path", "")
    if not raw:
        return None
    try:
        path = Path(str(raw))
    except (TypeError, ValueError):
        return None
    return path if path.exists() and path.is_file() else None


def generic_extraction_enabled(document: Any, config: Mapping[str, Any] | None = None) -> bool:
    """Whether the conservative pattern matcher may run for this source.

    Production archives default to verified issuer-table rules only.  A broad
    row matcher can mistake an adjacent coupon, footnote, or date for a UPB or
    dollar amount in real SEC HTML/PDF layouts.  Generic extraction is available
    only through the explicit experimental setting or the named helper below.
    """

    # A production report must opt in through one explicit, namespaced setting.
    # Metadata, missing paths, filenames, and URL patterns are all untrusted
    # source attributes and therefore cannot turn the experimental matcher on.
    settings = config.get("extraction", {}) if isinstance(config, Mapping) else {}
    if not isinstance(settings, Mapping):
        return False
    value = settings.get("experimental_generic_numeric", False)
    return bool(value) and str(value).lower() not in {"0", "false", "no", "off"}


def numeric_extraction_status(document: Any, config: Mapping[str, Any] | None = None) -> str:
    """Return ``verified``, ``generic-test``, or ``unverified`` for reporting."""

    try:
        from .issuer_tables import extract_issuer_tables  # type: ignore
    except ImportError:
        extract_issuer_tables = None
    if extract_issuer_tables is not None:
        try:
            if extract_issuer_tables(document):
                return "verified"
        except Exception:
            pass
    return "generic-test" if generic_extraction_enabled(document, config) else "unverified"


def extract_generic_candidates(document: Any, *, config: Mapping[str, Any] | None = None) -> list[Evidence]:
    """Run the experimental row matcher explicitly for fixture/unit testing.

    The normal ``extract_financial_facts`` entry point remains fail-closed for
    unrecognized production layouts.  This helper is intentionally named so a
    caller cannot mistake experimental candidates for verified report facts.
    """

    merged = dict(config or {})
    extraction = dict(merged.get("extraction", {}) or {}) if isinstance(merged.get("extraction", {}), Mapping) else {}
    extraction["experimental_generic_numeric"] = True
    merged["extraction"] = extraction
    return extract_financial_facts(document, config=merged)


def read_document_spans(document: Any) -> list[SourceSpan]:
    """Read an archived document into source lines with stable locations.

    PDF pages are extracted through PyMuPDF.  HTML tables receive table/row
    locations so a financial value can be reviewed without relying on a fragile
    character offset.
    """

    path = _path_for(document)
    metadata = _metadata(document)
    mime = str(_doc_value(document, "mime_type", "")).lower()
    suffix = path.suffix.lower() if path else ""
    if path and (suffix == ".pdf" or "pdf" in mime):
        try:
            import pymupdf as fitz  # PyMuPDF, the single PDF parser used by this project.
        except ImportError as exc:  # pragma: no cover - environment contract
            raise RuntimeError("PyMuPDF is required to extract PDF evidence") from exc
        spans: list[SourceSpan] = []
        with fitz.open(str(path)) as pdf:
            for page_no, page in enumerate(pdf, start=1):
                text = page.get_text("text") or ""
                for line_no, line in enumerate(text.splitlines(), start=1):
                    clean = " ".join(line.split())
                    if clean:
                        spans.append(SourceSpan(clean, f"PDF page {page_no}, line {line_no}", len(spans)))
        return spans
    if path and (suffix in {".html", ".htm", ".xhtml"} or "html" in mime):
        raw = path.read_bytes()
        return _html_spans(raw, str(_doc_value(document, "url", "")))
    if path:
        raw = path.read_bytes()
        # Source archives can retain issuer-provided HTML without a useful mime
        # type.  Detect it before falling back to plain text.
        if b"<html" in raw[:1000].lower() or b"<table" in raw[:1000].lower():
            return _html_spans(raw, str(_doc_value(document, "url", "")))
        text = raw.decode("utf-8", errors="replace")
    else:
        text = _text_from_metadata(document)
    spans = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        clean = " ".join(line.split())
        if clean:
            spans.append(SourceSpan(clean, f"text line {line_no}", len(spans)))
    return spans


def _html_spans(raw: bytes, source_url: str = "") -> list[SourceSpan]:
    soup = BeautifulSoup(raw, "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    spans: list[SourceSpan] = []
    tables = soup.find_all("table")
    table_cells: set[int] = set()
    for table_no, table in enumerate(tables, start=1):
        rows = table.find_all("tr")
        for row_no, row in enumerate(rows, start=1):
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all(["th", "td"])]
            if not cells:
                continue
            text = " | ".join(cells)
            spans.append(SourceSpan(text, f"HTML table {table_no}, row {row_no}", len(spans)))
            for cell in row.find_all(["th", "td"]):
                table_cells.add(id(cell))
    # Add visible non-table text, excluding table descendants to avoid emitting
    # duplicate cell lines.  Removing the tables from a second parse is more
    # reliable than comparing flattened text: a row such as ``label | $1`` is
    # not equal to either of its individual cell strings.
    visible_soup = BeautifulSoup(raw, "html.parser")
    for element in visible_soup(["script", "style", "noscript", "template", "table"]):
        element.decompose()
    # Preserve each paragraph as one source span.  ``get_text("\n")`` splits
    # literal line breaks inside a paragraph into unrelated fragments, which
    # loses the qualifier in management explanations (for example, the reason
    # revenue changed).  Block elements retain the issuer's sentence boundary
    # while still giving us deterministic text locations.
    block_tags = ("p", "li", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre")
    for element in visible_soup.find_all(block_tags):
        if element.find_parent(block_tags):
            continue
        clean = " ".join(element.get_text(" ", strip=True).split())
        if clean:
            spans.append(SourceSpan(clean, f"HTML text line {len(spans) + 1}", len(spans)))
    # A few issuer HTML archives use bare div/section blocks.  Include those
    # only when they do not contain one of the paragraph blocks above, avoiding
    # duplicate ancestor text.
    for element in visible_soup.find_all(("div", "section", "article")):
        if element.find(block_tags):
            continue
        clean = " ".join(element.get_text(" ", strip=True).split())
        if clean:
            spans.append(SourceSpan(clean, f"HTML text line {len(spans) + 1}", len(spans)))
    return spans


def _normalise_period(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"unknown", "n/a", "na"}:
        return "unknown"
    text = text.upper().replace(" ", "-").replace("/", "-")
    text = re.sub(r"--+", "-", text)
    if re.fullmatch(r"20\d{2}-Q[1-4]", text):
        return text
    if re.fullmatch(r"Q[1-4]-20\d{2}", text):
        q, year = text.split("-")
        return f"{year}-{q}"
    return str(value)


def _period_from_text(text: str) -> list[str]:
    periods: list[str] = []
    for match in _YEAR_Q_RE.finditer(text):
        periods.append(f"{match.group(1)}-Q{match.group(2)}")
    for match in _PERIOD_Q_RE.finditer(text):
        if match.group(2):
            periods.append(f"{match.group(2)}-Q{match.group(1)}")
        elif match.group(3) and match.group(4):
            periods.append(f"20{match.group(4)}-Q{match.group(3)}")
        elif match.group(5) and match.group(6):
            quarter = {"first": 1, "second": 2, "third": 3, "fourth": 4}[match.group(5).lower()]
            periods.append(f"{match.group(6)}-Q{quarter}")
    for match in _DATE_RE.finditer(text):
        month_match = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)", match.group(0), re.I)
        if not month_match:
            continue
        month = _MONTHS[month_match.group(1).lower()]
        quarter = (month - 1) // 3 + 1
        periods.append(f"{match.group(2)}-Q{quarter}")
    # Preserve order and source appearance, not set order.
    return list(dict.fromkeys(periods))


def document_period(document: Any, spans: Sequence[SourceSpan] = ()) -> str:
    declared = _normalise_period(_doc_value(document, "period", "unknown"))
    if declared != "unknown":
        return declared
    metadata = _metadata(document)
    for key in ("period", "reporting_period", "fiscal_period"):
        declared = _normalise_period(metadata.get(key, "unknown"))
        if declared != "unknown":
            return declared
    # Only use a period explicitly written in source text.  Filing or retrieval
    # timestamps are never used for this purpose.
    text = " ".join(span.text for span in spans[:80])
    periods = _period_from_text(text)
    return periods[0] if periods else "unknown"


def _period_hints(document: Any, context: str) -> list[str]:
    metadata = _metadata(document)
    hints: list[str] = []
    for key in ("column_periods", "period_columns", "table_periods", "comparison_periods"):
        values = metadata.get(key)
        if isinstance(values, (list, tuple)):
            hints.extend(_normalise_period(v) for v in values if _normalise_period(v) != "unknown")
    hints.extend(_period_from_text(context))
    return list(dict.fromkeys(hints))


def _source_values(text: str) -> list[tuple[str, int, int]]:
    values: list[tuple[str, int, int]] = []
    for match in _NUMBER_RE.finditer(text):
        raw = " ".join(match.group(0).split())
        # Dates and years are not financial values.  Keep a year only when the
        # token is visibly money/rate/parenthesized, or when a metric's context
        # later identifies it as a count.
        stripped = re.sub(r"[^0-9.]", "", raw)
        if re.fullmatch(r"(?:19|20)\d{2}", stripped) and not any(ch in raw for ch in "$%()"):
            continue
        values.append((raw, match.start(), match.end()))
    return values


def _expected_unit(spec: MetricSpec, raw: str, line: str, context: str = "") -> str:
    # Prefer the row itself.  A broad context window may mention loans or rates
    # in an adjacent row and must not change the unit of an MSR dollar value.
    local = f"{raw} {line}".lower()
    lower = local
    if spec.expected == "rate":
        if "bps" in lower or "basis point" in lower:
            return "basis_points"
        return "percent"
    if spec.expected == "count":
        unit = normalise_unit("count", lower)
        if "million" in lower or re.search(r"\bmm\b", lower):
            return "loans_millions"
        if "thousand" in lower or re.search(r"\b000s?\b", lower):
            return "loans_thousands"
        return unit if unit in {"loans", "count"} else "count"
    if spec.expected == "per_loan":
        return "USD_per_loan"
    if spec.expected == "portfolio":
        lower_label = lower
        if "loan" in lower_label or "account" in lower_label:
            if "million" in lower_label or re.search(r"\bmm\b", lower_label):
                return "loans_millions"
            if "thousand" in lower_label:
                return "loans_thousands"
            return "loans"
        return normalise_unit("USD", lower)
    unit = normalise_unit("USD", lower)
    if spec.expected == "money" and re.search(r"\b(?:UPB|unpaid\s+principal|principal\s+balance)\b", context, re.I):
        # The page may also contain percentage rows; a broad normalizer would
        # see '%' first.  UPB rows inherit only the explicit statement scale.
        if re.search(r"\b(?:million|mm)\b|dollars?\s+in\s+millions", context, re.I):
            unit = "USD_millions"
        elif re.search(r"\b(?:billion|bn)\b|dollars?\s+in\s+billions", context, re.I):
            unit = "USD_billions"
        elif re.search(r"\b(?:thousand|000s?)\b|dollars?\s+in\s+thousands", context, re.I):
            unit = "USD_thousands"
        else:
            unit = "USD"
    # Plain table numbers inherit a scale from a nearby units header, but only
    # from an explicit units phrase.  Do not let unrelated neighbouring rows
    # supply a scale or business population.
    if unit in {"USD", "unknown"} and context:
        header = " ".join(re.findall(r"[^\n|]{0,80}(?:\$|USD|in\s+)(?:\s*in)?\s*(?:millions?|billions?|thousands?|000s?|mm|bn)[^\n|]{0,30}", context, re.I))
        if header:
            inherited = normalise_unit("USD", header)
            if inherited.startswith("USD_"):
                return inherited
    return unit


def _measure_type(metric: str, unit: str) -> str:
    if unit in {"percent", "basis_points"} or "rate" in metric:
        return "rate"
    if unit in {"loans", "loans_millions", "loans_thousands", "count"} or metric.endswith("_count") or metric == "loans_serviced":
        return "count"
    if metric in {"msr_carrying_value", "owned_msr_portfolio", "subservicing_portfolio", "servicing_upb", "servicing_for_others_upb", "owned_servicing_upb", "total_servicing_portfolio_upb", "servicing_advances", "servicing_liquidity"}:
        return "stock"
    if metric in {"servicing_fee_income", "servicing_operating_expense", "servicing_pretax_income", "adjusted_servicing_result", "msr_amortization", "msr_fair_value_change", "msr_hedge_result"}:
        return "flow"
    return "unknown"


def _candidate_values(spec: MetricSpec, line: str, match: re.Match[str], context: str) -> list[tuple[str, str]]:
    after = line[match.end():]
    values = _source_values(after)
    if not values:
        values = _source_values(line)
    if not values:
        return []
    result: list[tuple[str, str]] = []
    for raw, _start, _end in values:
        unit = _expected_unit(spec, raw, line, context)
        low = f"{raw} {context}".lower()
        explicit_rate = "%" in raw or "bps" in raw.lower() or "basis point" in low
        explicit_money = any(marker in low for marker in ("$", "million", "billion", "thousand", "mm", "bn", "dollar"))
        if spec.expected == "rate":
            if not explicit_rate and not re.search(r"rate|percent|percentage|delinquen|default", context, re.I):
                continue
        elif spec.expected in {"money", "per_loan"}:
            if "%" in raw or "bps" in raw.lower():
                continue
            # Financial tables often put ($ in millions) in a prior header.  A
            # plain number is accepted when the metric itself is monetary.
        elif spec.expected in {"count", "portfolio"}:
            if "%" in raw or "bps" in raw.lower():
                continue
        result.append((raw, unit))
    # Body prose such as "increased 5% to $120" can have multiple numbers.  A
    # monetary metric should use the final monetary amount; table rows retain
    # all values because the column periods are evidence-bearing.
    if "|" not in line and len(result) > 1:
        if spec.expected == "rate":
            result = result[-1:]
        elif spec.expected in {"money", "per_loan"}:
            result = result[-1:]
    return result


def _is_duplicate(items: Sequence[Evidence], metric: str, raw_value: str, location: str) -> bool:
    return any(item.metric == metric and item.raw_value == raw_value and item.location == location for item in items)


def _page_key(location: str) -> str:
    match = re.search(r"PDF page\s+(\d+)", str(location))
    return match.group(1) if match else ""


def _page_period_hints(spans: Sequence[SourceSpan]) -> dict[str, list[str]]:
    """Read explicit month/year table headers emitted as separate PDF lines."""

    result: dict[str, list[str]] = {}
    grouped: dict[str, list[SourceSpan]] = {}
    for span in spans:
        key = _page_key(span.location)
        if key:
            grouped.setdefault(key, []).append(span)
    for page, page_spans in grouped.items():
        # The SEC/issuer table convention is a date row followed by a year row.
        # Require an explicit table heading and pair only the first five dates and
        # years following it; this avoids using publication dates as periods.
        heading_index = next((i for i, item in enumerate(page_spans) if re.search(r"As of/For the Quarter Ended", item.text, re.I)), None)
        if heading_index is None:
            continue
        window = page_spans[max(0, heading_index - 20): heading_index + 25]
        months: list[int] = []
        years: list[int] = []
        for item in window:
            month_match = re.fullmatch(r"(?:January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|July|Jul|August|Aug|September|Sept?|October|Oct|November|Nov|December|Dec)\.?\s+\d{1,2}\.?", item.text, re.I)
            if month_match:
                month_name = re.match(r"[A-Za-z]+", item.text).group(0).lower()
                months.append(_MONTHS[month_name])
                continue
            year_match = re.fullmatch(r"20\d{2}", item.text.strip())
            if year_match:
                years.append(int(year_match.group(0)))
        if len(months) >= 2 and len(years) >= len(months):
            result[page] = [f"{years[i]}-Q{(months[i] - 1) // 3 + 1}" for i in range(len(months))]
    return result


def _explicit_facts(document: Any, config: Mapping[str, Any] | None, period: str, spans: Sequence[SourceSpan] = ()) -> list[Evidence]:
    metadata = _metadata(document)
    raw_facts = metadata.get("facts", metadata.get("financial_facts", []))
    if not isinstance(raw_facts, Sequence) or isinstance(raw_facts, (str, bytes)):
        return []
    result: list[Evidence] = []
    ticker = _ticker_for(document, config)
    for fact in raw_facts:
        if not isinstance(fact, Mapping):
            continue
        metric = str(fact.get("metric", "")).strip()
        raw_value = str(fact.get("raw_value", fact.get("value", ""))).strip()
        if not metric or not raw_value:
            continue
        parsed, sign = parse_source_number(raw_value)
        if parsed is None:
            continue
        fact_period = _normalise_period(fact.get("period", period))
        unit = normalise_unit(fact.get("unit"), f"{metric} {fact.get('definition', '')}")
        if fact.get("unit"):
            # Keep a caller's explicit normalized vocabulary when valid.
            explicit_unit = str(fact["unit"])
            if explicit_unit in {"USD", "USD_millions", "USD_billions", "USD_thousands", "USD_per_loan", "percent", "basis_points", "loans", "loans_millions", "loans_thousands", "count"}:
                unit = explicit_unit
        scope = str(fact.get("scope", "")).strip()
        if not scope:
            scope, _definition, supported = scope_for(metric, str(fact.get("definition", "")), metric)
        else:
            supported = scope in {"servicing", "servicing_for_others", "servicing_owned", "servicing_owned_msr", "servicing_subservicing"}
        if not supported:
            continue
        definition = str(fact.get("definition", "")).strip() or metric.replace("_", " ")
        location = str(fact.get("location", metadata.get("location", ""))).strip()
        excerpt = str(fact.get("excerpt", "")).strip()
        # Caller-provided facts are accepted only when they carry a source quote
        # and location.  If archived bytes are available, the quote/value must be
        # present in those bytes as well; this prevents metadata from becoming a
        # way to inject unsupported numeric facts into a report.
        if not location or not excerpt or not raw_value:
            continue
        source_text = " ".join(span.text for span in spans)
        if spans:
            compact_source = re.sub(r"\s+", "", source_text).lower()
            compact_excerpt = re.sub(r"\s+", "", excerpt).lower()
            compact_raw = re.sub(r"\s+", "", raw_value).lower()
            # Both the quote and the reported token must be grounded in the
            # archived bytes.  Accepting a valid number somewhere else in the
            # file would let metadata attach an invented definition/location
            # to an unrelated source row.
            if compact_excerpt not in compact_source or compact_raw not in compact_excerpt:
                continue
        elif re.sub(r"\s+", "", raw_value).lower() not in re.sub(r"\s+", "", excerpt).lower():
            continue
        result.append(Evidence(
            id=str(fact.get("id") or evidence_id(str(_doc_value(document, "id", "")), metric, raw_value, location, fact_period)),
            document_id=str(_doc_value(document, "id", "")), issuer=str(_doc_value(document, "issuer", "")), ticker=ticker,
            metric=metric, value=decimal_string(parsed), raw_value=raw_value, unit=unit, currency=currency_for_unit(unit),
            period=fact_period, scope=scope, definition=definition, location=location, excerpt=excerpt,
            source_url=str(_doc_value(document, "url", "")), source_kind=str(_doc_value(document, "source", "")),
            source_title=str(_doc_value(document, "title", "")), document_kind=str(_doc_value(document, "kind", "")),
            published=str(_doc_value(document, "published", "")), sign=sign,
            measure_type=str(fact.get("measure_type", _measure_type(metric, unit))),
            period_start=str(fact.get("period_start", "")), period_end=str(fact.get("period_end", "")),
        ))
    return result


def extract_financial_facts(document: Any, *, config: Mapping[str, Any] | None = None) -> list[Evidence]:
    """Extract servicing-only numerical evidence from one archived document."""

    # Transcript and prepared-remarks text is handled by
    # ``extract_transcript_passages``.  Even when a test explicitly enables the
    # broad row matcher, do not promote conversational figures to table facts;
    # the surrounding speaker, period, and accounting definition are not a
    # verified tabular layout.
    if is_transcript_document(document):
        return []
    spans = read_document_spans(document)
    period = document_period(document, spans)
    # Issuer table parsers may provide stricter row/column semantics than the
    # conservative generic matcher (for example, PDF tables whose cells are
    # emitted on separate lines).  A non-empty dedicated result is authoritative
    # for that document and prevents generic bank-wide or duplicate rows from
    # entering the report.  Unrecognized documents continue through the generic
    # matcher below.
    try:
        from .issuer_tables import extract_issuer_tables  # type: ignore
    except ImportError:
        extract_issuer_tables = None
    if extract_issuer_tables is not None:
        try:
            dedicated = extract_issuer_tables(document)
        except Exception:
            dedicated = None
        if dedicated:
            return list(dedicated)
    if not generic_extraction_enabled(document, config):
        # The archived source remains available to the report and delivery
        # layers, but no unverified number is allowed into financial evidence.
        return []
    result = _explicit_facts(document, config, period, spans)
    ticker = _ticker_for(document, config)
    doc_id = str(_doc_value(document, "id", ""))
    page_periods = _page_period_hints(spans)
    page_contexts: dict[str, str] = defaultdict(str)
    for page_span in spans:
        key = _page_key(page_span.location)
        if key:
            page_contexts[key] += " " + page_span.text
    context_window: list[str] = []
    for span_index, span in enumerate(spans):
        line = span.text
        context_window.append(line)
        page_context = page_contexts.get(_page_key(span.location), "")
        context = " ".join(context_window[-30:] + [line, page_context])
        # Header unit context can be outside the table row.  Keep a generous
        # window while limiting text passed to regexes.
        context = context[-2500:]
        for spec in METRICS:
            match = spec.pattern.search(line)
            if not match:
                continue
            lower_line = line.lower()
            # TFC's portfolio table labels UPB rows as "loans serviced" and
            # carries the UPB definition in a footnote.  Keep dedicated rows
            # from being duplicated as loan counts or rate rows.
            if spec.metric == "loans_serviced" and ("loans serviced for others" in lower_line or "bank-owned loans serviced" in lower_line or "weighted-average" in lower_line):
                continue
            if spec.metric == "servicing_for_others_upb" and ("weighted-average" in lower_line or "servicing fee" in lower_line or "coupon" in lower_line) and not re.search(r"\b(?:UPB|unpaid\s+principal)\b", lower_line, re.I):
                continue
            # Scope must come from the metric row itself.  A prior line or a
            # document-wide heading mentioning servicing cannot turn a bank-wide
            # result into servicing evidence.
            scope, scope_definition, supported = scope_for(line, "", spec.metric)
            if not supported:
                continue
            candidates = _candidate_values(spec, line, match, context)
            excerpt_line = line
            # PDF table extraction commonly places a metric label and its values
            # on adjacent text lines.  Look ahead only until the first numeric
            # row is found; the metric's own label still controls scope.
            if not candidates:
                lookahead: list[str] = []
                for following in spans[span_index + 1: span_index + 14]:
                    # A new alphabetic row label marks the end of a fragmented
                    # PDF table row.  Unit markers (NM, bps) are allowed to pass.
                    is_row_label = bool(re.search(r"[A-Za-z]", following.text)) and following.text.strip().lower() not in {"nm", "bp", "bps", "basis points"}
                    if spec.pattern.search(following.text) or is_row_label:
                        break
                    lookahead.append(following.text)
                if lookahead:
                    joined = " | ".join([line, *lookahead])
                    candidate_match = spec.pattern.search(joined)
                    if candidate_match:
                        candidates = _candidate_values(spec, joined, candidate_match, context)
                        if candidates:
                            excerpt_line = joined
            if not candidates:
                continue
            hints = list(dict.fromkeys(page_periods.get(_page_key(span.location), []) + _period_hints(document, context)))
            # The declared document period wins for the current value.  Explicit
            # table periods are used only when present; otherwise extra columns
            # stay unknown and cannot accidentally drive a comparison.
            current_period = period
            if hints and current_period == "unknown":
                current_period = hints[0]
            for value_index, (raw_value, unit) in enumerate(candidates):
                value, sign = parse_source_number(raw_value)
                if value is None:
                    continue
                fact_period = current_period
                if value_index < len(hints):
                    # If the document's declared period is present in hints, use
                    # its position.  Otherwise source order remains current-first.
                    if current_period in hints:
                        fact_period = hints[value_index]
                    elif value_index == 0:
                        fact_period = current_period
                    else:
                        fact_period = hints[value_index]
                elif value_index > 0:
                    prior = _metadata(document).get("prior_period")
                    fact_period = _normalise_period(prior) if prior else "unknown"
                if _is_duplicate(result, spec.metric, raw_value, span.location):
                    continue
                result.append(Evidence(
                    id=evidence_id(doc_id, spec.metric, raw_value, span.location, fact_period),
                    document_id=doc_id,
                    issuer=str(_doc_value(document, "issuer", "")),
                    ticker=ticker,
                    metric=spec.metric,
                    value=decimal_string(value),
                    raw_value=raw_value,
                    unit=unit,
                    currency=currency_for_unit(unit),
                    period=fact_period,
                    scope=scope,
                    definition=spec.definition if scope_definition == "" else f"{spec.definition}; scope: {scope_definition}",
                    location=span.location,
                    excerpt=excerpt_line[:1200],
                    source_url=str(_doc_value(document, "url", "")),
                    source_kind=str(_doc_value(document, "source", "")),
                    source_title=str(_doc_value(document, "title", "")),
                    document_kind=str(_doc_value(document, "kind", "")),
                    published=str(_doc_value(document, "published", "")),
                    sign=sign,
                    measure_type=_measure_type(spec.metric, unit),
                    notes="Additional table column has unknown period" if fact_period == "unknown" else "",
                ))
    return result


def extract_document_facts(document: Any, *, config: Mapping[str, Any] | None = None) -> list[Evidence]:
    return extract_financial_facts(document, config=config)


def extract_facts(documents: Iterable[Any], *, config: Mapping[str, Any] | None = None) -> list[Evidence]:
    result: list[Evidence] = []
    for document in documents:
        result.extend(extract_financial_facts(document, config=config))
    return result


def _commentary_candidate(text: str) -> bool:
    # ``Services`` appears in issuer legal names and generic product prose.
    # Require the mortgage-servicing business label itself so bank-wide
    # headlines and unrelated product descriptions cannot become explanations.
    if not re.search(r"\bservicing\b|\bserviced\b|\bservicer(?:s)?\b|\bMSRs?\b|mortgage\s+servicing|subservic", text, re.I):
        return False
    # Rows from detailed reconciliation tables are source facts, not management
    # explanation.  The short narrative row in the release generally has only
    # one or two separators and remains eligible.
    if text.count("|") >= 4:
        return False
    if re.search(r"forward[- ]looking|risk factors?|ability to .*service|service providers?|may adversely|could adversely|future periods?|terrorist|cyber[- ]|pandemic|legal, regulatory|satisfactorily and profitably|\bexpected\b|\bwill\b|\bcould\b|\bmay\b|\bbelieve\b|\bplan(?:s|ned)?\b", text, re.I):
        return False
    # A metric row may contain words such as "expense" or "portfolio" without
    # being management commentary.  Require an explanatory verb, comparison,
    # or explicit causal phrase so source excerpts remain useful and reviewable.
    return bool(re.search(r"(?:due\s+to|driven\s+by|reflect(?:s|ed)|because|result(?:ed|ing)|has\s+been|was\s+|were\s+|higher|lower|increased|decreased|declined|grew|changed|impact(?:ed|s)?|amortization|hedg(?:e|ing)|transfer(?:red)?|delinquen|forbearance|default)", text, re.I))


def extract_commentary(document: Any, *, max_items: int = 8) -> list[Commentary]:
    """Return source excerpts that explain servicing changes or limitations."""

    spans = read_document_spans(document)
    period = document_period(document, spans)
    ticker = _ticker_for(document, None)
    result: list[Commentary] = []
    seen: set[str] = set()
    for span in spans:
        # Financial HTML often wraps the entire release in layout tables.  The
        # same management paragraph then appears once as a table row and once
        # as normal body text; use the body span for a stable, readable quote
        # and leave detailed table rows to Evidence extraction.
        if span.location.startswith("HTML table"):
            continue
        if not _commentary_candidate(span.text):
            continue
        # Keep the source line intact; dropping a clause can change the issuer's
        # qualification or make an explanation appear causal.
        excerpt = span.text[:1600].strip()
        key = re.sub(r"\s+", " ", excerpt).lower()
        if key in seen:
            continue
        seen.add(key)
        cid = evidence_id(str(_doc_value(document, "id", "")), "commentary", excerpt, span.location, period)
        result.append(Commentary(
            id=cid, document_id=str(_doc_value(document, "id", "")), issuer=str(_doc_value(document, "issuer", "")),
            ticker=ticker, period=period, text=excerpt, location=span.location,
            source_url=str(_doc_value(document, "url", "")), source_title=str(_doc_value(document, "title", "")),
        ))
        if len(result) >= max_items:
            break
    return result


_TRANSCRIPT_STRUCTURE_RE = re.compile(
    r"\b(?:question(?:s)?\s*[-–—]?\s*(?:and|&)\s*[-–—]?\s*answer(?:s)?|q\s*&\s*a|analyst|operator|prepared\s+remarks|"
    r"chief\s+(?:executive|financial|operating)|chief\s+\w+\s+officer|president|vice\s+president|"
    r"executive\s+vice|\b(?:ceo|cfo|coo|evp|svp)\b|head\s+of|treasurer)\b",
    re.I,
)
_TRANSCRIPT_QA_HEADING_RE = re.compile(
    r"^(?:question(?:s)?\s*[-–—]?\s*(?:and|&)\s*[-–—]?\s*answer(?:s)?(?:\s+session)?|q\s*(?:and|&)\s*a|analyst\s+q(?:uestion)?\s*&?\s*a(?:nswer)?s?)\b",
    re.I,
)
_TRANSCRIPT_REMARKS_HEADING_RE = re.compile(r"^(?:prepared|opening)\s+remarks?\b", re.I)
_TRANSCRIPT_OPERATOR_RE = re.compile(r"\b(?:operator|moderator|conference\s+coordinator)\b", re.I)
_TRANSCRIPT_ANALYST_RE = re.compile(r"\b(?:analyst|question(?:s)?|research)\b", re.I)
_TRANSCRIPT_MANAGEMENT_RE = re.compile(
    r"\b(?:chief\s+\w+\s+officer|ceo|cfo|coo|president|vice\s+president|executive\s+vice|"
    r"evp|svp|head\s+of|treasurer|management|manager|officer)\b",
    re.I,
)
_TRANSCRIPT_NAME_RE = re.compile(
    r"^[A-Z][A-Za-z0-9.'’\-]+(?:\s+[A-Z][A-Za-z0-9.'’\-]+){1,5}(?:\s*\([^)]*\))?$"
)
_TRANSCRIPT_TIMESTAMP_RE = re.compile(r"\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b")
_TRANSCRIPT_TIMESTAMP_PREFIX_RE = re.compile(r"^\[?\d{1,2}:(?:\d{1,2}:)?\d{2}\]?\s*", re.I)
_TRANSCRIPT_EVENT_RE = re.compile(
    r"\b(?:watch|listen|webcast|replay|register|event\s+details?|view\s+(?:the\s+)?presentation|"
    r"download\s+(?:the\s+)?(?:transcript|remarks)|join\s+(?:us|the)|upcoming\s+event)\b",
    re.I,
)
_TRANSCRIPT_BOILERPLATE_RE = re.compile(
    r"\b(?:safe[- ]harbor|forward[- ]looking\s+statements?|legal\s+disclaimer|copyright|"
    r"all\s+participants?|phone\s+lines?|webcast|conference\s+call|transcription|"
    r"thank\s+you\s+for\s+(?:joining|calling)|good\s+(?:morning|afternoon|evening)|"
    r"welcome\s+to|non[- ]?gaap\s+reconciliation|reconciliation\s+of\s+non[- ]?gaap)\b",
    re.I,
)
_TRANSCRIPT_TOPIC_RE = re.compile(
    r"\b(?:servicing|servicer|subservic|MSRs?|mortgage\s+servicing|prepay(?:ment|ments)?|"
    r"recapture|advance(?:s)?|delinquen(?:cy|t)|default(?:s)?|foreclos(?:ure|ed)|forbear(?:ance|ing)|"
    r"custodial|hedg(?:e|ing)|valuation|fair\s+value|fund(?:ing|ed)|financ(?:e|ing)|"
    r"cost(?:s)?\s+to\s+service|servicing\s+fee|profitability)\b",
    re.I,
)
_TRANSCRIPT_SERVICE_ANCHOR_RE = re.compile(r"\b(?:servicing|servicer|subservic|MSRs?|mortgage\s+servicing)\b", re.I)
_TRANSCRIPT_EXPLANATION_RE = re.compile(
    r"\b(?:because|due\s+to|driven\s+by|reflect(?:s|ed|ing)?|increased?|decreased?|declined?|"
    r"grew|changed?|remain(?:ed|s)?|expect(?:ed|s)?|outlook|guidance|impact(?:ed|s)?|"
    r"discuss|how|what|why|includes?|represents?|consists?)\b",
    re.I,
)
# Detect only visibly financial number forms for the provenance flag.  This is
# deliberately not a parser: timestamps, quarter labels and speaker names are
# not treated as financial values.
_TRANSCRIPT_DIGIT_RE = re.compile(
    r"(?<![A-Za-z])(?:[$€£]\s*\d[\d,.]*|\d[\d,.]*\s*(?:%|bps?|million|billion|thousand|bn|mm)\b)",
    re.I,
)


def _transcript_speaker_role(label: str) -> str:
    """Classify a speaker label without guessing from a person's name alone."""

    text = str(label or "").strip()
    if not text:
        return "unknown"
    if _TRANSCRIPT_OPERATOR_RE.search(text):
        return "operator"
    lowered = text.lower().strip()
    if lowered in {"q", "question", "questions", "analyst", "analyst question"} or _TRANSCRIPT_ANALYST_RE.search(text):
        return "analyst"
    if lowered in {"a", "answer", "management", "manager"} or _TRANSCRIPT_MANAGEMENT_RE.search(text):
        return "management"
    return "unknown"


def _transcript_name_like(label: str) -> bool:
    text = str(label or "").strip()
    if not text or len(text) > 120:
        return False
    # Keep title-bearing labels (``Jane Doe, CFO``) eligible even though the
    # comma is not part of the simple name pattern.
    base = re.split(r"\s*,\s*|\s+[–—-]\s*", text, maxsplit=1)[0].strip()
    return bool(_TRANSCRIPT_NAME_RE.fullmatch(base))


def _transcript_label_like(label: str, role: str) -> bool:
    """Avoid mistaking an ordinary sentence containing ``analyst`` for a label."""

    text = " ".join(str(label or "").split()).strip()
    if not text:
        return False
    lowered = text.lower()
    if lowered in {
        "q", "a", "question", "questions", "answer", "analyst", "analyst question",
        "operator", "moderator", "conference coordinator", "management", "manager",
    }:
        return True
    if _transcript_name_like(text):
        return True
    # A title-only line such as ``Chief Financial Officer`` may not include a
    # person's name.  Require that it is short and does not contain a servicing
    # topic, which keeps ordinary prose from becoming an active speaker label.
    if role == "management":
        return len(text) <= 100 and not _TRANSCRIPT_SERVICE_ANCHOR_RE.search(text)
    if role in {"analyst", "operator"}:
        # ``Analyst, Firm`` and ``Operator — ...`` are common labels.  A phrase
        # such as ``analyst coverage`` inside an event-page sentence is not.
        return bool(re.match(r"^(?:analyst|research\s+analyst|question|questions|q|operator|moderator|conference\s+coordinator)\b\s*[,\(\-–—]", text, re.I))
    return False


def _parse_transcript_speaker(text: str) -> tuple[str, str, bool, str]:
    """Return ``(speaker, role, has_label, body)`` for one source span."""

    value = " ".join(str(text or "").split())
    if not value:
        return "", "unknown", False, ""
    # A leading timestamp contains a colon of its own; remove it only for
    # speaker-label parsing while retaining the complete source text in the
    # returned passage.
    parse_value = _TRANSCRIPT_TIMESTAMP_PREFIX_RE.sub("", value, count=1).strip() or value
    if _TRANSCRIPT_QA_HEADING_RE.search(parse_value) or _TRANSCRIPT_REMARKS_HEADING_RE.search(parse_value):
        return "", "unknown", False, value

    # Most HTML transcript pages put ``Speaker: statement`` in one paragraph.
    if ":" in parse_value:
        prefix, body = parse_value.split(":", 1)
        prefix = prefix.strip()
        body = body.strip()
        role = _transcript_speaker_role(prefix)
        if _transcript_label_like(prefix, role):
            return prefix, role, True, body

    # PDF/text transcripts often use a dash instead of a colon.
    for separator in ("—", "–", " - "):
        if separator not in parse_value:
            continue
        prefix, body = parse_value.split(separator, 1)
        prefix = prefix.strip()
        body = body.strip()
        role = _transcript_speaker_role(prefix)
        if _transcript_label_like(prefix, role):
            # ``John Doe - Chief Financial Officer`` is a title line whose
            # speaker role is carried by the right side.
            if role == "unknown":
                role = _transcript_speaker_role(body)
            return prefix, role, True, body

    role = _transcript_speaker_role(parse_value)
    if _transcript_label_like(parse_value, role):
        return parse_value, role, True, ""
    if _transcript_name_like(parse_value):
        return parse_value, "unknown", True, ""
    return "", "unknown", False, parse_value


def _transcript_section(text: str, current: str) -> str:
    value = " ".join(str(text or "").split())
    if _TRANSCRIPT_QA_HEADING_RE.search(value):
        return "analyst_q_and_a"
    if _TRANSCRIPT_REMARKS_HEADING_RE.search(value):
        return "prepared_remarks"
    return current


def _transcript_statement_type(text: str) -> str:
    value = str(text or "")
    # Forward-looking language takes precedence when a sentence includes both a
    # reported result and a projection; this prevents an outlook from being
    # displayed as an achieved result.
    if re.search(
        r"\b(?:expect(?:s|ed)?|outlook|guidance|anticipat(?:e|es|ed)|forecast|target|plan(?:s|ned)?|"
        r"intend(?:s|ed)?|will|going\s+to|positioned|looking\s+ahead|next\s+quarter|next\s+year|"
        r"coming\s+quarters?|second\s+half|full[- ]year|longer\s+term|future)\b",
        value,
        re.I,
    ):
        return "outlook"
    if re.search(
        r"\b(?:this\s+quarter|quarter\s+ended|reported|delivered|increased?|decreased?|declined?|"
        r"grew|was|were|during\s+the\s+quarter|year[- ]over[- ]year|sequential(?:ly)?|"
        r"current\s+quarter|period)\b",
        value,
        re.I,
    ):
        return "reported_performance"
    return "other"


def _transcript_relevance_score(text: str, role: str) -> int:
    value = str(text or "")
    if not _TRANSCRIPT_SERVICE_ANCHOR_RE.search(value) or not _TRANSCRIPT_TOPIC_RE.search(value):
        return 0
    if _TRANSCRIPT_BOILERPLATE_RE.search(value):
        return 0
    if _TRANSCRIPT_EVENT_RE.search(value):
        return 0
    score = 2  # Explicit servicing/MSR anchor.
    if re.search(r"\b(?:MSRs?|subservic|prepay|recapture|advance|delinquen|default|hedg|valuation|fund|financ|cost|profitability)\w*\b", value, re.I):
        score += 2
    if _TRANSCRIPT_EXPLANATION_RE.search(value):
        score += 1
    if "?" in value:
        score += 1
    if _TRANSCRIPT_DIGIT_RE.search(value):
        score += 1
    if len(value) >= 80:
        score += 1
    if role in {"management", "analyst"}:
        score += 1
    return score


def _transcript_has_structure(spans: Sequence[SourceSpan], *, kind: str) -> bool:
    explicit_speaker = False
    has_heading = False
    for span in spans:
        value = " ".join(span.text.split())
        if _TRANSCRIPT_QA_HEADING_RE.search(value) or _TRANSCRIPT_REMARKS_HEADING_RE.search(value):
            has_heading = True
        _speaker, role, has_label, _body = _parse_transcript_speaker(value)
        if has_label and role in {"management", "analyst", "operator"}:
            explicit_speaker = True
    if explicit_speaker:
        return True
    # Prepared remarks can be published as speakerless prose.  Require the
    # explicit section heading and reject pages whose early text is clearly an
    # event/webcast landing page.
    if kind == "prepared_remarks" and has_heading:
        return not any(_TRANSCRIPT_EVENT_RE.search(span.text) for span in spans[:20])
    return False


def extract_transcript_passages(
    document: Any,
    *,
    config: Mapping[str, Any] | None = None,
    max_items: int | None = 8,
) -> list[TranscriptPassage]:
    """Select servicing passages from an archived transcript or prepared remarks.

    Only documents whose ``kind`` is explicitly transcript-like are eligible;
    event pages and webcast links return no passages.  Returned numeric text is
    marked ``source_excerpt_only`` and remains verbatim source context.  The
    helper never emits a financial ``Evidence`` record from conversational text.
    ``max_items=None`` returns every qualifying candidate after the complete
    document scan; numeric limits retain the historical bounded selection.
    """

    if not is_transcript_document(document):
        return []
    if max_items is None:
        limit: int | None = None
    else:
        try:
            limit = int(max_items)
        except (TypeError, ValueError, OverflowError):
            limit = 8
        limit = max(0, min(12, limit))
    if limit == 0:
        return []
    kind = re.sub(r"[\s-]+", "_", str(_doc_value(document, "kind", "")).strip().lower())
    try:
        spans = read_document_spans(document)
    except (OSError, UnicodeError, RuntimeError, ValueError):
        return []
    if not spans or not _transcript_has_structure(spans, kind=kind):
        return []
    period = document_period(document, spans)
    ticker = _ticker_for(document, config)
    document_id = str(_doc_value(document, "id", ""))
    section = "prepared_remarks" if kind in {"prepared_remarks", "prepared_remark"} else "unknown"
    active_speaker = ""
    active_role = "unknown"
    candidates: list[tuple[int, int, TranscriptPassage]] = []
    seen: set[str] = set()
    for index, span in enumerate(spans):
        line = " ".join(str(span.text or "").split()).strip()
        if not line:
            continue
        section = _transcript_section(line, section)
        speaker, role, has_label, body = _parse_transcript_speaker(line)
        if has_label and not body:
            active_speaker = speaker
            active_role = role
            continue
        if has_label:
            # A label with a body is also the active speaker for subsequent
            # fragmented PDF lines, but an unknown name never supplies a role.
            active_speaker = speaker or active_speaker
            if role != "unknown":
                active_role = role
        else:
            speaker = active_speaker
            role = active_role

        # Question marks in Q&A are a useful explicit cue when a transcript
        # omits the analyst's repeated label.  Do not infer a management role
        # for an unlabeled Q&A answer from the prior analyst question.
        if section == "analyst_q_and_a":
            if re.search(r"\?\s*$", line) and role != "management":
                role = "analyst"
            elif not has_label and role == "analyst":
                # A question's speaker label must not bleed into the next
                # unlabeled answer; without a management label the role is
                # unknown and the passage is omitted conservatively.
                role = "unknown"
        elif role == "unknown" and section == "prepared_remarks":
            role = "management"
        if role == "operator" or role not in {"management", "analyst"}:
            continue
        if _TRANSCRIPT_QA_HEADING_RE.search(line) or _TRANSCRIPT_REMARKS_HEADING_RE.search(line):
            continue
        score = _transcript_relevance_score(line, role)
        if score < 4:
            continue
        normalised = re.sub(r"\s+", " ", line).lower()
        if normalised in seen:
            continue
        seen.add(normalised)
        context = "analyst_question" if role == "analyst" else "management_answer" if section == "analyst_q_and_a" else "prepared_remarks"
        numeric_status = "source_excerpt_only" if _TRANSCRIPT_DIGIT_RE.search(line) else "none"
        timestamp_match = _TRANSCRIPT_TIMESTAMP_RE.search(line)
        metric = f"transcript_{context}_{_transcript_statement_type(line)}"
        passage = TranscriptPassage(
            id=evidence_id(document_id, metric, line, span.location, period),
            document_id=document_id,
            issuer=str(_doc_value(document, "issuer", "")),
            ticker=ticker,
            period=period,
            text=line,
            location=span.location,
            source_url=str(_doc_value(document, "url", "")),
            source_title=str(_doc_value(document, "title", "")),
            speaker=speaker,
            speaker_role=role,
            context=context,
            statement_type=_transcript_statement_type(line),
            numeric_status=numeric_status,
            numeric_evidence_ids=(),
            timestamp=timestamp_match.group(0) if timestamp_match else "",
        )
        candidates.append((score, index, passage))

    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: (-item[0], item[1]))
    management = [item for item in ordered if item[2].speaker_role == "management"]
    analyst = [item for item in ordered if item[2].speaker_role == "analyst"]
    selected: list[tuple[int, int, TranscriptPassage]] = []
    if management:
        selected.append(management[0])
    if analyst and (limit is None or len(selected) < limit):
        selected.append(analyst[0])
    for item in ordered:
        if limit is not None and len(selected) >= limit:
            break
        if item not in selected:
            selected.append(item)
    return [item[2] for item in selected]


def extract_transcript_evidence(
    document: Any,
    *,
    config: Mapping[str, Any] | None = None,
    max_items: int | None = 8,
) -> list[TranscriptPassage]:
    """Compatibility alias for callers that name source passages "evidence"."""

    return extract_transcript_passages(document, config=config, max_items=max_items)


# Public aliases used by callers and fixtures.
extract = extract_financial_facts
read_source = read_document_spans
