"""Evidence primitives and conservative arithmetic for servicing disclosures.

The reporting layer deliberately works with small, inspectable records rather than
numbers hidden in a narrative.  Values are kept as decimal strings so that a
source value such as ``1,200.0`` keeps its scale and never passes through binary
floating point arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence


_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212"
_MISSING = {"", "-", "--", "—", "–", "n/a", "na", "nm", "not meaningful", "not available", "n.m."}


def decimal_string(value: Decimal | str | int) -> str:
    """Return a non-exponent decimal string while preserving source scale."""

    if isinstance(value, bool):
        raise TypeError("boolean is not a financial value")
    if isinstance(value, float):
        raise TypeError("float financial values are not accepted; use Decimal or text")
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if not dec.is_finite():
        raise ValueError("financial values must be finite")
    # Decimal.__format__ preserves trailing zeroes in the coefficient and avoids
    # scientific notation for ordinary statement values.
    return format(dec, "f")


def parse_decimal(value: Any, *, allow_missing: bool = False) -> Decimal | None:
    """Parse an exact financial number.

    Parentheses and Unicode minus signs are handled.  A float is rejected because
    converting it would make the source precision unknowable.  Missing source
    markers return ``None`` only when ``allow_missing`` is true.
    """

    if value is None:
        if allow_missing:
            return None
        raise ValueError("missing financial value")
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError("financial values must be Decimal or source text, not float")
    text = str(value).strip()
    if text.lower() in _MISSING:
        if allow_missing:
            return None
        raise ValueError(f"missing financial value: {text!r}")
    text = text.translate(str.maketrans({ch: "-" for ch in _DASHES}))
    # A standalone Unicode minus is a missing-cell marker, just like ``-``.
    # Check after translation so all statement dash variants follow the same
    # fail-closed path instead of reaching Decimal("-") and raising.
    if text in _MISSING:
        if allow_missing:
            return None
        raise ValueError(f"missing financial value: {value!r}")
    text = text.replace(",", "").replace("$", "").replace("€", "").replace("£", "")
    text = re.sub(r"\s+", "", text)
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    # Percent, bps, and statement scale markers are unit metadata, not part of
    # Decimal syntax.  The scale is retained by extraction as the unit field.
    text = re.sub(r"(?:%|bps?|basis\s*points?|million|billion|thousand|mm|bn)$", "", text, flags=re.I)
    if text.startswith("+"):
        text = text[1:]
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid financial value: {value!r}") from exc
    if negative:
        parsed = -parsed
    if not parsed.is_finite():
        raise ValueError(f"invalid non-finite financial value: {value!r}")
    return parsed


def parse_source_number(raw: str) -> tuple[Decimal | None, str]:
    """Parse a token and return ``(value, sign)``.

    ``sign`` is retained separately for callers that need to explain that a
    parenthesized statement amount was reported as a loss or outflow.
    """

    text = str(raw).strip()
    parsed = parse_decimal(text, allow_missing=True)
    if parsed is None:
        return None, "missing"
    if text.startswith("(") and text.endswith(")"):
        return parsed, "parentheses"
    if parsed < 0 or text.lstrip().startswith(("-", *_DASHES)):
        return parsed, "negative"
    if text.lstrip().startswith("+"):
        return parsed, "positive"
    return parsed, "reported"


def normalise_unit(unit: str | None, context: str = "") -> str:
    """Map source unit wording to a small stable vocabulary."""

    raw = f"{unit or ''} {context}".lower().replace(",", "")
    if "basis point" in raw or re.search(r"\bbps?\b", raw):
        return "basis_points"
    if "per loan" in raw or "per account" in raw:
        return "USD_per_loan"
    if "%" in raw or "percent" in raw or "rate" in raw or "ratio" in raw:
        return "percent"
    if re.search(r"\b(?:upb|unpaid principal|principal balance)\b", raw):
        if "million" in raw or re.search(r"\bmm\b", raw):
            return "USD_millions"
        if "billion" in raw or re.search(r"\bbn\b", raw):
            return "USD_billions"
        if "thousand" in raw or re.search(r"\b000s?\b", raw):
            return "USD_thousands"
        return "USD"
    if re.search(r"\b(?:loan|loans|account|accounts|unit|units|borrower|borrowers)\b", raw):
        if "million" in raw or re.search(r"\bmm\b", raw):
            return "loans_millions"
        if "thousand" in raw or re.search(r"\b000s?\b", raw):
            return "loans_thousands"
        return "loans"
    if "billion" in raw or re.search(r"\bbn\b", raw):
        return "USD_billions"
    if "million" in raw or re.search(r"\bmm\b", raw):
        return "USD_millions"
    if "thousand" in raw or re.search(r"\b000s?\b", raw):
        return "USD_thousands"
    if "$" in raw or "dollar" in raw or "revenue" in raw or "income" in raw or "expense" in raw or "value" in raw or "cost" in raw or "liquidity" in raw or "advance" in raw:
        return "USD"
    if "count" in raw or "number of" in raw:
        return "count"
    return str(unit or "unknown") or "unknown"


def currency_for_unit(unit: str) -> str:
    return "USD" if unit.startswith("USD") else ""


def period_kind(period: str | None) -> str:
    """Classify a period for compatibility checks without guessing a date."""

    text = str(period or "unknown").strip().upper()
    if text in {"", "UNKNOWN", "N/A", "NA"}:
        return "unknown"
    if "YTD" in text or "YEAR-TO-DATE" in text or re.search(r"\b[1-9]M\b", text):
        return "ytd"
    if re.search(r"\bQ[1-4]\b", text) or re.search(r"\bFY\b", text):
        return "quarter_or_fy"
    return "other"


def _period_descriptor(value: str | None) -> tuple[str, int | None, int | None]:
    """Return (shape, fiscal year, elapsed quarter/month) for a period label."""

    text = str(value or "unknown").strip().upper().replace(" ", "-")
    quarter = re.search(r"(?:^|[-_/])Q([1-4])(?:$|[-_/])", text)
    year = re.search(r"\b(20\d{2})\b", text)
    fiscal_year = int(year.group(1)) if year else None
    q = int(quarter.group(1)) if quarter else None
    if "YTD" in text or re.search(r"(?:^|[-_/])[369]M(?:$|[-_/])", text):
        return "ytd", fiscal_year, q
    if q is not None:
        return "quarter", fiscal_year, q
    if "FY" in text or "YEAR" in text:
        return "fy", fiscal_year, None
    return "unknown", fiscal_year, None


def same_period_shape(left: str, right: str) -> bool:
    """Return true for comparable durations, allowing QoQ and YoY labels."""

    l_shape, _l_year, l_q = _period_descriptor(left)
    r_shape, _r_year, r_q = _period_descriptor(right)
    if l_shape == "unknown" or r_shape == "unknown" or l_shape != r_shape:
        return False
    # Cumulative amounts must cover the same number of months.  A Q2 YTD and a
    # Q1 YTD amount are different durations even though both are YTD.
    if l_shape == "ytd" and l_q is not None and r_q is not None:
        return l_q == r_q
    return True


def scope_for(label: str, context: str = "", metric: str = "") -> tuple[str, str, bool]:
    """Classify business scope and whether a line is safe for servicing facts."""

    text = f"{label} {context} {metric}".lower()
    # Broad consolidated/company-wide labels remain out of scope even when a
    # sentence happens to mention a servicing business.
    if re.search(r"\b(?:consolidated|total\s+company|company[- ]?wide|company[- ]?wide)\b", text):
        return "bankwide", "consolidated or broader company result", False
    # Keep the narrower scopes explicit.  They are never silently collapsed into
    # a generic servicing population.
    if "subservic" in text:
        return "servicing_subservicing", "subservicing portfolio or economics", True
    if "owned msr" in text or "owned mortgage servicing" in text or "mortgage servicing rights" in text and "owned" in text:
        return "servicing_owned_msr", "owned mortgage servicing rights", True
    if "servicing for others" in text or "servicing for third parties" in text or "loans serviced for others" in text:
        return "servicing_for_others", "servicing performed for others", True
    if "bank-owned loans serviced" in text or "owned loans serviced" in text:
        return "servicing_owned", "bank-owned loans serviced", True
    if re.search(r"mortgage\s+servicing\s+rights|\bMSRs?\b", text, re.I):
        return "servicing_owned_msr", "mortgage servicing rights", True
    if "mortgage servicing" in text or re.search(r"\bservic(?:e|ed|ing|er|ers)\b", text):
        # Explicit production/origination words override a generic surrounding
        # sentence so mortgage banking output cannot be mislabeled.
        if re.search(r"origination|production|mortgage banking|loan sale|gain on sale", text):
            return "mortgage_origination", "mortgage production/origination", False
        return "servicing", "mortgage servicing", True
    if re.search(r"origination|production|mortgage banking|loan sale|gain on sale", text):
        return "mortgage_origination", "mortgage production/origination", False
    if re.search(r"consolidated|total company|company-wide|companywide|net interest|provision for credit|consumer banking|commercial banking|total assets", text):
        return "bankwide", "consolidated or broader company result", False
    if re.search(r"\b(?:net income|total revenue|total expense|revenue|expense|profit)\b", text):
        return "ambiguous_broader_mortgage", "broader result without servicing scope", False
    return "unknown", "scope not identified", False


@dataclass(frozen=True)
class Evidence:
    """One source-supported financial fact or source excerpt."""

    id: str
    document_id: str
    issuer: str
    ticker: str
    metric: str
    value: str
    raw_value: str
    unit: str
    currency: str
    period: str
    scope: str
    definition: str
    location: str
    excerpt: str
    source_url: str
    source_kind: str = ""
    source_title: str = ""
    document_kind: str = ""
    published: str = ""
    sign: str = "reported"
    derivation: str = ""
    inputs: tuple[str, ...] = field(default_factory=tuple)
    status: str = "supported"
    notes: str = ""
    measure_type: str = "unknown"  # flow, stock, rate, count, or unknown
    period_start: str = ""
    period_end: str = ""

    @property
    def decimal_value(self) -> Decimal:
        parsed = parse_decimal(self.value)
        assert parsed is not None
        return parsed

    @property
    def period_type(self) -> str:
        return period_kind(self.period)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["inputs"] = list(self.inputs)
        result["decimal_value"] = self.value
        result["period_type"] = self.period_type
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Evidence":
        data = dict(value)
        data.pop("decimal_value", None)
        data.pop("period_type", None)
        data["inputs"] = tuple(data.get("inputs") or ())
        return cls(**data)


# A short alias is convenient for callers that call source facts "facts".
Fact = Evidence


def evidence_id(document_id: str, metric: str, raw_value: str, location: str, period: str) -> str:
    digest = hashlib.sha256("|".join(map(str, (document_id, metric, raw_value, location, period))).encode("utf-8")).hexdigest()
    return digest[:24]


def _compatibility_error(current: Evidence, prior: Evidence) -> str | None:
    if current.status != "supported" or prior.status != "supported":
        return "unsupported evidence"
    if current.ticker and prior.ticker and current.ticker.upper() != prior.ticker.upper():
        return "issuer mismatch"
    if current.currency and prior.currency and current.currency.upper() != prior.currency.upper():
        return "currency mismatch"
    if current.metric != prior.metric:
        return "metric mismatch"
    if current.unit != prior.unit:
        return "unit mismatch"
    if current.scope != prior.scope:
        return "business scope mismatch"
    if current.period == prior.period:
        return "same period"
    if not same_period_shape(current.period, prior.period):
        return "period mismatch"
    # GAAP and adjusted figures must remain distinct.  The extractor encodes the
    # distinction in metric/scope where possible, while this catches custom facts.
    if _basis_signature(current) != _basis_signature(prior):
        return "GAAP/adjusted mismatch"
    if current.measure_type != "unknown" and prior.measure_type != "unknown" and current.measure_type != prior.measure_type:
        return "measure type mismatch"
    # Definitions can use issuer-specific wording, so compare the population
    # terms that determine whether two values describe the same denominator.
    if not _definitions_compatible(current, prior):
        return "population/definition mismatch"
    return None


def _definitions_compatible(current: Evidence, prior: Evidence) -> bool:
    """Compare denominator/population and accounting-basis hints."""

    population_terms = (
        "owned", "others", "third", "subservic", "upb", "unpaid principal", "loan", "account", "portfolio",
        "residential", "commercial", "held for sale", "mortgage", "before msr", "including valuation", "excluding valuation",
    )
    c_definition = current.definition.lower()
    p_definition = prior.definition.lower()
    c_terms = {term for term in population_terms if term in c_definition}
    p_terms = {term for term in population_terms if term in p_definition}
    if c_terms != p_terms and (c_terms or p_terms):
        return False
    # Presence alone does not distinguish a population that includes a class
    # from one that explicitly excludes it.  Keep these qualifiers in the
    # compatibility signature so arithmetic cannot compare unlike bases.
    def qualifier_signature(value: str) -> set[str]:
        signature: set[str] = set()
        if re.search(r"\b(?:includ(?:e|es|ed|ing)|inclusion|inclusive)\b", value):
            signature.add("include")
        if re.search(r"\b(?:exclud(?:e|es|ed|ing)|exclusion|exclusive)\b|\bwithout\b", value):
            signature.add("exclude")
        if re.search(r"\bbefore\b", value):
            signature.add("before")
        if re.search(r"\bafter\b", value):
            signature.add("after")
        return signature

    c_qualifiers = qualifier_signature(c_definition)
    p_qualifiers = qualifier_signature(p_definition)
    if c_qualifiers != p_qualifiers and (c_qualifiers or p_qualifiers):
        return False
    return _basis_signature(current) == _basis_signature(prior)


def _basis_signature(fact: Evidence) -> str:
    """Capture accounting/valuation basis hints used for like-for-like checks."""

    text = f"{fact.metric} {fact.definition}".lower()
    if "non-gaap" in text or "adjusted" in text or "excluding valuation" in text:
        return "adjusted_or_excluding_valuation"
    if "gaap" in text or "including valuation" in text or "before msr valuation" in text:
        return "gaap_or_including_valuation"
    return "unstated"


def derive_absolute_change(current: Evidence, prior: Evidence, *, period: str | None = None) -> Evidence | None:
    """Derive a like-for-like absolute change, retaining exact Decimal scale."""

    reason = _compatibility_error(current, prior)
    if reason:
        return None
    value = current.decimal_value - prior.decimal_value
    label = f"{current.metric} absolute change"
    result_period = period or current.period
    return Evidence(
        id=evidence_id(current.document_id, label, decimal_string(value), current.location, result_period),
        document_id=current.document_id,
        issuer=current.issuer,
        ticker=current.ticker,
        metric=label,
        value=decimal_string(value),
        raw_value=decimal_string(value),
        unit=current.unit,
        currency=current.currency,
        period=result_period,
        scope=current.scope,
        definition=f"Absolute change: current {current.value} minus prior {prior.value}",
        location=f"derived from {current.id} and {prior.id}",
        excerpt=f"Derived as {current.value} - {prior.value} = {decimal_string(value)} {current.unit}.",
        source_url=current.source_url,
        source_kind=current.source_kind,
        source_title=current.source_title,
        document_kind=current.document_kind,
        published=current.published,
        sign="derived",
        derivation="absolute_change",
        inputs=(current.id, prior.id),
        notes="Derived figure; inspect the two cited source facts for definitions and periods.",
    )


def derive_rate_change(current: Evidence, prior: Evidence, *, basis_points: bool = False) -> Evidence | None:
    """Derive a percentage-point or basis-point change for compatible rates."""

    if current.unit not in {"percent", "basis_points"} or prior.unit != current.unit:
        return None
    reason = _compatibility_error(current, prior)
    if reason:
        return None
    raw_delta = current.decimal_value - prior.decimal_value
    if basis_points and current.unit == "percent":
        delta = raw_delta * Decimal("100")
        unit = "basis_points"
    else:
        delta = raw_delta
        unit = current.unit
    label = f"{current.metric} {'basis-point' if unit == 'basis_points' else 'percentage-point'} change"
    return Evidence(
        id=evidence_id(current.document_id, label, decimal_string(delta), current.location, current.period),
        document_id=current.document_id,
        issuer=current.issuer,
        ticker=current.ticker,
        metric=label,
        value=decimal_string(delta),
        raw_value=decimal_string(delta),
        unit=unit,
        currency="",
        period=current.period,
        scope=current.scope,
        definition=f"Current {current.value} minus prior {prior.value}",
        location=f"derived from {current.id} and {prior.id}",
        excerpt=f"Derived as {current.value} - {prior.value} = {decimal_string(delta)} {unit}.",
        source_url=current.source_url,
        source_kind=current.source_kind,
        source_title=current.source_title,
        document_kind=current.document_kind,
        published=current.published,
        sign="derived",
        derivation="rate_change",
        inputs=(current.id, prior.id),
    )


def derive_standalone_quarter(current_ytd: Evidence, prior_ytd: Evidence, *, quarter_period: str) -> Evidence | None:
    """Derive a standalone flow quarter from compatible cumulative YTD amounts.

    Stocks, rates, percentages, and margins are intentionally rejected.  The
    caller must pass explicit YTD labels; this function never infers them from
    filing dates.
    """

    safe_flow_metrics = {
        "servicing_fee_income", "servicing_operating_expense", "servicing_pretax_income",
        "adjusted_servicing_result", "servicing_interest_expense", "servicing_valuation_related_items",
        "servicing_expenses_excluding_valuation", "msr_cash_flow_realization", "msr_amortization",
        "msr_fair_value_change", "msr_hedge_result", "residential_servicing_income_before_msr_valuation",
        "residential_msr_valuation", "residential_servicing_income",
    }
    if current_ytd.metric not in safe_flow_metrics:
        return None
    if current_ytd.measure_type not in {"unknown", "flow"} or prior_ytd.measure_type not in {"unknown", "flow"}:
        return None
    if current_ytd.unit in {"percent", "basis_points", "loans", "count", "loans_millions", "loans_thousands"}:
        return None
    if period_kind(current_ytd.period) != "ytd" or period_kind(prior_ytd.period) != "ytd":
        return None
    if current_ytd.metric != prior_ytd.metric or current_ytd.unit != prior_ytd.unit or current_ytd.scope != prior_ytd.scope or current_ytd.currency != prior_ytd.currency:
        return None
    if not _definitions_compatible(current_ytd, prior_ytd):
        return None
    current_shape, current_year, current_q = _period_descriptor(current_ytd.period)
    prior_shape, prior_year, prior_q = _period_descriptor(prior_ytd.period)
    target_shape, target_year, target_q = _period_descriptor(quarter_period)
    # A valid subtraction is Qn YTD minus Q(n-1) YTD within one fiscal year.
    # This rejects cross-year subtraction and 9M-minus-3M/other non-adjacent
    # intervals.  Fiscal-year start/end metadata, when supplied, must agree.
    if (current_shape, prior_shape, target_shape) != ("ytd", "ytd", "quarter"):
        return None
    if current_year is None or prior_year is None or target_year is None or current_year != prior_year or current_year != target_year:
        return None
    if current_q is None or prior_q is None or target_q is None or current_q != target_q or prior_q != current_q - 1:
        return None
    if bool(current_ytd.period_start) != bool(prior_ytd.period_start) or (current_ytd.period_start and current_ytd.period_start != prior_ytd.period_start):
        return None
    if bool(current_ytd.period_end) != bool(prior_ytd.period_end):
        return None
    if current_ytd.period_end and prior_ytd.period_end and current_ytd.period_end == prior_ytd.period_end:
        return None
    if _basis_signature(current_ytd) != _basis_signature(prior_ytd):
        return None
    value = current_ytd.decimal_value - prior_ytd.decimal_value
    label = f"{current_ytd.metric} standalone quarter"
    return Evidence(
        id=evidence_id(current_ytd.document_id, label, decimal_string(value), current_ytd.location, quarter_period),
        document_id=current_ytd.document_id,
        issuer=current_ytd.issuer,
        ticker=current_ytd.ticker,
        metric=label,
        value=decimal_string(value),
        raw_value=decimal_string(value),
        unit=current_ytd.unit,
        currency=current_ytd.currency,
        period=quarter_period,
        scope=current_ytd.scope,
        definition=f"Standalone flow derived as current YTD {current_ytd.value} minus prior YTD {prior_ytd.value}",
        location=f"derived from {current_ytd.id} and {prior_ytd.id}",
        excerpt=f"Derived as {current_ytd.value} - {prior_ytd.value} = {decimal_string(value)} {current_ytd.unit}.",
        source_url=current_ytd.source_url,
        source_kind=current_ytd.source_kind,
        source_title=current_ytd.source_title,
        document_kind=current_ytd.document_kind,
        published=current_ytd.published,
        sign="derived",
        derivation="standalone_quarter_from_ytd",
        inputs=(current_ytd.id, prior_ytd.id),
        measure_type="flow",
    )


def evidence_json(evidence: Iterable[Evidence | Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [item.to_dict() if isinstance(item, Evidence) else Evidence.from_dict(item).to_dict() for item in evidence]


def group_by_metric(evidence: Iterable[Evidence]) -> dict[str, list[Evidence]]:
    result: dict[str, list[Evidence]] = {}
    for item in evidence:
        result.setdefault(item.metric, []).append(item)
    return result


def compatible(current: Evidence, prior: Evidence) -> bool:
    return _compatibility_error(current, prior) is None
