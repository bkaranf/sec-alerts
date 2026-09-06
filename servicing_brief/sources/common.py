"""Shared source-layer helpers.

These helpers do not perform network access.  Keeping path, period, filename,
and provenance rules in one place makes SEC and IR records consistent while
preserving the distinction between their acquisition layers.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from servicing_brief.models import Document


def _sources_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("sources", {})
    return value if isinstance(value, dict) else {}


def _storage(config: dict[str, Any]) -> Path:
    value = config.get("_storage") or _sources_config(config).get("storage") or "data"
    return Path(str(value)).resolve()


def _safe_error(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    identity = os.environ.get("EDGAR_IDENTITY", "")
    if identity:
        message = message.replace(identity, "[identity]")
    return " ".join(message.split())[:500]


def _period_order(period: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|FY)", period or "")
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2) or 4)


_PERIOD_PATTERNS = (
    # 2026 Q2 / 2026-Q2 / 2026Q2 / Q2 2026 / Q2-2026
    re.compile(r"\b(20\d{2})\s*[-_/ ]?\s*[Qq]([1-4])\b"),
    re.compile(r"\b[Qq]([1-4])\s*[-_/ ]?\s*(20\d{2})\b"),
    # FY2025 / 2025 FY / 2025 annual (the annual marker is required)
    re.compile(r"\b[Ff][Yy]\s*(20\d{2})\b"),
    re.compile(r"\b(20\d{2})\s*(?:[Ff][Yy]|[Aa]nnual)\b"),
)
_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_QUARTER_END_PATTERN = re.compile(
    r"\b(?:first|second|third|fourth|1st|2nd|3rd|4th|[1-4])\s+quarter(?:ly)?\b[^.]{0,80}?\b(?:ended|ending)\b[^.]{0,20}?"
    r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
    r"(?:[0-3]?\d)(?:st|nd|rd|th)?(?:,|\s+)\s*(20\d{2})\b",
    re.IGNORECASE,
)
_NAMED_QUARTER_PATTERN = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s*(?:of\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_SHORT_QUARTER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([1-4])\s*[Qq]\s*(20\d{2}|\d{2})(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])[Qq]([1-4])\s*(20\d{2}|\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_CONTENT_NAMED_QUARTER_PATTERN = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s*(?:of\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_CONTENT_QUARTER_END_PATTERN = re.compile(
    r"\b(?:for\s+)?(?:the\s+)?(?:quarter|three\s+months?)\s+(?:ended|ending)\s+"
    r"(?:on\s+)?(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+[0-3]?\d(?:st|nd|rd|th)?(?:,|\s+)\s*(20\d{2})\b",
    re.IGNORECASE,
)
_CONTENT_CONTEXT_WORDS = re.compile(
    r"\b(?:earnings?|results?|reported|report|highlights?|performance|financial|"
    r"period|quarter(?:ly)?|ended|as\s+of)\b",
    re.IGNORECASE,
)

_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")
_DOC_EXTENSIONS = {".pdf", ".xls", ".xlsx", ".csv", ".html", ".htm", ".txt", ".doc", ".docx", ".ppt", ".pptx"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_part(value: Any, default: str = "unknown") -> str:
    """Return a short filesystem-safe component without exposing URLs."""

    text = str(value or "").strip()
    text = _SAFE_PART.sub("_", text).strip("._-")
    return text[:140] or default


def normalize_cik(value: Any) -> str:
    """Normalize CIK values to the SEC's ten-digit representation."""

    raw = str(value or "").strip()
    try:
        return f"{int(raw):010d}"
    except (TypeError, ValueError):
        digits = re.sub(r"\D", "", raw)
        return digits.zfill(10) if digits else ""


