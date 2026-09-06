"""Build compact HTML and plain text mortgage-servicing earnings briefs."""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import hashlib
import re
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader
from bs4 import BeautifulSoup

from .evidence import Evidence, compatible, derive_absolute_change, derive_rate_change, evidence_json, parse_decimal
from .extraction import (Commentary, _commentary_from_spans, _financial_facts_from_spans,
                         extract_financial_facts, extract_transcript_passages,
                         is_transcript_document, read_document_spans)
from .narrative import generate_narrative
from .reviewed_context import select_context
from .analysis import AnalysisValidationError, select_analysis
from .reader_content import assert_reader_content
from .branding import apply_page_theme, brand_view, validate_page_theme
from .company_boundary import canonical_event, normalize_cik, require_company_boundary, validate_source_documents


_METRIC_LABELS = {
    "residential_servicing_income_before_msr_valuation": "Residential servicing income before MSR valuation",
    "residential_msr_valuation": "Residential net MSR valuation",
    "residential_servicing_income": "Total residential servicing income",
    "servicing_for_others_upb": "Servicing for others UPB",
    "owned_servicing_upb": "Bank-owned loans serviced UPB",
    "total_servicing_portfolio_upb": "Total servicing portfolio UPB",
    "servicing_upb": "Servicing UPB",
    "loans_serviced": "Loans serviced",
    "owned_msr_portfolio": "Owned MSR portfolio",
    "subservicing_portfolio": "Subservicing portfolio",
    "servicing_fee_income": "Servicing fee income",
    "servicing_operating_expense": "Servicing operating expense",
    "servicing_pretax_income": "Servicing pretax income",
    "adjusted_servicing_result": "Adjusted servicing result",
    "servicing_interest_expense": "Servicing interest expense",
    "servicing_valuation_related_items": "Servicing valuation-related items",
    "servicing_expenses_excluding_valuation": "Servicing expenses excluding valuation",
    "msr_cash_flow_realization": "MSR cash-flow realization",
    "servicing_cost_per_loan": "Servicing cost per loan",
    "msr_carrying_value": "MSR carrying value",
    "msr_fair_value_change": "MSR fair-value change",
    "msr_amortization": "MSR amortization",
    "msr_hedge_result": "MSR hedge result",
    "servicing_advances": "Servicing advances",
    "servicing_liquidity": "Servicing liquidity",
    "servicing_delinquency_rate": "Servicing delinquency/default rate",
    "servicing_fee_rate": "Weighted-average servicing fee rate",
    "serviced_loans_coupon_rate": "Weighted-average coupon rate",
    "servicing_foreclosure_count": "Servicing foreclosure activity",
    "servicing_forbearance_count": "Servicing forbearance activity",
}

# Edited only for the exact release sentences below. Other source language
# remains quoted; the full original commentary is retained in report.json.
_RELEASE_SUMMARIES = {
    "The increase from the prior quarter was primarily due to lower realization of MSR cash flows, reflecting lower prepayment speeds, and an increase in earnings on custodial deposits and other income due to higher average balances.":
        "Management attributed higher servicing revenue excluding valuation-related items primarily to slower prepayments, which reduced MSR cash-flow realization, and higher average custodial balances, which lifted earnings on deposits and other income.",
    "The increase from the prior quarter was primarily due to higher interest expense due to higher average balances of outstanding financing for MSRs.":
        "Management attributed higher expenses excluding valuation items primarily to higher interest expense on larger average MSR financing balances.",
}

_PFSI_TABLE_LABELS = {
    "servicing_pretax_income": "Pretax income",
    "adjusted_servicing_result": "Pretax before valuation¹",
    "servicing_valuation_related_items": "Valuation effects¹",
    "servicing_interest_expense": "Interest expense",
    "total_servicing_portfolio_upb": "Portfolio UPB",
}

_HIGHLIGHT_PHRASES = {
    ("PFSI", "servicing_pretax_income", "green"): "stronger pretax income",
    ("PFSI", "servicing_interest_expense", "red"): "higher interest expense",
    ("TFC", "servicing_for_others_upb", "green"): "higher servicing-for-others UPB",
    ("TFC", "residential_servicing_income", "red"): "lower residential servicing income",
}


def _source_exact_comparison(row: Mapping[str, Any] | None, metric: str) -> bool:
    """Return true only for two supported, non-derived instances of one metric."""

    if not row:
        return False
    fact = row.get("fact")
    prior = row.get("qoq")
    return bool(
        fact is not None
        and prior is not None
        and fact.metric == prior.metric == metric
        and fact.status == prior.status == "supported"
        and not fact.derivation
        and not prior.derivation
        and fact.unit == prior.unit
        and fact.currency == prior.currency
        and fact.scope == prior.scope
    )


def _reader_citations(value: str) -> str:
    """Clean citation title attributes without rewriting their destination URLs."""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(True):
        if tag.has_attr("title"):
            tag["title"] = re.sub(r"\s*—\s*", " - ", str(tag["title"]))
    return str(soup)


def _reader_copy(value, *, quoted=False):
    """Apply display punctuation while keeping URLs and evidence unchanged.

    Source excerpts are quoted in the reader-facing brief, so their em dashes
    become a plain `` - `` separator.  Other display prose keeps the existing
    semicolon treatment.  This copy is rendered only; canonical evidence is
    never mutated.
    """
    if isinstance(value, str):
        return value if value.startswith(("https://", "http://")) else re.sub(r"\s*—\s*", " - " if quoted else "; ", value)
    if isinstance(value, list):
        return [_reader_copy(item, quoted=quoted) for item in value]
    if isinstance(value, tuple):
        return tuple(_reader_copy(item, quoted=quoted) for item in value)
    if isinstance(value, dict):
        result = {}
        quote_item = quoted or ("text" in value and "location" in value and ("url" in value or "source_url" in value))
        for key, item in value.items():
            if key in {"url", "source_url"}:
                result[key] = item
            elif key.endswith("citations") and isinstance(item, str):
                result[key] = _reader_citations(item)
            else:
                result[key] = _reader_copy(item, quoted=quote_item if key == "text" else quoted)
        return result
    return value


