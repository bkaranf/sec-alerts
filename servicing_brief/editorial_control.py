"""Offline quality gate for source-backed earnings-call editorial insights.

The gate checks provenance and editorial bookkeeping.  It does not fetch a
source or decide whether a paraphrase is semantically faithful; that remains a
writer and reviewer judgment supported by the required excerpt and rationale.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
import hashlib
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse


__all__ = ["validate_source_insights"]


_AVAILABLE_DISPOSITIONS = {"insight_added", "no_material_incremental_insight"}
_INSIGHT_TYPES = {"outlook", "reported_result", "management_explanation", "strategy"}

# These are intentionally a small editorial screen, not a semantic classifier.
# A useful call note normally contains at least one concrete change, result,
# explanation, or outlook marker.  The required source excerpt and human
# rationale carry the substantive review burden.
_SUBSTANTIVE_MARKER = re.compile(
    r"\b(?:expect(?:s|ed)?|outlook|stable|grew|growth|increas(?:e|ed|ing)|"
    r"decreas(?:e|ed|ing)|declin(?:e|ed|ing)|rose|fell|competition|competitive|"
    r"pricing|originations?|margin(?:s)?|renewals?|renewal|extend(?:s|ed|ing)?|"
    r"chatbot|strategy|strategic|loss(?:es)?|provisions?|credit|payment(?:s)?|"
    r"lending|balance(?:s)?|tailwind(?:s)?|expansion|improv(?:e|ed|ing)|eas(?:e|ed|ing)|"
    r"delinquenc(?:y|ies)|impaired|revenue|cost(?:s)?|profit|portfolio|"
    r"prepayment(?:s)?|rate(?:s)?|risk|operating)\b",
    re.IGNORECASE,
)
_PROCEDURAL_MARKER = re.compile(
    r"\b(?:review(?:ed)?|available|unavailable|found|archive|archived|"
    r"transcript(?:s)?|prepared\s+remarks|speaker(?:s)?|quote|webcast|"
    r"call|page|source|document|note(?:s)?|schedule|media|attributed)\b",
    re.IGNORECASE,
)


def _words(value: str) -> list[str]:
    return re.findall(r"\b[\w’$%+]+(?:[-][\w]+)*\b", value, flags=re.UNICODE)


def _is_procedural_only(value: str) -> bool:
    """Return whether text has no concrete editorial insight marker.

    This deliberately errs toward rejection for ``insight_added``.  It is a
    structured check against availability prose, not a claim that a heuristic
    can prove the quality or truth of a paraphrase.
    """

    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return True
    if not _SUBSTANTIVE_MARKER.search(text):
        return True
    # A source note containing only procedural clauses plus a generic source
    # noun is still procedural.  Concrete markers above are required outside
    # those clauses; this clause catches phrases such as "reviewed transcript
    # and no breakout appeared" without rejecting a real outlook sentence.
    sentences = [part.strip() for part in re.split(r"[.!?]+", text) if part.strip()]
    for sentence in sentences:
        if _SUBSTANTIVE_MARKER.search(sentence) and not (
            _PROCEDURAL_MARKER.search(sentence)
            and re.search(r"\b(?:no|not|without|never)\b", sentence, re.IGNORECASE)
        ):
            return False
    return True


def _meaningful_reason(value: Any, field: str, ticker: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ticker}: {field} is required")
    text = re.sub(r"\s+", " ", value).strip()
    if len(_words(text)) < 6:
        raise ValueError(f"{ticker}: {field} must explain the reviewer judgment")
    return text


def _required_text(record: Mapping[str, Any], field: str, ticker: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ticker}: {field} is required")
    return value.strip()


def _source_url(record: Mapping[str, Any], ticker: str) -> str:
    source_url = record.get("source_url")
    legacy_url = record.get("url")
    if source_url is not None and legacy_url is not None and source_url != legacy_url:
        raise ValueError(f"{ticker}: source_url and url disagree")
    value = source_url if source_url is not None else legacy_url
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{ticker}: source_url is required")
    value = value.strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc or any(ch.isspace() for ch in value):
        raise ValueError(f"{ticker}: source_url must be a public HTTPS URL")
    return value


def _source_hash(record: Mapping[str, Any], ticker: str) -> str:
    supplied = record.get("sha256")
    legacy = record.get("hash")
    if supplied is not None and legacy is not None and supplied != legacy:
        raise ValueError(f"{ticker}: sha256 and hash disagree")
    value = supplied if supplied is not None else legacy
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()):
        raise ValueError(f"{ticker}: source hash must be a 64-character SHA-256 value")
    return value.strip().lower()


def _check_file_hash(record: Mapping[str, Any], ticker: str, *, required: bool) -> tuple[str, str]:
    raw_path = record.get("source_path", "")
    if raw_path is None:
        raw_path = ""
    if not isinstance(raw_path, str):
        raise ValueError(f"{ticker}: source_path must be text")
    raw_path = raw_path.strip()
    expected = _source_hash(record, ticker)
    if not raw_path:
        if required:
            raise ValueError(f"{ticker}: source_path is required for available material")
        if expected:
            raise ValueError(f"{ticker}: a source hash requires source_path")
        return "", ""
    if not expected:
        raise ValueError(f"{ticker}: source hash is required when source_path is supplied")
    path = Path(raw_path)
    if not path.is_file():
        raise ValueError(f"{ticker}: source_path does not point to a file: {raw_path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected:
        raise ValueError(f"{ticker}: source hash does not match source_path")
    return raw_path, actual


def _record_summary(record: Mapping[str, Any], source_hash: str) -> dict[str, str]:
    ticker = str(record["ticker"])
    return {
        "ticker": ticker,
        "availability": str(record["availability"]),
        "disposition": str(record["disposition"]),
        "source_id": str(record["source_id"]),
        "sha256": source_hash,
    }


def _validate_document_review(record: Mapping[str, Any], ticker: str, disposition: str) -> None:
    """Validate v2 passage adjudication against the record's verified file."""

    review = record.get("document_review")
    if not isinstance(review, Mapping):
        raise ValueError(f"{ticker}: document_review is required for available v2 material")
    scope = review.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError(f"{ticker}: document_review.scope is required")
    passages = review.get("passages")
    if not isinstance(passages, list) or not passages:
        raise ValueError(f"{ticker}: document_review.passages must be a non-empty list")
    decisions: list[str] = []
    for index, passage in enumerate(passages):
        if not isinstance(passage, Mapping):
            raise ValueError(f"{ticker}: document_review.passages[{index}] must be an object")
        prefix = f"{ticker}: document_review.passages[{index}]"
        for field in ("location", "evidence_excerpt", "reader_value_reason"):
            value = passage.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{prefix}.{field} is required")
        decision = passage.get("decision")
        if decision not in {"include", "exclude"}:
            raise ValueError(f"{prefix}.decision must be include or exclude")
        decisions.append(decision)
    if disposition == "no_material_incremental_insight" and any(item != "exclude" for item in decisions):
        raise ValueError(f"{ticker}: no-material disposition requires every document passage to be excluded")
    if disposition == "insight_added" and not any(item == "include" for item in decisions):
        raise ValueError(f"{ticker}: insight_added requires at least one included document passage")