def file_bytes(value: Any) -> bytes:
    """Convert EdgarTools/HTTP payloads to bytes without altering content."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if value is None:
        return b""
    return str(value).encode("utf-8", errors="replace")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def period_from_report_date(value: Any, form: str = "") -> str:
    """Derive a label from an explicit report period, never from filing date.

    Calendar-quarter conversion is used only for a report date supplied by
    EdgarTools (the period end), and is intentionally conservative for unusual
    month ends.  The exact date remains in Document.metadata.
    """

    text = str(value or "").strip()
    match = re.search(r"(20\d{2})[-/]([01]\d)[-/]([0-3]\d)", text)
    if not match:
        return ""
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return ""
    if str(form).upper().startswith("10-K"):
        return f"{year}-FY"
    return f"{year}-Q{((month - 1) // 3) + 1}"


def infer_period(*values: Any, form: str = "", period_end: Any = None) -> str:
    """Use explicit period language or an explicitly supplied report date.

    Arbitrary source text is never treated as a report date.  Press releases
    commonly contain publication dates, incorporation dates, and prior-year
    comparisons; only ``period_end`` (normally EdgarTools ``report_date``)
    may use the date-to-quarter conversion.
    """

    # First prefer explicit quarter/year wording from titles or snippets.
    for value in values:
        text = str(value or "")
        matches: list[tuple[tuple[int, int], str]] = []
        for index, pattern in enumerate(_PERIOD_PATTERNS):
            for match in pattern.finditer(text):
                groups = match.groups()
                if index == 1:
                    label = f"{groups[1]}-Q{groups[0]}"
                    order = (int(groups[1]), int(groups[0]))
                elif index in (2, 3):
                    label = f"{groups[0]}-FY"
                    order = (int(groups[0]), 4)
                else:
                    label = f"{groups[0]}-Q{groups[1]}"
                    order = (int(groups[0]), int(groups[1]))
                matches.append((order, label))
        if matches:
            # Releases often mention the prior quarter before the current
            # result in a comparison sentence; use the latest explicit period
            # within that one title/text value.
            return max(matches, key=lambda item: item[0])[1]

    # Context-required date phrases reduce false matches from publication
    # timestamps and prior-period tables in an earnings release.
    for value in values:
        text = str(value or "")
        named = _NAMED_QUARTER_PATTERN.search(text)
        if named:
            ordinal = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}[named.group(1).lower()]
            return f"{named.group(2)}-Q{ordinal}"
        short_matches = list(_SHORT_QUARTER_PATTERN.finditer(text))
        if short_matches:
            parsed: list[tuple[tuple[int, int], str]] = []
            for short in short_matches:
                quarter = short.group(1) or short.group(3)
                year_text = short.group(2) or short.group(4)
                year = int(year_text)
                if year < 100:
                    year += 2000
                parsed.append(((year, int(quarter)), f"{year}-Q{quarter}"))
            return max(parsed, key=lambda item: item[0])[1]
        ended = _QUARTER_END_PATTERN.search(text)
        if ended:
            month = _MONTHS[ended.group(1).lower()]
            return f"{ended.group(2)}-Q{((month - 1) // 3) + 1}"

    if period_end is not None:
        period = period_from_report_date(period_end, form=form)
        if period:
            return period
    return "unknown"


def infer_content_period(value: Any, *, max_chars: int = 30_000) -> str:
    """Read a likely earnings period from the opening source content.

    This helper is deliberately narrower than :func:`infer_period`.  It is
    intended for an 8-K exhibit or an issuer HTML release where the filing
    event date is not the financial period.  Only the opening portion is
    inspected, and a quarter token must be near earnings/reporting language;
    dates in a historical table or a forward-looking paragraph therefore do
    not determine the event period.  Binary PDF content should be passed as
    an empty value and remains ``unknown`` until an anchor or URL supplies a
    period.
    """

    text = str(value or "")[: max(1, int(max_chars or 30_000))]
    if not text:
        return "unknown"
    # HTML comments and tags often contain XBRL contexts or presentation
    # rendering data.  Removing tags keeps the visible heading while reducing
    # accidental matches from markup attributes.
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)

    for match in _CONTENT_NAMED_QUARTER_PATTERN.finditer(text):
        window = text[max(0, match.start() - 120) : match.end() + 160]
        if _CONTENT_CONTEXT_WORDS.search(window):
            ordinal = {
                "first": 1,
                "1st": 1,
                "second": 2,
                "2nd": 2,
                "third": 3,
                "3rd": 3,
                "fourth": 4,
                "4th": 4,
            }[match.group(1).lower()]
            return f"{match.group(2)}-Q{ordinal}"

    for match in _CONTENT_QUARTER_END_PATTERN.finditer(text):
        month_name = re.search(
            r"(january|february|march|april|may|june|july|august|september|"
            r"october|november|december)",
            match.group(0),
            flags=re.IGNORECASE,
        )
        if month_name:
            month = _MONTHS[month_name.group(1).lower()]
            return f"{match.group(1)}-Q{((month - 1) // 3) + 1}"

    for match in _SHORT_QUARTER_PATTERN.finditer(text):
        window = text[max(0, match.start() - 120) : match.end() + 160]
        if not _CONTENT_CONTEXT_WORDS.search(window):
            continue
        quarter = match.group(1) or match.group(3)
        year_text = match.group(2) or match.group(4)
        year = int(year_text)
        if year < 100:
            year += 2000
        return f"{year}-Q{quarter}"
    return "unknown"


def latest_completed_period(as_of: date | datetime | None = None) -> tuple[int, int]:
    """Return the latest calendar quarter that has ended as of ``as_of``.

    The current quarter is intentionally excluded.  A filing or presentation
    labelled ``Q3 2026`` on September 4, 2026 can be a scheduled event or
    forward-looking reference and must not be treated as a completed earnings
    package until the quarter closes.
    """

    when = as_of or datetime.now(timezone.utc)
    if isinstance(when, datetime):
        when = when.date()
    quarter = ((when.month - 1) // 3) + 1
    if when.month in {3, 6, 9, 12} and when.day == (31 if when.month in {3, 12} else 30):
        # The end date itself is complete.  This branch is mainly useful in
        # deterministic tests; ordinary runs use the previous quarter until
        # the first day of the following quarter.
        return when.year, quarter
    if quarter == 1:
        return when.year - 1, 4
    return when.year, quarter - 1


def title_kind(title: Any, filename: Any = "", form: str = "") -> str:
    """Classify a document using its title and filename, without guessing period."""

    text = f"{title or ''} {filename or ''}".lower()
    form_upper = str(form or "").upper()
    if form_upper.startswith("10-K"):
        return form_upper
    if form_upper.startswith("10-Q"):
        return form_upper
    if form_upper.startswith("NT 10-"):
        return form_upper
    # Issuer and SEC links often call a slide deck simply ``deck`` (including
    # compact names such as ``2Q25_Earnings_Deck--LMtest.pdf``).  Treat that
    # explicit filename marker as a presentation even when an anchor title
    # says only ``Earnings release``.
    if "presentation" in text or "investor deck" in text or "slide" in text or "deck" in text:
        return "presentation"
    # A call transcript or prepared remarks can include the word ``earnings``
    # in its title.  Check these explicit source types before the generic
    # earnings/results release rule so they remain separate company events.
    if "prepared remarks" in text or "prepared-remarks" in text:
        return "prepared_remarks"
    if "transcript" in text:
        return "transcript"
    if "supplement" in text or "financial supplement" in text:
        return "supplement"
    if "release" in text or "earnings" in text or "results" in text:
        return "release"
    if "annual report" in text:
        return "annual-report"
    if form_upper.startswith("8-K"):
        return form_upper
    return "financial-material"


def extension_for(url: str, content_type: str = "") -> str:
    path_ext = Path(urlparse(url).path).suffix.lower()
    if path_ext in _DOC_EXTENSIONS:
        return path_ext
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "text/html": ".html",
        "text/plain": ".txt",
    }.get(ctype, ".bin")


def archive_bytes(
    storage: str | os.PathLike[str],
    *,
    ticker: str,
    period: str,
    kind: str,
    payload: bytes,
    extension: str,
    accession: str = "",
    content_hash: str | None = None,
) -> tuple[Path, str]:
    """Atomically archive original bytes and return ``(path, hash)``.

    A content hash is always included in the filename.  This keeps revised
    issuer documents and amendments recoverable even when an issuer reuses a
    URL or document name.
    """

    data = bytes(payload)
    computed_digest = sha256_bytes(data)
    if content_hash and str(content_hash).lower() != computed_digest:
        raise ValueError("provided content hash does not match archived payload")
    digest = computed_digest
    ext = extension if extension.startswith(".") else f".{extension}" if extension else ".bin"
    ext = ext.lower()
    folder = Path(storage).resolve() / "archive" / clean_part(ticker) / clean_part(period)
    folder.mkdir(parents=True, exist_ok=True)
    accession_part = clean_part(accession, default="")
    pieces = [clean_part(ticker), clean_part(period), clean_part(kind)]
    if accession_part:
        pieces.append(accession_part)
    pieces.append(digest[:16])
    destination = folder / ("_".join(pieces) + ext)
    if not destination.exists():
        fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=str(folder))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return destination, digest


def write_json_atomic(path: Path, value: Any) -> None:
    """Write a small manifest/checkpoint JSON file without partial updates."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def metadata_value(obj: Any, *names: str, default: Any = "") -> Any:
    """Read an EdgarTools/fake object attribute without triggering odd failures."""

    for name in names:
        try:
            value = getattr(obj, name)
            if callable(value) and name not in {"text", "html", "download"}:
                value = value()
            if value is not None and value != "":
                return value
        except Exception:
            continue
    return default