def _value(document: Any, key: str, default: Any = "") -> Any:
    if isinstance(document, Mapping):
        return document.get(key, default)
    return getattr(document, key, default)


def _metadata(document: Any) -> dict[str, Any]:
    value = _value(document, "metadata", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _latest_document_versions(documents: Sequence[Any]) -> list[Any]:
    """Use the last observed version of one issuer URL and reporting period.

    Publication dates commonly stay unchanged when an issuer replaces a file.
    Observation timestamps establish version order; evidence hashes do not.
    Missing timestamps retain every version. Ties at the newest timestamp
    retain those candidates for conflict review, without reviving older data.
    """
    groups = {}
    for doc in documents:
        key = tuple(str(_value(doc, field)) for field in ("cik", "source", "url", "period"))
        groups.setdefault(key, []).append(doc)
    superseded = set()
    for key, versions in groups.items():
        if not key[2] or len(versions) < 2:
            continue
        observations = []
        for doc in versions:
            try:
                stamp = datetime.fromisoformat(str(_value(doc, "retrieved")).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    break
                observations.append((stamp, doc))
            except ValueError:
                break
        if len(observations) != len(versions):
            continue
        latest = max(stamp for stamp, _ in observations)
        superseded.update(id(doc) for stamp, doc in observations if stamp < latest)
    return [doc for doc in documents if id(doc) not in superseded]


def _ticker(document: Any, config: Mapping[str, Any]) -> str:
    metadata = _metadata(document)
    if metadata.get("ticker"):
        return str(metadata["ticker"]).upper()
    direct = _value(document, "ticker", "")
    if direct:
        return str(direct).upper()
    cik = str(_value(document, "cik", "")).lstrip("0") or "0"
    issuer = str(_value(document, "issuer", "")).lower()
    for company in config.get("companies", []) or []:
        if not isinstance(company, Mapping):
            continue
        ccik = str(company.get("cik", "")).lstrip("0") or "0"
        if (cik and ccik == cik) or (company.get("name") and str(company["name"]).lower() in issuer):
            return str(company.get("ticker", "")).upper()
    return ""


def _company_map(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for company in config.get("companies", []) or []:
        if not isinstance(company, Mapping):
            continue
        ticker = str(company.get("ticker", "")).upper()
        if ticker:
            result[ticker] = dict(company)
    return result


def _period_sort(value: str) -> tuple[int, int, int]:
    text = str(value or "")
    year = re.search(r"20\d{2}", text)
    q = re.search(r"Q([1-4])", text, re.I)
    return (int(year.group(0)) if year else 0, int(q.group(1)) if q else 0, 1 if "YTD" in text.upper() else 0)


def _is_unknown_period(value: str) -> bool:
    return not value or str(value).strip().lower() in {"unknown", "n/a", "na"}


def _period_targets(period: str) -> tuple[str | None, str | None]:
    match = re.fullmatch(r"(20\d{2})-Q([1-4])", str(period or ""))
    if match:
        year, quarter = int(match.group(1)), int(match.group(2))
        prior_q = f"{year}-Q{quarter - 1}" if quarter > 1 else f"{year - 1}-Q4"
        return prior_q, f"{year - 1}-Q{quarter}"
    fy = re.fullmatch(r"(20\d{2})-FY", str(period or ""), re.I)
    if fy:
        return None, f"{int(fy.group(1)) - 1}-FY"
    return None, None


def _resolve(candidates: Sequence[Evidence]) -> tuple[Evidence | None, list[Evidence]]:
    if not candidates:
        return None, []
    # Evidence does not store all document authority metadata, but SEC and
    # amendment hints are retained in source_kind/document_kind/title.
    def key(item: Evidence) -> tuple[int, str, str]:
        words = f"{item.document_kind} {item.source_title}".lower()
        amendment = item.document_kind.lower().endswith("/a") or any(term in words for term in ("amend", "restat", "corrected", "revision")) or bool(re.search(r"/a(?:\b|$)", words))
        source_rank = 2 if item.source_kind.lower() == "sec" else 1
        return (3 if amendment else source_rank, item.published, item.id)
    ordered = sorted(candidates, key=key, reverse=True)
    selected = ordered[0]
    conflicts = [item for item in ordered[1:] if item.value != selected.value]
    return selected, conflicts


def _facts_for_documents(documents: Sequence[Any], config: Mapping[str, Any], *, include_commentary=True) -> tuple[list[Evidence], list[Commentary], list[dict[str, Any]]]:
    facts: list[Evidence] = []
    commentary: list[Commentary] = []
    errors: list[dict[str, Any]] = []
    for document in documents:
        try:
            if not include_commentary:
                facts.extend(extract_financial_facts(document, config=config))
                continue
            spans = read_document_spans(document)
            if not is_transcript_document(document):
                facts.extend(_financial_facts_from_spans(document, spans, config=config))
            commentary.extend(_commentary_from_spans(document, spans))
        except Exception as exc:
            errors.append({"document_id": str(_value(document, "id", "")), "error": type(exc).__name__})
    return facts, commentary, errors


def _find_prior(current: Evidence, previous: Sequence[Evidence], target: str | None) -> Evidence | None:
    if not target:
        return None
    # Matching a ticker, period, unit, and broad scope is insufficient for a
    # like-for-like comparison: adjusted/GAAP, owned/third-party populations,
    # and flow/stock definitions must also agree.  Reuse the evidence-layer
    # compatibility gate so tables and arithmetic make the same decision.
    candidates = [fact for fact in previous if fact.period == target and fact.status == "supported" and compatible(current, fact)]
    selected, _conflicts = _resolve(candidates)
    return selected


def _company_name(ticker: str, docs: Sequence[Any], company_map: Mapping[str, Mapping[str, Any]]) -> str:
    if ticker in company_map and company_map[ticker].get("name"):
        return str(company_map[ticker]["name"])
    for doc in docs:
        if _value(doc, "issuer", ""):
            return str(_value(doc, "issuer", ""))
    return ticker or "Unknown issuer"


# Presentation and information hierarchy are owned by the Astra lead.
# One invocation builds one company note; source/evidence records remain full.
_MAIN_METRICS = {
    "TFC": ["residential_servicing_income", "residential_servicing_income_before_msr_valuation", "residential_msr_valuation", "servicing_for_others_upb", "owned_servicing_upb", "total_servicing_portfolio_upb"],
    "PFSI": ["servicing_pretax_income", "adjusted_servicing_result", "servicing_valuation_related_items", "servicing_interest_expense", "total_servicing_portfolio_upb"],
}
_SHORT_LABELS = {
    "residential_servicing_income": "Residential servicing income",
    "residential_servicing_income_before_msr_valuation": "Income before MSR valuation",
    "residential_msr_valuation": "Net MSR valuation",
    "servicing_for_others_upb": "Residential servicing for others · UPB",
    "owned_servicing_upb": "Bank-owned residential loans serviced · UPB",
    "total_servicing_portfolio_upb": "Total servicing portfolio · UPB",
    "servicing_pretax_income": "Servicing pretax income",
    "adjusted_servicing_result": "Pretax income excluding valuation items¹",
    "servicing_valuation_related_items": "Valuation-related items¹",
    "servicing_fee_income": "Servicing fees",
    "servicing_expenses_excluding_valuation": "Expenses excluding valuation items¹",
    "servicing_interest_expense": "Servicing interest expense",
    "owned_msr_portfolio": "Owned servicing · UPB",
    "subservicing_portfolio": "Subservicing · UPB",
}


def _amount(fact, value=None):
    if fact is None:
        return "n/a"
    number = parse_decimal(fact.value if value is None else value)
    magnitude = format(abs(number), ",f")
    suffix = {"USD_millions": "m", "USD_billions": "bn", "USD_thousands": "k", "USD": "", "percent": "%", "basis_points": " bps", "USD_per_loan": "/loan", "loans": " loans", "loans_millions": "m loans", "loans_thousands": "k loans"}.get(fact.unit, " " + fact.unit.replace("_", " "))
    amount = ("$" if fact.currency == "USD" else "") + magnitude + suffix
    return f"({amount})" if number < 0 else amount


def _period_label(period):
    match = re.fullmatch(r"(\d{4})-Q([1-4])", period or "")
    return f"Q{match[2]} {match[1]}" if match else period.replace("-", " ")


def _date(value, *, time=False):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc) if time else stamp
        if time:
            stamp = stamp.astimezone(ZoneInfo("America/New_York"))
        result = stamp.strftime("%b %d, %Y · %I:%M %p %Z" if time else "%b %d, %Y")
        return result.replace(" 0", " ")
    except (ValueError, TypeError):
        return str(value or "Date not established")


def _citation(facts, doc_numbers, *, color="#743b30"):
    links = []
    seen = set()
    for fact in facts:
        if fact is None or fact.document_id in seen:
            continue
        seen.add(fact.document_id)
        label = str(doc_numbers.get(fact.document_id, "source"))
        url = fact.source_url
        page = re.search(r"PDF page (\d+)", fact.location)
        if page and ".pdf" in url.lower():
            url = url.split("#")[0] + "#page=" + page[1]
        if url.startswith(("https://", "http://")):
            links.append(f'<a style="color:{escape(color, quote=True)};text-decoration:none;" href="{escape(url, quote=True)}" title="{escape(fact.location, quote=True)}">[{label}]</a>')
    return " ".join(links)


def _source_name(doc):
    kind = str(_value(doc, "kind")).lower()
    labels = {"release": "Earnings release", "presentation": "Presentation", "supplement": "Financial supplement", "10-q": "Quarterly filing · 10-Q", "10-k": "Annual filing · 10-K", "8-k": "Current report · 8-K", "transcript": "Earnings-call transcript", "earnings_call_transcript": "Earnings-call transcript", "prepared_remarks": "Prepared earnings remarks"}
    return labels.get(kind, str(_value(doc, "title", "Source document")))


def _event_date(documents):
    """Distinguish a proved release date from a filing or server timestamp."""
    date_pattern = r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4})"
    verified_documents = []
    for doc in documents:
        try:
            raw = Path(_value(doc, "path")).read_bytes()
        except (OSError, ValueError):
            continue
        if hashlib.sha256(raw).hexdigest() != str(_value(doc, "content_hash")).lower():
            continue
        verified_documents.append(doc)
        if str(_value(doc, "kind")).upper() not in {"8-K", "8-K/A", "RELEASE"}:
            continue
        try:
            text = BeautifulSoup(raw.decode("utf-8"), "html.parser").get_text(" ", strip=True).replace("\ufffd", " ")
        except UnicodeError:
            continue
        text = re.sub(r"\s+", " ", text)
        patterns = [r"Earnings Release\s+issued\s+" + date_pattern,
                    r"On\s+" + date_pattern + r",.{0,180}?issued (?:a |an )?(?:press|earnings) release"]
        if _value(doc, 'kind') == 'release':
            # A dateline in the opening identifies publication independently
            # of the SEC filing timestamp. Do not use dividend dates later
            # in the release as publication evidence.
            patterns = [r"\b[A-Z][A-Z ]+,\s*[A-Za-z]{2,6}\.?\s*[-–]\s*" + date_pattern + r"\s*[-–]"]
            text = text[:600]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                try:
                    date = datetime.strptime(re.sub(r"\s+", " ", match[1]), "%B %d, %Y").date().isoformat()
                except ValueError:
                    continue
                return {"date": date, "label": "Released", "source_url": _value(doc, "url"), "excerpt": match[0], "document_id": _value(doc, "id")}
    for doc in verified_documents:
        if _value(doc, "kind") == "release" and _value(doc, "published"):
            if _value(doc, "source") == "sec":
                return {"date": _value(doc, "published"), "label": "Filed", "source_url": _value(doc, "url"), "document_id": _value(doc, "id")}
            if _metadata(doc).get("publication_date_source") in {"issuer_content", "issuer_metadata"}:
                return {"date": _value(doc, "published"), "label": "Released", "source_url": _value(doc, "url"), "document_id": _value(doc, "id")}
    filings = [d for d in verified_documents if _value(d, "source") == "sec" and _value(d, "published")]
    if filings:
        doc = min(filings, key=lambda d: _value(d, "published"))
        return {"date": _value(doc, "published"), "label": "Filed", "source_url": _value(doc, "url"), "document_id": _value(doc, "id")}
    return {"date": "", "label": "Publication date not established"}


