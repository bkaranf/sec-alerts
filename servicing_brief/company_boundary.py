"""Identity and earnings-event boundaries for normal company briefs.

The normal release unit is one reporting entity and one earnings event.  This
module keeps that rule independent of the prose renderer: issuer identity is
read from structured report metadata and a small HTML marker, while the plain
text alternative carries only a readable ticker and period header.  No check
tries to infer a company from a quoted passage or a source title.

The older five-company review package has its own explicit release adapter and
does not use this normal-brief contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Any

from bs4 import BeautifulSoup


COMPANY_IDENTITY_KEY = "company_identity"
IDENTITY_HEADER = "X-Servicing-Brief-Identity"
BOUNDARY_HEADER = "X-Servicing-Brief-Boundary"
TEST_HEADER = "X-Servicing-Brief-Test"
BOUNDARY_VERSION = "company-v1"
HTML_COMPANY_ATTRIBUTE = "data-brief-company"
HTML_CIK_ATTRIBUTE = "data-brief-cik"
HTML_EVENT_ATTRIBUTE = "data-brief-event"

_TICKER = re.compile(r"[A-Z][A-Z0-9.-]{0,14}\Z")
_CIK = re.compile(r"[0-9]{1,10}\Z")
_IDENTITY_FIELD_NAMES = ("ticker", "cik", "event")
_IDENTITY_CONTAINER_NAMES = ("company_identity", "identity", "reporting_entity")
_RELEASE_KINDS = {"release", "earnings_release", "earnings-release", "press_release", "press-release"}
_FILING_KINDS = {"8-k", "8-k/a", "8k", "8k/a"}
_CONTEXT_METADATA_KEYS = ("context", "is_context", "explicit_context", "reporting_context")


class CompanyBoundaryError(ValueError):
    """Raised when a normal brief has no unambiguous company boundary."""


@dataclass(frozen=True)
class BriefIdentity:
    """Canonical identity for a normal company earnings brief."""

    ticker: str
    cik: str
    event: str
    kind: str = "earnings_brief"

    @property
    def event_key(self) -> str:
        """Return the event key used for Q4/FY equivalence checks."""

        return _event_key(self.event)

    def to_dict(self) -> dict[str, str]:
        return {"ticker": self.ticker, "cik": self.cik, "event": self.event, "kind": self.kind}

    def token(self) -> str:
        """Return the stable value used in the MIME identity header."""

        return f"ticker={self.ticker}; cik={self.cik}; event={self.event}"


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_cik(value: Any) -> str:
    """Normalize a SEC CIK, rejecting blank, nonnumeric, and oversized values."""

    if isinstance(value, bool) or value is None:
        raise CompanyBoundaryError("company boundary requires a numeric CIK")
    raw = str(value).strip()
    if not _CIK.fullmatch(raw):
        raise CompanyBoundaryError("company boundary CIK must contain one to ten digits")
    if int(raw) == 0:
        raise CompanyBoundaryError("company boundary CIK must be nonzero")
    return raw.zfill(10)


def normalize_ticker(value: Any) -> str:
    """Normalize a public ticker, rejecting blank or malformed values."""

    if isinstance(value, bool) or value is None:
        raise CompanyBoundaryError("company boundary requires a ticker")
    raw = str(value).strip().upper()
    if not _TICKER.fullmatch(raw):
        raise CompanyBoundaryError("company boundary ticker is blank or invalid")
    return raw


def canonical_event(value: Any) -> str:
    """Parse a quarter/FY label into ``YYYY-Qn`` or ``YYYY-FY``.

    Source periods are authoritative.  This parser only accepts explicit
    quarter/year or fiscal-year labels; it never derives an event from a
    publication timestamp.
    """

    if value is None:
        raise CompanyBoundaryError("company boundary requires an earnings event")
    raw = re.sub(r"\s+", " ", str(value).strip().upper())
    if not raw or raw.casefold() in {"unknown", "n/a", "na", "none"}:
        raise CompanyBoundaryError("company boundary earnings event is blank or unknown")

    # Accept the machine form used by source documents and the readable form
    # used by the plain-text header (Q3 FY2026, Q3 2026, FY2026 Q3, etc.).
    patterns = (
        (r"^(20\d{2})[- /]?Q([1-4])$", lambda m: f"{m[1]}-Q{m[2]}"),
        (r"^Q([1-4])(?:\s*FY)?\s*(20\d{2})$", lambda m: f"{m[2]}-Q{m[1]}"),
        (r"^FY\s*(20\d{2})\s*Q([1-4])$", lambda m: f"{m[1]}-Q{m[2]}"),
        (r"^(20\d{2})\s*FY\s*Q([1-4])$", lambda m: f"{m[1]}-Q{m[2]}"),
        (r"^(?:FY\s*)?(20\d{2})(?:\s*[-/]?\s*FY)?$", lambda m: f"{m[1]}-FY"),
        (r"^FY\s*(20\d{2})$", lambda m: f"{m[1]}-FY"),
    )
    for pattern, formatter in patterns:
        match = re.fullmatch(pattern, raw)
        if match:
            return formatter(match)
    raise CompanyBoundaryError("company boundary earnings event must be an explicit quarter or fiscal year")


def _event_key(value: str) -> str:
    # A Q4 earnings release and its annual FY disclosure describe one
    # reporting event.  Keep the report's readable event value while using a
    # shared key for this equivalence only.
    return value[:-3] + "-Q4" if value.endswith("-FY") else value


def _events_match(left: Any, right: Any) -> bool:
    try:
        return _event_key(canonical_event(left)) == _event_key(canonical_event(right))
    except CompanyBoundaryError:
        return False


def _identity_from_mapping(raw: Mapping[str, Any], *, source: str, require_kind: bool) -> BriefIdentity:
    missing = [name for name in _IDENTITY_FIELD_NAMES if raw.get(name) in (None, "")]
    if missing:
        raise CompanyBoundaryError(f"{source} is missing required field(s): {', '.join(missing)}")
    if require_kind and "kind" not in raw:
        raise CompanyBoundaryError(f"{source}.kind must be explicitly 'earnings_brief'")
    kind = str(raw.get("kind", "") or "").strip() or "earnings_brief"
    if require_kind and kind != "earnings_brief":
        raise CompanyBoundaryError(f"{source}.kind must be 'earnings_brief'")
    if kind != "earnings_brief":
        raise CompanyBoundaryError(f"{source}.kind must be 'earnings_brief'")
    return BriefIdentity(
        ticker=normalize_ticker(raw.get("ticker")),
        cik=normalize_cik(raw.get("cik")),
        event=canonical_event(raw.get("event")),
        kind=kind,
    )


def has_company_identity(report: Mapping[str, Any] | None) -> bool:
    """Return whether a report opts into the strict normal-brief contract."""

    if not isinstance(report, Mapping):
        return False
    if any(name in report for name in _IDENTITY_CONTAINER_NAMES):
        return True
    return any(name in report for name in _IDENTITY_FIELD_NAMES)


def report_identity(report: Mapping[str, Any], *, strict: bool = True) -> BriefIdentity:
    """Read one structured identity from a report.

    ``company_identity`` is the only schema accepted by strict publication
    paths.  The two aliases and top-level fields remain available to an
    explicit non-strict archival reader, but never to prepare/send.  No value
    is obtained from report prose.
    """

    if not isinstance(report, Mapping):
        raise CompanyBoundaryError("company boundary report must be an object")
    candidates: list[tuple[str, Mapping[str, Any]]] = []
    if strict:
        # A normal publication has one explicit schema owner.  The historical
        # aliases remain parseable only for callers that deliberately request
        # ``strict=False``; they must never become a prepare/send bypass.
        if COMPANY_IDENTITY_KEY not in report:
            raise CompanyBoundaryError("normal brief requires company_identity metadata")
        value = report.get(COMPANY_IDENTITY_KEY)
        if not isinstance(value, Mapping):
            raise CompanyBoundaryError("company_identity must be an object")
        candidates.append((COMPANY_IDENTITY_KEY, value))
    else:
        for name in _IDENTITY_CONTAINER_NAMES:
            if name not in report:
                continue
            value = report.get(name)
            if not isinstance(value, Mapping):
                raise CompanyBoundaryError(f"{name} must be an object")
            candidates.append((name, value))
        top_level = {name: report.get(name) for name in _IDENTITY_FIELD_NAMES if name in report}
        if top_level:
            candidates.append(("report", top_level))
        if not candidates:
            raise CompanyBoundaryError("normal brief requires company_identity metadata")

    identities = [
        _identity_from_mapping(candidate, source=name, require_kind=strict)
        for name, candidate in candidates
    ]
    first = identities[0]
    for other in identities[1:]:
        if first.cik != other.cik or first.ticker != other.ticker or not _events_match(first.event, other.event):
            raise CompanyBoundaryError("report contains conflicting company identities")
    return first


def _document_ticker(document: Any) -> str:
    metadata = _field(document, "metadata", {})
    if isinstance(metadata, Mapping) and metadata.get("ticker") not in (None, ""):
        return normalize_ticker(metadata.get("ticker"))
    direct = _field(document, "ticker", "")
    if direct not in (None, ""):
        return normalize_ticker(direct)
    raise CompanyBoundaryError("source document is missing a verified ticker")


def _document_event(document: Any) -> str | None:
    raw = _field(document, "period", "")
    if raw in (None, "") or str(raw).strip().casefold() in {"unknown", "n/a", "na"}:
        return None
    return canonical_event(raw)


def _document_kind(document: Any) -> str:
    return str(_field(document, "kind", "") or "").strip().casefold().replace("_", "-")


def _document_is_context(document: Any) -> bool:
    metadata = _field(document, "metadata", {})
    if not isinstance(metadata, Mapping):
        return False
    for key in _CONTEXT_METADATA_KEYS:
        value = metadata.get(key)
        if value is True or str(value).strip().casefold() in {"context", "supporting", "annual_context", "prior_period"}:
            return True
    return str(metadata.get("role", "") or "").strip().casefold() in {"context", "supporting_context", "prior_period"}


def validate_source_documents(
    documents: Iterable[Any],
    identity: BriefIdentity | Mapping[str, Any] | None = None,
    *,
    context_documents: Iterable[Any] = (),
    require_event: bool = True,
) -> dict[str, Any]:
    """Validate source issuer identity and event grouping.

    Primary documents must belong to the identity's event unless explicitly
    marked as context.  Context documents can carry a prior period or unknown
    period, but must retain the same issuer CIK and ticker.  This preserves
    useful annual/prior-period context without allowing an accidental second
    earnings package through the boundary.
    """

    primary = list(documents)
    contexts = list(context_documents)
    expected: BriefIdentity | None
    if identity is None:
        expected = None
    elif isinstance(identity, BriefIdentity):
        expected = identity
    elif isinstance(identity, Mapping):
        expected = _identity_from_mapping(identity, source="company_identity", require_kind=False)
    else:
        raise CompanyBoundaryError("company boundary identity must be an object")

    all_documents = [(item, False) for item in primary] + [(item, True) for item in contexts]
    if not all_documents:
        if expected is None and require_event:
            raise CompanyBoundaryError("normal brief requires at least one source document")
        return {"identity": expected.to_dict() if expected else None, "documents": 0, "context_documents": 0}

    observed_ciks: set[str] = set()
    observed_tickers: set[str] = set()
    normalized: list[tuple[Any, bool, str, str, str | None]] = []
    for document, is_context in all_documents:
        try:
            cik = normalize_cik(_field(document, "cik", ""))
        except CompanyBoundaryError as exc:
            raise CompanyBoundaryError("source document has a blank or invalid CIK") from exc
        ticker = _document_ticker(document)
        event = _document_event(document)
        observed_ciks.add(cik)
        observed_tickers.add(ticker)
        normalized.append((document, is_context or _document_is_context(document), cik, ticker, event))

    if expected is None:
        if len(observed_ciks) != 1 or len(observed_tickers) != 1:
            raise CompanyBoundaryError("source documents must contain exactly one reporting company")
        observed_events = [event for _doc, is_context, _cik, _ticker, event in normalized if event and not is_context]
        if require_event and not observed_events:
            raise CompanyBoundaryError("normal brief has no current earnings-event source")
        expected = BriefIdentity(
            ticker=next(iter(observed_tickers)),
            cik=next(iter(observed_ciks)),
            event="" if not require_event else canonical_event(observed_events[0]),
        )

    for _document, _is_context, cik, ticker, _event in normalized:
        if cik != expected.cik or ticker != expected.ticker:
            raise CompanyBoundaryError("source documents do not match the report's reporting company")

    primary_events: list[str] = []
    for document, is_context, _cik, _ticker, event in normalized:
        if is_context:
            continue
        kind = _document_kind(document)
        if event is None:
            if require_event:
                raise CompanyBoundaryError("primary source document has an unknown earnings event; mark it as explicit context")
            continue
        primary_events.append(event)
        if not _events_match(event, expected.event):
            raise CompanyBoundaryError("primary source document belongs to a different earnings event")
        if kind in _RELEASE_KINDS or kind in _FILING_KINDS:
            if not _events_match(event, expected.event):
                raise CompanyBoundaryError("current primary release does not match the report event")

    # A strict package with only context sources has no current event witness.
    # A report generated from source material must have one; a metadata-only
    # one-off artifact may intentionally pass no Document objects.
    if require_event and not primary_events:
        raise CompanyBoundaryError("normal brief has no current earnings-event source")
    return {
        "identity": expected.to_dict(),
        "documents": len(primary),
        "context_documents": len(contexts),
        "events": sorted({_event_key(item) for item in primary_events}),
    }


def _marker_identity(element: Any) -> BriefIdentity:
    attrs = {
        "ticker": element.get(HTML_COMPANY_ATTRIBUTE),
        "cik": element.get(HTML_CIK_ATTRIBUTE),
        "event": element.get(HTML_EVENT_ATTRIBUTE),
        "kind": "earnings_brief",
    }
    return _identity_from_mapping(attrs, source="HTML company marker", require_kind=False)


def html_identity_marker(html: str) -> BriefIdentity:
    """Parse exactly one structured issuer marker from an HTML surface."""

    if not isinstance(html, str):
        raise CompanyBoundaryError("brief HTML must be text")
    soup = BeautifulSoup(html, "html.parser")
    markers = soup.find_all(attrs={HTML_COMPANY_ATTRIBUTE: True})
    if len(markers) != 1:
        raise CompanyBoundaryError("brief HTML requires exactly one data-brief-company identity marker")
    marker = markers[0]
    for attribute in (HTML_CIK_ATTRIBUTE, HTML_EVENT_ATTRIBUTE):
        matches = soup.find_all(attrs={attribute: True})
        if len(matches) != 1 or matches[0] is not marker:
            raise CompanyBoundaryError(f"brief HTML requires exactly one {attribute} on the issuer marker")
    return _marker_identity(marker)


_TEXT_COMPANY = re.compile(r"^Company:\s*(?P<name>.+?)\s*\((?P<ticker>[A-Z][A-Z0-9.-]{0,14})\)\s*$")
_TEXT_EVENT = re.compile(r"^Earnings period:\s*(?P<event>.+?)\s*$", re.IGNORECASE)


def text_identity_headers(text: str) -> tuple[str, str]:
    """Return the ticker and event represented by the readable text headers."""

    if not isinstance(text, str):
        raise CompanyBoundaryError("brief plaintext must be text")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    company_matches = [(index, _TEXT_COMPANY.fullmatch(line)) for index, line in enumerate(lines)]
    company_matches = [(index, match) for index, match in company_matches if match]
    event_matches = [(index, _TEXT_EVENT.fullmatch(line)) for index, line in enumerate(lines)]
    event_matches = [(index, match) for index, match in event_matches if match]
    if len(company_matches) != 1:
        raise CompanyBoundaryError("brief plaintext requires exactly one Company: ... (TICKER) header")
    if len(event_matches) != 1:
        raise CompanyBoundaryError("brief plaintext requires exactly one Earnings period: header")
    company_index, company_match = company_matches[0]
    event_index, event_match = event_matches[0]
    if company_index > 7 or event_index > 7:
        raise CompanyBoundaryError("company and earnings period headers must appear at the top of plaintext")
    # CIK is intentionally omitted from reader text.  Reject labeled identity
    # fields while allowing SEC CIKs that happen to occur inside source URLs.
    if re.search(r"(?i)\b(?:cik|data[-_ ]brief[-_ ]cik)\s*[:=]", "\n".join(lines[:8])):
        raise CompanyBoundaryError("brief plaintext must keep CIK out of the reader header")
    return company_match["ticker"].upper(), event_match["event"].strip()


def validate_report_surfaces(report: Mapping[str, Any], identity: BriefIdentity | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate HTML and plaintext identity markers against report metadata."""

    expected = identity if isinstance(identity, BriefIdentity) else report_identity(report, strict=True) if identity is None else _identity_from_mapping(identity, source="company_identity", require_kind=False)
    html = report.get("html")
    text = report.get("text")
    if not isinstance(html, str) or not html:
        raise CompanyBoundaryError("normal brief requires an HTML body")
    if not isinstance(text, str) or not text:
        raise CompanyBoundaryError("normal brief requires a plaintext alternative")
    html_identity = html_identity_marker(html)
    if html_identity.cik != expected.cik or html_identity.ticker != expected.ticker or not _events_match(html_identity.event, expected.event):
        raise CompanyBoundaryError("HTML identity marker does not match report metadata")
    # A branded image is another issuer identity assertion.  Repeated logos
    # are allowed because a renderer may repeat them on continuation sections;
    # an unbranded neutral fallback remains valid.  Any explicit ticker must,
    # however, agree with the structured report identity.
    soup = BeautifulSoup(html, "html.parser")
    for image in soup.find_all("img"):
        if "data-brand-ticker" not in image.attrs:
            continue
        try:
            brand_ticker = normalize_ticker(image.get("data-brand-ticker"))
        except CompanyBoundaryError as exc:
            raise CompanyBoundaryError("HTML branded image ticker is blank or invalid") from exc
        if brand_ticker != expected.ticker:
            raise CompanyBoundaryError("HTML branded image ticker does not match report metadata")
    text_ticker, text_event = text_identity_headers(text)
    if text_ticker != expected.ticker or not _events_match(text_event, expected.event):
        raise CompanyBoundaryError("plaintext identity headers do not match report metadata")
    return {"identity": expected.to_dict(), "html": html_identity.to_dict(), "text": {"ticker": text_ticker, "event": canonical_event(text_event)}}