def json_safe(value: Any) -> Any:
    """Make source metadata safe for SourceResult serialization."""

    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    return str(value)


def looks_like_document(url: str, title: str = "", content_type: str = "") -> bool:
    """Return whether a discovered IR link plausibly points to source material."""

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    ext = Path(parsed.path).suffix.lower()
    ctype = (content_type or "").lower()
    text = f"{title} {parsed.path}".lower()
    if ext in _DOC_EXTENSIONS or "application/pdf" in ctype:
        return True
    keywords = ("earnings", "result", "presentation", "supplement", "annual report", "transcript", "financial")
    return any(word in text for word in keywords)


def source_document(
    *,
    issuer: str,
    ticker: str,
    cik: str,
    title: str,
    kind: str,
    source: str,
    url: str,
    published: str,
    period: str,
    path: Path,
    content_hash: str,
    accession: str = "",
    accepted: str = "",
    discovered_from: str = "",
    classification: str = "issuer-published",
    mime_type: str = "application/octet-stream",
    metadata: dict[str, Any] | None = None,
) -> Document:
    """Construct the shared Document model with normalized provenance."""

    return Document(
        issuer=issuer,
        cik=normalize_cik(cik),
        title=title.strip() or kind,
        kind=kind,
        source=source,
        url=url,
        published=str(published or ""),
        period=period or "unknown",
        path=str(path),
        content_hash=content_hash,
        accession=accession,
        accepted=str(accepted or ""),
        discovered_from=discovered_from,
        classification=classification,
        mime_type=mime_type or "application/octet-stream",
        metadata=json_safe(metadata or {}),
    )


def company_ticker(company: dict[str, Any]) -> str:
    return str(company.get("ticker") or company.get("symbol") or "").strip().upper()


def company_name(company: dict[str, Any], fallback: str = "") -> str:
    return str(company.get("name") or company.get("legal_name") or fallback or company_ticker(company)).strip()