def validate_source_insights(control: dict, published_calls: Mapping[str, str]) -> dict[str, Any]:
    """Validate a source-insight control record against published call text.

    ``control`` is an offline record with ``version: 1`` or ``version: 2``
    and a ``companies`` list.  Each company supplies provenance, an
    availability/disposition, source excerpt, and either ``why_it_matters``
    or ``documented_reason_for_no_increment``.  Version 2 available records
    also carry explicit passage adjudications in ``document_review``.
    ``published_calls`` maps each ticker to the exact call text published by
    the editorial layer.

    The returned report is suitable for an audit log.  Any failed provenance,
    bookkeeping, or procedural-only insight check raises ``ValueError``.
    No network request or rendering action is performed.
    """

    if not isinstance(control, dict):
        raise ValueError("control must be a dict")
    version = control.get("version")
    if version not in {1, 2}:
        raise ValueError("control.version must be 1 or 2")
    if not isinstance(published_calls, Mapping):
        raise ValueError("published_calls must be a mapping of ticker to editorial call text")
    companies = control.get("companies")
    if not isinstance(companies, Sequence) or isinstance(companies, (str, bytes)) or not companies:
        raise ValueError("control.companies must be a non-empty list")

    control_tickers = []
    for position, raw_record in enumerate(companies):
        if not isinstance(raw_record, Mapping) or not isinstance(raw_record.get("ticker"), str):
            raise ValueError(f"companies[{position}] must supply a ticker")
        control_tickers.append(raw_record["ticker"].strip())
    published_tickers = {str(ticker).strip() for ticker in published_calls}
    if set(control_tickers) != published_tickers:
        raise ValueError("control.companies ticker set must match published_calls")

    summaries: list[dict[str, str]] = []
    seen: set[str] = set()
    for position, raw_record in enumerate(companies):
        if not isinstance(raw_record, Mapping):
            raise ValueError(f"companies[{position}] must be an object")
        record = raw_record
        ticker = _required_text(record, "ticker", f"companies[{position}]")
        if ticker in seen:
            raise ValueError(f"{ticker}: duplicate ticker")
        seen.add(ticker)

        availability = _required_text(record, "availability", ticker)
        disposition = _required_text(record, "disposition", ticker)
        if availability not in {"available", "unavailable"}:
            raise ValueError(f"{ticker}: availability must be available or unavailable")
        if availability == "available" and disposition not in _AVAILABLE_DISPOSITIONS:
            raise ValueError(f"{ticker}: invalid disposition for available material")
        if availability == "unavailable" and disposition != "unavailable":
            raise ValueError(f"{ticker}: unavailable material must have disposition unavailable")

        source_id = _required_text(record, "source_id", ticker)
        source_url = _source_url(record, ticker)
        location = _required_text(record, "location", ticker)
        evidence_excerpt = _required_text(record, "evidence_excerpt", ticker)
        raw_published_text = record.get("published_text", "")
        if raw_published_text is None:
            raw_published_text = ""
        if not isinstance(raw_published_text, str):
            raise ValueError(f"{ticker}: published_text must be text")
        published_text = raw_published_text.strip()
        if ticker not in published_calls or not isinstance(published_calls[ticker], str):
            raise ValueError(f"{ticker}: published call text is missing")
        if published_text != published_calls[ticker].strip():
            raise ValueError(f"{ticker}: published_text does not match actual rendered call text")
        if disposition in {"no_material_incremental_insight", "unavailable"} and published_text:
            raise ValueError(f"{ticker}: published_text must be empty for {disposition}")

        # Available source files must be byte-verified.  An unavailable call
        # may point to an archived official results page, or may honestly have
        # no local file/hash when only the public discovery URL was retained.
        _path, actual_hash = _check_file_hash(
            record,
            ticker,
            required=availability == "available",
        )
        if version == 2 and availability == "available":
            _validate_document_review(record, ticker, disposition)

        if availability == "available" and disposition == "insight_added":
            if not published_text:
                raise ValueError(f"{ticker}: published_text is required for an added insight")
            insight_type = _required_text(record, "insight_type", ticker)
            if insight_type not in _INSIGHT_TYPES:
                raise ValueError(f"{ticker}: unsupported insight_type")
            why = _meaningful_reason(record.get("why_it_matters"), "why_it_matters", ticker)
            if _is_procedural_only(why):
                raise ValueError(f"{ticker}: why_it_matters must state investor relevance")
            if _is_procedural_only(published_text):
                raise ValueError(f"{ticker}: published insight is purely procedural")
            # A non-empty, human-written source excerpt is mandatory.  The
            # gate intentionally does not claim to prove paraphrase semantics.
            if len(evidence_excerpt.strip()) < 8:
                raise ValueError(f"{ticker}: evidence_excerpt is too short")
            _ = why
        elif availability == "available" and disposition == "no_material_incremental_insight":
            _meaningful_reason(
                record.get("documented_reason_for_no_increment"),
                "documented_reason_for_no_increment",
                ticker,
            )
        else:
            reason = _meaningful_reason(
                record.get("documented_reason_for_no_increment"),
                "documented_reason_for_no_increment",
                ticker,
            )
            if record.get("insight_type") not in (None, ""):
                raise ValueError(f"{ticker}: unavailable material cannot carry insight_type")
            if record.get("why_it_matters") not in (None, ""):
                raise ValueError(f"{ticker}: unavailable material cannot carry why_it_matters")
            _ = reason

        summaries.append(_record_summary({**record, "source_id": source_id, "source_url": source_url}, actual_hash))

    disposition_counts = Counter(item["disposition"] for item in summaries)
    return {
        "valid": True,
        "checked": len(summaries),
        "dispositions": dict(disposition_counts),
        "records": summaries,
        "human_judgment_required": True,
        "method": "Structured provenance and editorial bookkeeping gate; excerpt fidelity and investor relevance still require human review.",
    }