def require_company_boundary(
    report: Mapping[str, Any],
    documents: Iterable[Any] = (),
    *,
    context_documents: Iterable[Any] = (),
    require_surfaces: bool = True,
) -> dict[str, Any]:
    """Fail closed for a new normal brief and return its verified identity."""

    expected = report_identity(report, strict=True)
    company_reports = report.get("company_reports")
    if not isinstance(company_reports, Mapping) or len(company_reports) != 1:
        raise CompanyBoundaryError("normal brief company_reports must contain exactly one company")
    key = next(iter(company_reports), "")
    if normalize_ticker(key) != expected.ticker:
        raise CompanyBoundaryError("company_reports key does not match report identity")
    child = company_reports[key]
    if not isinstance(child, Mapping):
        raise CompanyBoundaryError("company_reports entry must be an object")
    # The per-company view is itself a persisted output surface.  Checking
    # only the outer key would allow a stale or combined child artifact to be
    # written alongside an otherwise valid report.
    validate_report_surfaces(child, expected)
    source_result = validate_source_documents(documents, expected, context_documents=context_documents, require_event=True)
    surface_result = validate_report_surfaces(report, expected) if require_surfaces else {}
    return {"identity": expected.to_dict(), "sources": source_result, "surfaces": surface_result}


def _identity_from_token(value: str) -> BriefIdentity:
    fields: dict[str, str] = {}
    for piece in str(value or "").split(";"):
        if "=" not in piece:
            raise CompanyBoundaryError("MIME identity header is malformed")
        name, field_value = piece.split("=", 1)
        name = name.strip().casefold()
        if name not in _IDENTITY_FIELD_NAMES or name in fields:
            raise CompanyBoundaryError("MIME identity header contains duplicate or unknown fields")
        fields[name] = field_value.strip()
    if set(fields) != set(_IDENTITY_FIELD_NAMES):
        raise CompanyBoundaryError("MIME identity header requires ticker, cik, and event")
    return _identity_from_mapping({**fields, "kind": "earnings_brief"}, source="MIME identity header", require_kind=False)