def _company_view(config, documents, facts, prior_facts, commentary, *, baseline, coverage):
    ticker = _ticker(documents[0], config)
    name = _company_name(ticker, documents, _company_map(config))
    display_name = {"TFC": "Truist", "PFSI": "PennyMac Financial Services", "WFC": "Wells Fargo", "RKT": "Rocket Companies"}.get(ticker, name)
    period = max((str(_value(d, "period")) for d in documents if not _is_unknown_period(str(_value(d, "period")))), key=_period_sort, default="unknown")
    qoq_period, yoy_period = _period_targets(period)
    current = {}
    conflicts = []
    for fact in facts:
        if fact.period != period or fact.status != "supported":
            continue
        key = (fact.metric, fact.unit, fact.scope)
        current.setdefault(key, []).append(fact)
    selected = []
    for candidates in current.values():
        value, others = _resolve(candidates)
        selected.append(value)
        conflicts.extend(others)
    comparison_pool = [*prior_facts, *[f for f in facts if f.period != period]]
    rows = []
    changes = []
    for fact in selected:
        qoq = _find_prior(fact, comparison_pool, qoq_period)
        yoy = _find_prior(fact, comparison_pool, yoy_period)
        delta = (derive_rate_change(fact, qoq) if fact.unit in {"percent", "basis_points"} else derive_absolute_change(fact, qoq)) if qoq else None
        if delta:
            changes.append(delta)
        rows.append({"fact": fact, "qoq": qoq, "yoy": yoy, "delta": delta})
    metric_order = _MAIN_METRICS.get(ticker, list(_METRIC_LABELS))
    rows.sort(key=lambda row: metric_order.index(row["fact"].metric) if row["fact"].metric in metric_order else 999)
    main_rows = [row for row in rows if row["fact"].metric in metric_order][:9]
    by_metric = {row["fact"].metric: row for row in rows}
    sources = sorted(documents, key=lambda d: ({"release": 0, "presentation": 1, "supplement": 2, "10-Q": 3, "10-K": 3}.get(_value(d, "kind"), 4), _value(d, "published")))
    doc_numbers = {str(_value(doc, "id")): i + 1 for i, doc in enumerate(sources)}
    points = []
    for metric, topic in [(metric_order[0] if metric_order else "", "Earnings"), ("adjusted_servicing_result", "Before valuation"), ("servicing_interest_expense", "Funding costs"), ("total_servicing_portfolio_upb", "Portfolio")]:
        row = by_metric.get(metric)
        if not row or any(p["metric"] == metric for p in points):
            continue
        fact, prior = row["fact"], row["qoq"]
        label = _SHORT_LABELS.get(metric, _METRIC_LABELS.get(metric, metric)).replace("¹", "").replace(" · UPB", " UPB")
        sentence = f"{label} was {_amount(fact)}"
        if prior:
            direction = "up from" if fact.decimal_value > prior.decimal_value else "down from" if fact.decimal_value < prior.decimal_value else "unchanged from"
            sentence += f", {direction} {_amount(prior)} in {_period_label(qoq_period)}"
        sentence += "."
        points.append({"topic": topic, "text": sentence, "citations": _citation([fact, prior], doc_numbers), "metric": metric, "source_ids": [f.id for f in [fact, prior] if f]})
    points = points[:3]
    if len(points) < 3:
        for row in main_rows:
            fact, prior = row["fact"], row["qoq"]
            if any(p["metric"] == fact.metric for p in points) or not prior:
                continue
            sentence = f"{_SHORT_LABELS.get(fact.metric, _METRIC_LABELS.get(fact.metric, fact.metric))} was {_amount(fact)}, versus {_amount(prior)} in {_period_label(qoq_period)}."
            points.append({"topic": "Servicing", "text": sentence, "citations": _citation([fact, prior], doc_numbers), "metric": fact.metric, "source_ids": [fact.id, prior.id]})
            if len(points) == 3:
                break
    lead = "New financial materials are ready for review."
    if main_rows:
        row = main_rows[0]
        label = _SHORT_LABELS.get(row["fact"].metric, _METRIC_LABELS.get(row["fact"].metric, row["fact"].metric)).replace("¹", "")
        lead = label + ": " + _amount(row["fact"])
    new_ids = set(coverage.get("new_document_ids", []))
    additions = [d for d in documents if _value(d, "id") in new_ids]
    release_added = any(_value(d, "kind") == "release" for d in additions)
    supporting_update = not baseline and bool(additions) and not release_added and not any(f.document_id in new_ids for f in facts)
    if supporting_update:
        kinds = list(dict.fromkeys(_source_name(d) for d in additions))
        lead = "Now available: " + ", ".join(kinds) + "."
        points = []
        main_rows = []
    # A restrained editorial headline, derived only from compatible evidence.
    # The supporting paragraphs immediately state the actual figures.
    editorial = lead
    first_row = by_metric.get(metric_order[0]) if metric_order else None
    if ticker in {"TFC", "PFSI"} and first_row and first_row["qoq"] and not supporting_update:
        now, prior = first_row["fact"].decimal_value, first_row["qoq"].decimal_value
        direction = "rose" if now > prior else "fell" if now < prior else "held steady"
        prior_period_label = _period_label(qoq_period)
        prior_quarter_label = re.sub(r" \d{4}$", "", prior_period_label)
        editorial = (
            f"Residential servicing income {direction}."
            if ticker == "TFC"
            else f"Servicing pretax income {direction} {'from' if direction != 'held steady' else 'versus'} {prior_quarter_label}."
        )
        funding = by_metric.get("servicing_interest_expense")
        portfolio = by_metric.get("total_servicing_portfolio_upb")
        if ticker == "PFSI" and _source_exact_comparison(funding, "servicing_interest_expense") and funding["fact"].decimal_value > funding["qoq"].decimal_value:
            editorial = (
                f"Servicing pretax income rose from {prior_quarter_label}, while financing costs increased."
                if direction == "rose"
                else editorial + " Financing costs increased."
            )
        elif ticker == "TFC" and portfolio and portfolio["qoq"] and portfolio["fact"].decimal_value > portfolio["qoq"].decimal_value:
            editorial += " The portfolio grew."
    # Select a complete causal sentence from an earnings release. Never promote
    # company-wide 10-Q boilerplate or fragmented PDF lines to an explanation.
    release_ids = {_value(d, "id") for d in documents if _value(d, "kind") == "release" or _metadata(d).get("document", "").endswith("ex99-1.htm")}
    executive_intro = None
    if ticker == "PFSI" and not supporting_update:
        bridge = [by_metric.get(key) for key in ("adjusted_servicing_result", "servicing_valuation_related_items", "servicing_pretax_income")]
        if all(bridge):
            prevaluation, valuation, reported = [row["fact"] for row in bridge]
            prior_prevaluation = bridge[0]["qoq"]
            text = "Servicing income excluding valuation-related items"
            if prior_prevaluation:
                if prevaluation.decimal_value > prior_prevaluation.decimal_value:
                    text += f" rose to {_amount(prevaluation)} from {_amount(prior_prevaluation)} in {_period_label(qoq_period)}"
                elif prevaluation.decimal_value < prior_prevaluation.decimal_value:
                    text += f" fell to {_amount(prevaluation)} from {_amount(prior_prevaluation)} in {_period_label(qoq_period)}"
                else:
                    text += f" was {_amount(prevaluation)}, unchanged from {_amount(prior_prevaluation)} in {_period_label(qoq_period)}"
            else:
                text += f" was {_amount(prevaluation)}"
            text += f". Valuation-related items of {_amount(valuation)} left reported servicing pretax income at {_amount(reported)}. The issuer-defined prevaluation measure includes mortgage servicing rights (MSR) cash-flow realization and financing expense. It is not cash earnings."
            executive_intro = {"text": text, "citations": _citation([prevaluation, bridge[0]["qoq"], valuation, reported], doc_numbers, color="#dbe4e8")}
            # The opening already explains these two values. Keep only the
            # incremental funding-cost development in the next body section.
            points = [point for point in points if point.get("metric") not in {"adjusted_servicing_result", "servicing_pretax_income"}]
    explanations = []
    for comment in commentary:
        if comment.document_id not in release_ids or not re.match(r"Servicing (?:revenues|expenses)", comment.text):
            continue
        sentences = re.split(r"(?<=[.!?])\s+(?=[A-Z])", comment.text)
        quote = next((s for s in sentences if "increase from the prior quarter was primarily due to" in s.lower()), "")
        if quote and len(quote) <= 480:
            explanations.append({"text": quote, "summary": _RELEASE_SUMMARIES.get(" ".join(quote.split()), "") if ticker == "PFSI" else "", "topic": "Revenue excluding valuation items" if "revenues" in comment.text[:25] else "Expenses excluding valuation items", "url": comment.source_url, "location": comment.location, "source_number": doc_numbers.get(comment.document_id, "source")})
    if supporting_update:
        explanations = []
    display_rows = []
    green_metric = "servicing_pretax_income" if ticker == "PFSI" else "servicing_for_others_upb" if ticker == "TFC" else ""
    red_metric = "servicing_interest_expense" if ticker == "PFSI" else "residential_servicing_income" if ticker == "TFC" else ""
    green_highlighted = False
    red_highlighted = False
    highlight_phrases = []
    for row in main_rows:
        fact = row["fact"]
        label = _SHORT_LABELS.get(fact.metric, _METRIC_LABELS.get(fact.metric, fact.metric.replace("_", " ")))
        if ticker == "PFSI":
            label = _PFSI_TABLE_LABELS.get(fact.metric, label)
        highlight = ""
        if not green_highlighted and _source_exact_comparison(row, green_metric) and fact.decimal_value > row["qoq"].decimal_value:
            highlight = "green"
            green_highlighted = True
        if not highlight and not red_highlighted and _source_exact_comparison(row, red_metric) and ((ticker == "PFSI" and fact.decimal_value > row["qoq"].decimal_value) or (ticker == "TFC" and fact.decimal_value < row["qoq"].decimal_value)):
            highlight = "red"
            red_highlighted = True
        phrase = _HIGHLIGHT_PHRASES.get((ticker, fact.metric, highlight))
        if phrase:
            highlight_phrases.append(phrase)
        display_rows.append({"label": label, "current": _amount(fact), "qoq": _amount(row["qoq"]), "yoy": _amount(row["yoy"]), "citations": _citation([fact, row["qoq"], row["yoy"]], doc_numbers), "definition": fact.definition, "highlight": highlight})
    highlight_note = ""
    if highlight_phrases:
        basis = _period_label(qoq_period) if qoq_period else "the prior comparable period"
        highlight_note = f"Versus {basis}: {' and '.join(highlight_phrases)}."
    chart = None
    if main_rows:
        first = main_rows[0]
        values = [f for f in (first["yoy"], first["qoq"], first["fact"]) if f is not None]
        ceiling = max(abs(f.decimal_value) for f in values)
        if len(values) >= 2 and ceiling > 0:
            chart = {"title": _SHORT_LABELS.get(first["fact"].metric, _METRIC_LABELS.get(first["fact"].metric, "Servicing")),
                     "signed": any(f.decimal_value < 0 for f in values), "citations": _citation(values, doc_numbers),
                     "scale_max": str(ceiling), "scale_height_px": 120, "scale_formula": "abs(source_value) / scale_max * scale_height_px; pixels truncated to integer",
                     "bars": [{"period": _period_label(f.period), "amount": _amount(f), "width": format(abs(f.decimal_value) / ceiling * 100, ".1f"), "height": int(abs(f.decimal_value) / ceiling * 120), "raw_value": f.value, "unit": f.unit, "evidence_id": f.id, "negative": f.decimal_value < 0, "current": f.id == first["fact"].id} for f in values]}
            if first["qoq"] and first["yoy"]:
                now = first["fact"].decimal_value
                quarter_direction = "Above" if now > first["qoq"].decimal_value else "Below" if now < first["qoq"].decimal_value else "Level with"
                year_direction = "above" if now > first["yoy"].decimal_value else "below" if now < first["yoy"].decimal_value else "level with"
                chart["reader_note"] = f"{quarter_direction} last quarter; {year_direction} a year ago."
    notes = []
    if main_rows and ticker == "TFC":
        notes.append("Income covers residential servicing. The total portfolio includes both loans serviced for others and bank-owned loans, so it is broader than the third-party portfolio. Servicing expense and pretax profit cannot be determined from this table.")
    if main_rows and ticker == "PFSI":
        notes.append("¹ Non-GAAP presentation; see the issuer's reconciliation. Portfolio UPB is a period-end balance and includes owned servicing, subservicing and loans held for sale.")
    if any(row["qoq"] is None or row["yoy"] is None for row in main_rows):
        notes.append("n/a = no comparable figure available in this brief.")
    if not rows and not supporting_update:
        notes.append("Financial comparisons are unavailable in this brief; see the issuer materials below.")
    if conflicts:
        notes.append("Source values differ. Figures use the latest corrected or higher-authority disclosure.")
    if main_rows:
        has_negative = any(f is not None and f.decimal_value < 0 for row in main_rows for f in (row['fact'], row['qoq'], row['yoy']))
        notes.append("m = million; bn = billion; UPB = unpaid principal balance." + (" Parentheses indicate negative values." if has_negative else ""))
    issues = []
    own_names = {ticker, name, str(_value(documents[0], "cik"))}
    own_coverage = []
    for item in [*coverage.get("errors", []), *coverage.get("pending", [])]:
        if not isinstance(item, Mapping):
            continue
        identity = item.get("ticker") or item.get("issuer") or item.get("cik")
        if identity and str(identity) not in own_names:
            continue
        own_coverage.append(item)
    # Transport details, status codes and collector reasons stay in coverage
    # records.  Readers only need a short source-availability caveat.
    for source_key, source_label in (("ir", "Investor relations"), ("sec", "SEC")):
        source_items = [item for item in own_coverage if item.get("source") == source_key]
        blocked = any(item.get("blocked") or "block" in str(item.get("reason", "")).lower() for item in source_items)
        if blocked:
            issues.append(f"{source_label}: access was blocked; available documents are included.")
        elif source_items:
            issues.append(f"{source_label}: some source materials remain unconfirmed; available documents are included.")
    if not any(_value(d, "kind") == "presentation" for d in documents):
        issues.append("A current-period presentation was not found in the accessible sources.")
    source_rows = []
    for doc in sources:
        metadata = _metadata(doc)
        format_note = "HTML + original images in ZIP" if metadata.get("assets") else "PDF" if str(_value(doc, "path")).lower().endswith(".pdf") else "HTML" if str(_value(doc, "path")).lower().endswith((".htm", ".html")) else "Document"
        source_rows.append({"number": doc_numbers[_value(doc, "id")], "name": _source_name(doc), "url": _value(doc, "url"), "date": _date(_value(doc, "published")) if _value(doc, "published") else "", "format": format_note, "classification": str(_value(doc, "classification")).replace("sec-", "SEC ").replace("issuer-published", "Issuer published")})
    event_date = _event_date(documents)
    source_locations = list(dict.fromkeys((f.source_title, re.sub(r", row .*", "", f.location), f.source_url) for f in selected))
    return {"ticker": ticker, "name": display_name, "executive_intro": executive_intro, "period": _period_label(period), "raw_period": period, "qoq_label": _period_label(qoq_period or "Prior quarter"), "yoy_label": _period_label(yoy_period or "Prior year"), "lead": lead, "editorial_headline": editorial, "chart": chart, "points": points, "rows": display_rows, "highlight_note": highlight_note, "explanations": explanations[:2], "notes": notes, "issues": issues, "sources": source_rows, "baseline": baseline, "supporting_update": supporting_update, "event_date": _date(event_date["date"]) if event_date["date"] else "", "event_date_label": event_date["label"], "event_date_evidence": event_date, "as_of": _date(coverage.get("as_of"), time=True), "source_locations": source_locations, "questions": ["Can servicing earnings keep improving if MSR financing costs remain elevated?", "How much of the earnings improvement is sustainable after valuation and hedge effects?"] if main_rows and ticker == "PFSI" else ["Why did residential servicing income fall while the portfolio grew?", "What expense disclosure would help investors assess servicing profitability?"] if main_rows and ticker == "TFC" else []}, changes