def validate_mime_message(message: Any, expected: BriefIdentity | Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Validate the actual multipart MIME alternatives for a strict brief."""

    values = message.get_all(IDENTITY_HEADER, []) if hasattr(message, "get_all") else []
    if len(values) != 1:
        raise CompanyBoundaryError("MIME message requires exactly one company identity header")
    boundary_values = message.get_all(BOUNDARY_HEADER, []) if hasattr(message, "get_all") else []
    if boundary_values != [BOUNDARY_VERSION]:
        raise CompanyBoundaryError("MIME message requires exactly one valid company-boundary header")
    header_identity = _identity_from_token(str(values[0]))
    if expected is None:
        target = header_identity
    elif isinstance(expected, BriefIdentity):
        target = expected
    else:
        target = _identity_from_mapping(expected, source="company_identity", require_kind=False)
    if header_identity.cik != target.cik or header_identity.ticker != target.ticker or not _events_match(header_identity.event, target.event):
        raise CompanyBoundaryError("MIME identity header does not match the expected company event")

    plain_parts = []
    html_parts = []
    for part in message.walk() if hasattr(message, "walk") else ():
        if str(part.get_content_disposition() or "").casefold() == "attachment":
            continue
        if part.get_content_type() == "text/plain":
            plain_parts.append(part)
        elif part.get_content_type() == "text/html":
            html_parts.append(part)
    if len(plain_parts) != 1 or len(html_parts) != 1:
        raise CompanyBoundaryError("MIME message requires exactly one text/plain and one text/html alternative")
    try:
        plain = plain_parts[0].get_content()
        html = html_parts[0].get_content()
    except Exception as exc:  # pragma: no cover - defensive parser boundary
        raise CompanyBoundaryError("MIME message alternatives could not be decoded") from exc
    validate_report_surfaces({"html": html, "text": plain}, target)
    return {"identity": target.to_dict(), "plain_parts": 1, "html_parts": 1}


__all__ = [
    "BOUNDARY_HEADER",
    "BOUNDARY_VERSION",
    "COMPANY_IDENTITY_KEY",
    "CompanyBoundaryError",
    "BriefIdentity",
    "HTML_CIK_ATTRIBUTE",
    "HTML_COMPANY_ATTRIBUTE",
    "HTML_EVENT_ATTRIBUTE",
    "IDENTITY_HEADER",
    "TEST_HEADER",
    "canonical_event",
    "has_company_identity",
    "html_identity_marker",
    "normalize_cik",
    "normalize_ticker",
    "report_identity",
    "require_company_boundary",
    "text_identity_headers",
    "validate_mime_message",
    "validate_report_surfaces",
    "validate_source_documents",
]