def build_report(config, documents, *, baseline=False, coverage=None, previous_documents=None):
    """Produce one company draft, with complete source evidence beside it."""
    documents = list(documents)
    if not documents:
        raise ValueError("A company draft requires source documents")
    validate_source_documents(documents)
    ciks = {normalize_cik(_value(d, "cik")).lstrip("0") for d in documents}
    coverage = dict(coverage or {})
    coverage.setdefault("as_of", datetime.now(timezone.utc).isoformat())
    generated_at = datetime.now(timezone.utc).isoformat()
    previous_documents = [d for d in (previous_documents or []) if str(_value(d, "cik")).lstrip("0") in ciks]
    facts, commentary, errors = _facts_for_documents(documents, config)
    # Retain the full canonical extraction, including superseded versions, but
    # render and interpret only the current observed source content. A removed
    # metric or passage must not reappear from an earlier copy of that URL.
    canonical_facts, canonical_commentary = facts, commentary
    documents = _latest_document_versions(documents)
    active_ids = {str(_value(doc, "id")) for doc in documents}
    facts = [fact for fact in facts if fact.document_id in active_ids]
    commentary = [item for item in commentary if item.document_id in active_ids]
    current_period = max((str(_value(d, "period")) for d in documents), key=_period_sort, default="unknown")
    comparison_periods = set(_period_targets(current_period)) - {None}
    comparison_documents = [d for d in previous_documents if _value(d, "period") in comparison_periods]
    prior_facts, _, prior_errors = _facts_for_documents(comparison_documents, config, include_commentary=False)
    canonical_prior_facts = prior_facts
    previous_documents = _latest_document_versions(previous_documents)
    prior_ids = {str(_value(doc, "id")) for doc in previous_documents}
    prior_facts = [fact for fact in prior_facts if fact.document_id in prior_ids]
    view, changes = _company_view(config, documents, facts, prior_facts, commentary, baseline=baseline, coverage=coverage)
    view["company_identity"] = {
        "ticker": view["ticker"],
        "cik": normalize_cik(_value(documents[0], "cik")),
        "event": canonical_event(view["raw_period"]),
        "kind": "earnings_brief",
    }
    view["prepared_at"] = _date(generated_at, time=True)
    headline_main, sentence_break, headline_secondary = view["editorial_headline"].partition(". ")
    view["headline_main"] = headline_main + ("." if sentence_break else "")
    view["headline_secondary"] = headline_secondary
    view["context_heading"] = "Portfolio and risk"
    view["reviewed_context"] = select_context([*documents, *previous_documents], cik=_value(documents[0], "cik"), period=view["raw_period"],
                                              new_ids=set(coverage.get("new_document_ids", [])) if view["supporting_update"] else None)
    explicit_analysis = config.get("analysis", {}).get("catalog_path")
    analysis_path = explicit_analysis or (
        Path(__file__).with_name("reviewed_analysis") / f"{view['ticker']}-{view['raw_period']}.json"
    )
    analysis_error = ""
    try:
        analysis = select_analysis(
            analysis_path, [*documents, *previous_documents], identity=view["company_identity"],
            base_dir=config.get("_root", Path.cwd()), evidence=[*facts, *prior_facts],
            required_document_ids=coverage.get("new_document_ids", ()) if view["supporting_update"] else (),
        )
    except AnalysisValidationError as exc:
        if explicit_analysis:
            raise
        # A newly observed source package still earns its deterministic draft.
        # Hold the entire old essay out and record the need for fresh authorship.
        analysis = None
        analysis_error = str(exc)
    view["ai_analysis"] = analysis["sections"] if analysis else []
    context_ids = {p["document_id"] for entry in view["reviewed_context"] for p in entry["sources"]}
    analysis_urls = {p["source_url"] for entry in view["ai_analysis"] for p in entry["sources"]}
    context_ids.update(_value(d, "id") for d in previous_documents if _value(d, "url") in analysis_urls)
    context_documents = [d for d in previous_documents if _value(d, "id") in context_ids]
    for doc in context_documents:
        view["sources"].append({"number": len(view["sources"]) + 1, "name": _source_name(doc) + " · " + _period_label(_value(doc, "period")) + " context",
                                "url": _value(doc, "url"), "date": _date(_value(doc, "published")), "format": "PDF" if str(_value(doc, "path")).lower().endswith(".pdf") else "HTML",
                                "classification": str(_value(doc, "classification")).replace("sec-", "SEC ")})
    context_numbers = {source["url"]: source["number"] for source in view["sources"]}
    for entry in view["ai_analysis"]:
        for proof in entry["sources"]:
            proof["catalog_number"] = proof["number"]
    for entry in [*view["reviewed_context"], *view["ai_analysis"]]:
        for proof in entry["sources"]:
            proof["number"] = context_numbers.get(proof["source_url"], "source")
    # Keep exact locations in citation tooltips and metadata. Repeating every
    # paragraph's locator in a source-list label makes phone reading unwieldy.
    for source in view["sources"]:
        locations = [location for _, location, url in view["source_locations"] if url == source["url"]]
        locations.extend(proof["location"] for entry in view["reviewed_context"] for proof in entry["sources"] if proof["source_url"] == source["url"])
        locations.extend(proof["location"] for entry in view["ai_analysis"] for proof in entry["sources"] if proof["source_url"] == source["url"])
        locations = list(dict.fromkeys(re.sub(r"^(?:Presentation, |10-Q, )", "", re.sub(r";? HTML text line \d+", "", location)).strip(" ,;") for location in locations))
        source["locations"] = locations
        if analysis:
            source["name"] = {"Earnings release": "Earnings release", "Presentation": "Earnings presentation",
                              "Quarterly filing · 10-Q": "Form 10-Q"}.get(source["name"], source["name"])
        elif locations:
            source["name"] += ": " + "; ".join(locations)
    view['earnings_context'] = [item for item in view['reviewed_context'] if item['id'] == 'pfsi-q226-advance-expense']
    view['reviewed_context'] = [item for item in view['reviewed_context'] if item['id'] != 'pfsi-q226-advance-expense']
    for item in view['reviewed_context']:
        if item['id'] == 'pfsi-q226-retention':
            item['text'] = item['text'].replace('11.6% CPR', '11.6% conditional prepayment rate (CPR)')
    transcript_documents = [d for d in documents if is_transcript_document(d)]
    new_ids = set(coverage.get("new_document_ids", []))
    if view["supporting_update"]:
        transcript_documents = [d for d in transcript_documents if _value(d, "id") in new_ids]
    call_passages = [p for doc in transcript_documents for p in extract_transcript_passages(doc, config=config, max_items=None)
                     if p.period == view["raw_period"]]
    source_numbers = {source["url"]: source["number"] for source in view["sources"]}
    view["call_passages"] = []
    for passage in call_passages:
        words = len(passage.text.split())
        if words > 90:
            continue
        item = passage.to_dict()
        item["number"] = source_numbers.get(passage.source_url, "source")
        item["label"] = {"prepared_remarks": "Prepared remarks", "management_answer": "Management answer", "analyst_question": "Analyst question"}.get(passage.context, "Call excerpt")
        if passage.statement_type == "outlook" and passage.speaker_role != "analyst":
            item["label"] += " · outlook"
        view["call_passages"].append(item)
    if not any(is_transcript_document(d) for d in documents):
        ir_blocked = any(issue.startswith('Investor relations:') and 'blocked' in issue for issue in view['issues'])
        ir_unconfirmed = any(issue.startswith('Investor relations:') for issue in view['issues'])
        view["issues"] = [issue for issue in view["issues"] if not issue.startswith("Investor relations:")]
        view["issues"].append('Full call transcript not located; issuer IR access was blocked.' if ir_blocked
                              else 'Full call transcript not located; some issuer materials remain unconfirmed.' if ir_unconfirmed
                              else 'Full call transcript not located in available materials.')
    if view["questions"]:
        view["questions"] = (["What portion of servicing earnings can persist through changes in prepayments, valuations and hedging?", "How sensitive are servicing returns to the cost of financing MSRs?"]
                             if view["ticker"] == "PFSI" else ["Is portfolio growth translating into sustainable residential servicing income?", "What expense disclosure would help investors assess servicing profitability?"])
    if any(item['id'] == 'pfsi-q226-cenlar-outlook' for item in view['reviewed_context']):
        view['questions'] = [
            'What contribution remains from Cenlar fees after onboarding costs, ongoing servicing expense and required service levels?',
            'How are servicing-advance recoveries and financing costs affecting cash returns as the portfolio grows?',
        ]
    table_sources = {tuple(re.findall(r'href="([^"]+)"', row["citations"])) for row in view["rows"] if row["citations"]}
    view["table_citations"] = view["rows"][0]["citations"] if len(table_sources) == 1 else ""
    if view["table_citations"]:
        for row in view["rows"]:
            row["citations"] = ""
    evidence = list({f.id: f for f in [*canonical_facts, *canonical_prior_facts, *changes]}.values())
    ai_config = config.get("ai", {})
    ai_facts = [f for f in facts if f.period == view["raw_period"] and f.status == "supported"]
    ai_comments = [c for c in commentary if c.period == view["raw_period"] and re.match(r"(?:Mortgage |Residential )?Servicing\b|MSR\b", c.text, re.I)]
    narrative = generate_narrative(ai_facts, ai_comments, ai_config=ai_config, cache_dir=Path(config.get("_storage", ".")) / "narrative_cache")
    view["selected_excerpts"] = []
    cited_items = {item.id: item for item in [*ai_facts, *ai_comments]}
    source_numbers = {source["url"]: source["number"] for source in view["sources"]}
    if narrative.used_ai:
        seen = set()
        for claim in narrative.claims:
            quote = claim["text"]
            if len(quote) > 480 or quote in seen or any(item["text"] in quote for item in view["explanations"]):
                continue
            refs = [cited_items[key] for key in [*claim.get("evidence_ids", []), *claim.get("commentary_ids", [])] if key in cited_items]
            if refs:
                source = refs[0]
                view["selected_excerpts"].append({"text": quote, "url": source.source_url, "location": source.location, "number": source_numbers.get(source.source_url, "source")})
                seen.add(quote)
            if len(view["selected_excerpts"]) == 2:
                break
    view["narrative_note"] = "Evidence-only brief · source summaries and verified figures."
    if view["selected_excerpts"]:
        view["narrative_note"] = "Financial figures come from verified evidence. Optional AI selected the additional cited source excerpts."
    try:
        stamp = datetime.fromisoformat(coverage["as_of"].replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York"))
        date = stamp.strftime("%Y-%m-%d")
    except ValueError:
        date = str(coverage["as_of"])[:10]
    view["subject"] = f"{view['ticker']} | {view['period']} servicing {'document update' if view['supporting_update'] else 'earnings brief'} | {date}"
    env = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")), autoescape=lambda name: bool(name and ".html." in name))
    if not view.get('executive_intro') and re.search(r"\bMSRs?\b", str(view)):
        view["notes"].append("MSR = mortgage servicing rights.")
    display_view = _reader_copy(view)
    display_view['brand'] = brand_view(view['ticker'], view['name'])
    if display_view.get('executive_intro'):
        display_view['executive_intro']['citations'] = display_view['executive_intro']['citations'].replace('#dbe4e8', display_view['brand']['theme']['hero_text'])
    html = env.get_template("brief.html.j2").render(**display_view)
    html = apply_page_theme(html, display_view['brand']['theme'])
    validate_page_theme(html, view['ticker'])
    text = env.get_template("brief.txt.j2").render(**display_view)
    assert_reader_content(html, text, subject=view['subject'])
    report = {"subject": view["subject"], "company_identity": view["company_identity"], "generated_at": generated_at, "branding": {key: display_view["brand"][key] for key in ("ticker", "verified", "gap_reason", "primary_color", "public_logo_url")}, "event_date_evidence": view["event_date_evidence"], "chart": view["chart"], "html": html, "text": text, "evidence": evidence_json(evidence), "company_reports": {view["ticker"]: {"html": html, "text": text}}, "commentary": [c.to_dict() for c in commentary], "reviewed_context": view["reviewed_context"], "context_documents": [d.to_dict() for d in context_documents], "transcript_passages": [p.to_dict() for p in call_passages], "displayed_transcript_passage_ids": [p["id"] for p in view["call_passages"]], "changes": [c.to_dict() for c in changes], "narrative": narrative.to_dict(), "coverage": coverage, "extraction_errors": errors + prior_errors, "design_version": "astra-led-financial-brief-v7"}
    report['reviewed_context'] = [*view.get('earnings_context', []), *view['reviewed_context']]
    report['ai_analysis'] = analysis
    report['ai_analysis_status'] = {'status': 'source_validated' if analysis else 'requires_fresh_analysis' if analysis_error else 'not_authored', 'error': analysis_error}
    report['sources'] = view['sources']
    report['design_version'] = 'astra-led-financial-brief-v8-original-analysis'
    report['commentary'] = [item.to_dict() for item in canonical_commentary]
    require_company_boundary(report, documents, context_documents=context_documents)
    return report


