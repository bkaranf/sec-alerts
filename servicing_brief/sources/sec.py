"""SEC collection through the public EdgarTools API.

No SEC URL is fetched directly in this module.  EdgarTools owns company and
filing discovery, filing metadata, and attachment acquisition.  This module
only selects relevant filings, archives the bytes returned by EdgarTools, and
records provenance for the reporting layer.
"""

from __future__ import annotations

import contextlib
from html import unescape
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from servicing_brief.models import Document, SourceResult

from .common import (
    archive_bytes,
    clean_part,
    company_name,
    company_ticker,
    extension_for,
    file_bytes,
    infer_content_period,
    infer_period,
    json_safe,
    latest_completed_period,
    metadata_value,
    normalize_cik,
    source_document,
    title_kind,
    utc_now,
)
from .ratelimit import sec_acquisition_guard, shared_sec_lock_path


SEC_FORMS = (
    "10-Q",
    "10-Q/A",
    "10-K",
    "10-K/A",
    "8-K",
    "8-K/A",
    "NT 10-Q",
    "NT 10-Q/A",
    "NT 10-K",
    "NT 10-K/A",
)

# Item 2.02/7.01/9.01 cover earnings packages; the additional items cover
# acquisitions, impairments, funding and non-reliance events that matter to a
# mortgage-servicing oversight reader.
RELEVANT_8K_ITEMS = {
    "1.01",
    "2.01",
    "2.02",
    "2.03",
    "2.04",
    "2.05",
    "2.06",
    "3.02",
    "7.01",
    "8.01",
    "9.01",
}
_RELEVANT_8K_TEXT = re.compile(
    r"\b(?:earnings|results?|quarterly|financial results?|mortgage servicing|servicing portfolio|"
    r"acquisition|impairment|non[- ]reliance|restatement|liquidity|financing|delinquen|forbearance|"
    r"servicing advances?)\b",
    re.IGNORECASE,
)
# These rows are common in otherwise relevant 8-K accessions, but do not
# belong in an earnings package.  The description predicate below catches
# most of them before download; the opening-body predicate handles exhibits
# whose SEC description is only the generic ``Additional exhibit``.
_ROUTINE_8K_BODY = re.compile(
    r"(?:^|\s)(?:bylaws?|articles? of (?:incorporation|amendment)|underwriting agreement|"
    r"opinion re legality|certificate of designation|deposit agreement)(?:\s|$)",
    re.IGNORECASE,
)
_LEADERSHIP_8K_BODY = re.compile(
    r"\b(?:board|director|appointed|appointment|incoming ceo|chief executive officer)\b",
    re.IGNORECASE,
)
_EARNINGS_8K_BODY = re.compile(
    r"\b(?:earnings?|quarterly|results?|financial results?|conference call|"
    r"acquisition of|merger with|mortgage servicing|subservic|servicing portfolio|"
    r"impairment|restatement|delinquen|forbearance|liquidity)\b",
    re.IGNORECASE,
)
_ITEM_RE = re.compile(r"(?:item\s*)?(\d+\.\d+)", re.IGNORECASE)
_SEC_BLOCKED = re.compile(r"\b(?:403|429|too many requests|forbidden|identity(?:\s+is)?\s+not\s+set)\b", re.I)
# Workiva and similar SEC exhibits often use a compact period token embedded
# in a filename (``wellsfargo2q26pres``).  The ordinary period regex requires
# a word boundary and therefore misses that token.  Keep this filename-only
# fallback narrow: a quarter token must have a two/four digit year and be
# followed by a filename suffix/descriptor.  Content still has to agree when
# the 8-K event is grouped below.
_COMPACT_FILENAME_QUARTER = re.compile(
    r"(?<!\d)([1-4])[Qq](20\d{2}|\d{2})(?=[A-Za-z]|[^A-Za-z0-9]|$)"
)


def _sources_config(config: dict[str, Any]) -> dict[str, Any]:
    value = config.get("sources", {})
    return value if isinstance(value, dict) else {}


def _storage(config: dict[str, Any]) -> Path:
    value = config.get("_storage") or _sources_config(config).get("storage") or "data"
    return Path(str(value)).resolve()


def _lookback_start(config: dict[str, Any], *, bootstrap: bool, checkpoints: dict[str, str] | None, key: str) -> str:
    sources = _sources_config(config)
    now = datetime.now(timezone.utc).date()
    try:
        normal_days = max(1, int(sources.get("lookback_days", 120)))
    except (TypeError, ValueError):
        normal_days = 120
    try:
        bootstrap_days = max(normal_days, int(sources.get("bootstrap_lookback_days", 450)))
    except (TypeError, ValueError):
        bootstrap_days = max(normal_days, 450)
    days = bootstrap_days if bootstrap else normal_days
    checkpoint = (checkpoints or {}).get(key, "")
    if checkpoint:
        # A checkpoint is discovery time, so subtract overlap before using it
        # as a filing-date boundary.  This catches delayed indexing and a run
        # interrupted after discovery but before persistence.
        try:
            overlap = max(0, int(sources.get("overlap_days", 14)))
        except (TypeError, ValueError):
            overlap = 14
        try:
            checkpoint_date = datetime.fromisoformat(str(checkpoint).replace("Z", "+00:00")).date()
            start = checkpoint_date - timedelta(days=overlap)
            # A fresh checkpoint narrows discovery to the overlap window; a
            # stale checkpoint still receives the configured bounded lookback.
            return max(start, now - timedelta(days=days)).isoformat()
        except (TypeError, ValueError):
            pass
    return (now - timedelta(days=days)).isoformat()


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _accepted_text(filing: Any) -> str:
    return _date_text(metadata_value(filing, "acceptance_datetime", "acceptance_date", default=""))


def _filing_items(filing: Any) -> set[str]:
    values: list[str] = []
    for name in ("items", "parsed_items"):
        try:
            value = getattr(filing, name, "")
            if value is None:
                continue
            if isinstance(value, (list, tuple, set)):
                values.extend(str(item) for item in value)
            else:
                values.append(str(value))
        except Exception:
            continue
    items: set[str] = set()
    for value in values:
        for match in _ITEM_RE.finditer(value):
            items.add(match.group(1))
    return items


def _filing_form(filing: Any) -> str:
    return str(metadata_value(filing, "form", default="")).strip().upper()


def _filing_accession(filing: Any) -> str:
    return str(metadata_value(filing, "accession_no", "accession_number", default="")).strip()


def _filing_date(filing: Any) -> str:
    return _date_text(metadata_value(filing, "filing_date", default=""))


def _filing_report_date(filing: Any) -> str:
    # An 8-K's ``period_of_report`` is the event/report date, not the quarter
    # being announced.  Calling it for an 8-K can both trigger unnecessary
    # EdgarTools work and assign a future/current quarter to an earnings event.
    if _filing_form(filing) in {"8-K", "8-K/A"}:
        return ""
    return _date_text(metadata_value(filing, "report_date", "period_of_report", default=""))


def _candidate_period(filing: Any) -> str:
    """Read a filing's explicit reporting period without using filing date."""

    form = _filing_form(filing)
    title = str(metadata_value(filing, "primary_doc_description", "description", default="") or "")
    return infer_period(title, form=form, period_end=_filing_report_date(filing))


def _period_order(period: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|FY)", period or "")
    if not match:
        return (0, 0)
    return int(match.group(1)), int(match.group(2) or 4)


def _filing_company(filing: Any, fallback: str) -> str:
    return str(metadata_value(filing, "company", default=fallback) or fallback).strip()


def _filing_url(filing: Any) -> str:
    value = metadata_value(filing, "filing_url", "homepage_url", "url", default="")
    return str(value or "")


def _safe_error(exc: BaseException) -> str:
    """Return a useful error without ever echoing the SEC identity value."""

    message = str(exc).strip() or type(exc).__name__
    identity = os.environ.get("EDGAR_IDENTITY", "")
    if identity:
        message = message.replace(identity, "[identity]")
    # Avoid multiline dumps from transport libraries in SourceResult JSON.
    return " ".join(message.split())[:500]


def _is_blocked(exc: BaseException) -> bool:
    return bool(_SEC_BLOCKED.search(_safe_error(exc)))


def _call_get_filings(company: Any, filing_date: str, *, forms: Iterable[str] = SEC_FORMS) -> Any:
    """Call the documented Company.get_filings API with bounded compatibility.

    ``trigger_full_load`` is documented in EdgarTools 5.x and ensures the
    returned EntityFilings objects retain filing-level metadata.  The fallback
    is for small test doubles and older 5.x patch releases that lack that
    optional keyword; it does not introduce another acquisition layer.
    """

    kwargs = {"form": list(forms), "filing_date": filing_date, "amendments": True, "trigger_full_load": True}
    getter = getattr(company, "get_filings")
    try:
        return getter(**kwargs)
    except TypeError as exc:
        if "trigger_full_load" not in str(exc) and "unexpected keyword" not in str(exc):
            raise
        kwargs.pop("trigger_full_load", None)
        return getter(**kwargs)


def _iter_filings(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _is_relevant_8k(filing: Any, items: set[str]) -> tuple[bool, str]:
    if items & RELEVANT_8K_ITEMS:
        return True, "metadata item " + ",".join(sorted(items & RELEVANT_8K_ITEMS))
    # When SEC metadata is empty or incomplete, inspect the primary filing text
    # through EdgarTools.  This is bounded and runs under the global SEC lock.
    try:
        text_method = getattr(filing, "text", None)
        text = text_method() if callable(text_method) else ""
        if _RELEVANT_8K_TEXT.search(str(text)[:300_000]):
            return True, "primary filing text"
    except Exception:
        # The inability to inspect an otherwise unlabelled 8-K should be
        # visible to the caller, rather than silently treated as irrelevant.
        return False, "8-K items unavailable and primary text could not establish relevance"
    return False, "routine or unrelated 8-K items"


def _attachment_value(attachment: Any, *names: str, default: Any = "") -> Any:
    return metadata_value(attachment, *names, default=default)


def _iter_attachments(filing: Any) -> list[Any]:
    try:
        attachments = getattr(filing, "attachments")
        return list(attachments)
    except Exception:
        return []


def _attachment_filename(attachment: Any) -> str:
    return str(_attachment_value(attachment, "document", "filename", "name", default="") or "").strip()


def _attachment_type(attachment: Any) -> str:
    return str(_attachment_value(attachment, "document_type", "type", default="") or "").strip().upper()


def _attachment_description(attachment: Any) -> str:
    return str(_attachment_value(attachment, "display_description", "description", "purpose", default="") or "").strip()


def _attachment_purpose(attachment: Any) -> str:
    return str(_attachment_value(attachment, "purpose", default="") or "").strip()


def _attachment_url(attachment: Any, filing: Any) -> str:
    value = _attachment_value(attachment, "url", default="")
    return str(value or _filing_url(filing))


def _attachment_sequence(attachment: Any) -> str:
    return str(_attachment_value(attachment, "sequence_number", "sequence", "seq", default="") or "").strip()


def _attachment_payload_with_fidelity(attachment: Any) -> tuple[bytes, bool]:
    """Return attachment payload and whether EdgarTools returned raw bytes."""

    downloader = getattr(attachment, "download", None)
    if callable(downloader):
        try:
            value = downloader()
            return file_bytes(value), isinstance(value, (bytes, bytearray))
        except TypeError:
            value = downloader(None)
            return file_bytes(value), isinstance(value, (bytes, bytearray))
    value = getattr(attachment, "content", b"")
    return file_bytes(value), isinstance(value, (bytes, bytearray))


def _attachment_payload(attachment: Any) -> bytes:
    """Compatibility wrapper returning only content bytes."""

    return _attachment_payload_with_fidelity(attachment)[0]


def _attachment_extension(attachment: Any, url: str, payload: bytes) -> str:
    filename = _attachment_filename(attachment)
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix.lower()
    return extension_for(url, str(_attachment_type(attachment)))


def _mime_for_extension(extension: str) -> str:
    return {
        ".pdf": "application/pdf",
        ".xls": "application/vnd.ms-excel",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".ppt": "application/vnd.ms-powerpoint",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".html": "text/html",
        ".htm": "text/html",
        ".txt": "text/plain",
        ".xml": "application/xml",
    }.get(extension.lower(), "application/octet-stream")


def _payload_text(payload: bytes, extension: str, *, max_chars: int = 30_000) -> str:
    if extension.lower() not in {".html", ".htm", ".txt", ".xml"}:
        return ""
    return payload[: max(1, int(max_chars or 30_000))].decode("utf-8", errors="ignore")


def _opening_visible_text(payload: bytes, extension: str, *, max_chars: int = 12_000) -> str:
    """Return a bounded visible opening for exhibit relevance checks.

    SEC inline-XBRL HTML contains historical comparison facts, taxonomy
    labels, and filing metadata after the visible heading.  Relevance is
    therefore based on the opening text only; scanning an entire filing can
    turn an unrelated board or financing release into an earnings document.
    """

    text = _payload_text(payload, extension, max_chars=max_chars)
    if not text:
        return ""
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(unescape(text).split())


def _eight_k_material_attachment(attachment: Any, payload: bytes, extension: str) -> bool:
    """Return whether one downloaded 8-K row is reportable source material.

    Sequence 1 is the filed current report and is retained whenever a
    qualifying exhibit exists.  Non-primary rows must independently identify
    earnings, servicing, or a material financial event in their title or
    opening content.  This keeps routine legal, board, and administrative
    exhibits out of the package even when their accession has Item 9.01.
    """

    description = _attachment_description(attachment)
    filename = _attachment_filename(attachment)
    opening = _opening_visible_text(payload, extension)
    metadata_text = f"{description} {filename}"
    routine = f"{metadata_text} {opening}"
    if any(word in metadata_text.lower() for word in (
        "article", "bylaw", "opinion", "underwriting", "security holder",
        "rights of security", "material contract", "certificate of designation",
        "deposit agreement", "cover", "entity information",
    )) or _ROUTINE_8K_BODY.search(opening[:2_000]):
        return False
    # Leadership announcements routinely include a financial-services
    # biography later in the release.  A heading that is plainly a board or
    # director appointment is not a servicing event unless its own opening
    # text also announces earnings or an acquisition/servicing transaction.
    lead = opening[:1_500]
    if _LEADERSHIP_8K_BODY.search(lead) and not _EARNINGS_8K_BODY.search(lead):
        return False
    return bool(_RELEVANT_8K_TEXT.search(routine))


def _attachment_period_hint(attachment: Any, payload: bytes, extension: str, *, form: str) -> str:
    """Read a period from attachment metadata/content without filing dates."""

    title = _attachment_description(attachment)
    filename = _attachment_filename(attachment)
    explicit = infer_period(title, filename, form=form)
    if explicit != "unknown":
        return explicit
    # Some filing systems concatenate the issuer, period, and document kind
    # without separators.  Treat that token as explicit filename metadata,
    # while keeping all free-form body text on the bounded opening path below.
    compact = _COMPACT_FILENAME_QUARTER.search(filename)
    if compact:
        year = int(compact.group(2))
        if year < 100:
            year += 2000
        return f"{year}-Q{compact.group(1)}"
    if form in {"8-K", "8-K/A"}:
        # A full deck commonly repeats prior quarters in comparison tables or
        # endnotes.  Only its opening visible text can identify the announced
        # period; scanning 30k characters made WFC's 2Q26 deck resolve to a
        # later ``third quarter 2025`` footnote.
        opening = _opening_visible_text(payload, extension, max_chars=6_000)
        return infer_content_period(opening, max_chars=6_000)
    return "unknown"


def _filing_event_period(
    filing: Any,
    attachments: list[Any],
    payloads: dict[int, tuple[bytes, bool, str]],
) -> tuple[str, list[str]]:
    """Return a verified earnings period and any conflicting hints.

    For periodic SEC forms, EdgarTools' period-of-report metadata is the
    authoritative period.  For an 8-K, its event date is deliberately ignored;
    a period is accepted only when an exhibit title/filename or opening
    earnings content identifies it.  A conflict leaves the event unknown so a
    later reporting step can review it instead of attaching a wrong-quarter
    deck.
    """

    form = _filing_form(filing)
    if form not in {"8-K", "8-K/A"}:
        period = _candidate_period(filing)
        return period, []
    hints: list[str] = []
    for attachment in attachments:
        payload_info = payloads.get(id(attachment))
        if payload_info is None:
            continue
        payload, _original_bytes, extension = payload_info
        hint = _attachment_period_hint(attachment, payload, extension, form=form)
        if hint != "unknown" and hint not in hints:
            hints.append(hint)
    if len(hints) == 1:
        return hints[0], []
    if len(hints) > 1:
        return "unknown", hints
    return "unknown", []


def _attachment_references(payload: bytes, extension: str) -> set[str]:
    """Extract linked asset basenames from an HTML exhibit."""

    if extension.lower() not in {".html", ".htm", ".xml"}:
        return set()
    text = payload[:2_000_000].decode("utf-8", errors="ignore")
    names: set[str] = set()
    for match in re.finditer(r"(?:src|href)\s*=\s*['\"]([^'\"]+)['\"]", text, re.IGNORECASE):
        value = match.group(1).split("?", 1)[0].split("#", 1)[0]
        name = Path(urlparse(value).path).name.strip().lower()
        if name:
            names.add(name)
    return names


_GENERIC_EXHIBIT_TITLE = re.compile(
    r"^(?:document|exhibit\s+\d+(?:\.\d+)?|additional\s+exhibits?|form\s+8-k|8-k|current\s+report)$",
    re.IGNORECASE,
)


def _content_heading(payload: bytes, extension: str) -> str:
    """Extract one short visible/hidden heading from a text exhibit."""

    text = _payload_text(payload, extension, max_chars=40_000)
    if not text:
        return ""
    for pattern in (
        r"<title[^>]*>(.*?)</title>",
        r"<h[1-3][^>]*>(.*?)</h[1-3]>",
        r"<b[^>]*>(.*?)</b>",
        # Workiva SEC exhibits frequently render the release heading in a
        # styled ``font`` element instead of a semantic heading.  Keep this
        # after the semantic tags so a real title wins when one exists.
        r"<font[^>]*>(.*?)</font>",
    ):
        for match in re.finditer(pattern, text, re.IGNORECASE | re.DOTALL):
            candidate = re.sub(r"<[^>]+>", " ", match.group(1))
            candidate = " ".join(unescape(candidate).split())
            if not candidate or len(candidate) > 240 or _GENERIC_EXHIBIT_TITLE.fullmatch(candidate):
                continue
            return candidate
    # Plain text exhibits may begin with a useful release heading.
    candidate = re.sub(r"<[^>]+>", " ", text[:2_000])
    candidate = " ".join(unescape(candidate).split())
    return candidate[:240] if candidate else ""


def _sec_document_title(attachment: Any, payload: bytes, extension: str, form: str) -> str:
    description = _attachment_description(attachment)
    filename = _attachment_filename(attachment)
    if description and not _GENERIC_EXHIBIT_TITLE.fullmatch(description.strip()):
        return description
    heading = _content_heading(payload, extension)
    if heading:
        return heading
    return description or filename or f"{form} filing"


def _classify_eight_k_attachment(attachment: Any, payload: bytes, extension: str, title: str, filename: str) -> str:
    """Classify an 8-K exhibit from explicit labels and its opening heading.

    Full exhibit text often mentions a supplemental table or a prior release;
    those later references must not override a filename such as ``pres`` or a
    visible heading such as ``News Release``.
    """

    explicit = f"{title} {filename}".lower()
    opening = _opening_visible_text(payload, extension, max_chars=5_000).lower()
    if re.search(r"(?:presentation|deck|slide|earnings report|investor update|conference call|(?<![a-z])pres(?=[._-]))", explicit):
        return "presentation"
    if re.search(r"(?:supplement|quarterly performance summary|financial data)", explicit):
        return "supplement"
    if re.search(r"(?:release|news|reports? .*results?|announces? .*results?)", explicit):
        return "release"
    if re.search(r"(?:presentation|deck|slide|earnings report|investor update|conference call)", opening):
        return "presentation"
    if re.search(r"(?:supplement|quarterly performance summary)", opening):
        return "supplement"
    if re.search(r"(?:earnings? release|news release|reports? .*results?|announces? .*results?|financial results?)", opening):
        return "release"
    return "8-K"


def _attachment_is_xbrl(attachment: Any) -> bool:
    filename = _attachment_filename(attachment).lower()
    doc_type = _attachment_type(attachment)
    description = f"{_attachment_description(attachment)} {_attachment_purpose(attachment)}".upper()
    # EdgarTools exposes XBRL support rows as ordinary HTML (for example
    # ``R1.htm``) with ``IDEA: XBRL DOCUMENT`` in the description.  Filtering
    # only on extension or EX-101 therefore still archives hundreds of rows.
    # Sequence 1 is handled before this predicate because the primary filing
    # itself can be inline XBRL and is the authoritative original document.
    return (
        bool(getattr(attachment, "ixbrl", False)) and not _attachment_sequence(attachment) == "1"
    ) or doc_type.startswith("EX-101") or "XBRL" in doc_type or "XBRL DOCUMENT" in description or filename.endswith((".xsd", ".xbrl")) or (
        filename.endswith(".xml") and any(word in filename for word in ("instance", "schema", "cal", "def", "lab", "pre"))
    )


def _attachment_is_asset(attachment: Any) -> bool:
    """Return whether an attachment is a linked presentation/image asset.

    Assets are retained as byte-level provenance for their parent HTML exhibit,
    but they are not independent financial documents in the report.  This is
    especially important for Edgar filings where a single slide deck is an
    HTML wrapper followed by dozens of JPEG ``GRAPHIC`` rows.
    """

    filename = _attachment_filename(attachment).lower()
    description = _attachment_description(attachment).lower()
    extension = Path(filename).suffix.lower()
    return extension in {".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".bmp", ".tif", ".tiff"} or description.strip() in {"graphic", "image", "logo"}


def _attachment_is_metadata_row(attachment: Any) -> bool:
    """Exclude EDGAR cover/entity rows that carry no reportable content."""

    description = _attachment_description(attachment).strip().lower()
    doc_type = _attachment_type(attachment).strip().lower()
    filename = _attachment_filename(attachment).strip().lower()
    return description in {
        "cover",
        "document and entity information",
        "entity information",
        "document and entity information (cover)",
    } or description.startswith("cover ") or doc_type in {"cover", "document and entity information", "entity information"} or (
        Path(filename).name in {"r1.htm", "r1.html"} and description.startswith("cover")
    )


def _attachment_is_relevant(attachment: Any, form: str, *, is_eight_k: bool) -> bool:
    sequence = _attachment_sequence(attachment)
    if sequence == "1":
        return True
    if _attachment_is_xbrl(attachment) or _attachment_is_asset(attachment) or _attachment_is_metadata_row(attachment):
        return False
    doc_type = _attachment_type(attachment)
    filename = _attachment_filename(attachment).lower()
    description = _attachment_description(attachment).lower()
    if is_eight_k:
        # Inspect financial exhibits and EX-99 material, while filtering the
        # routine legal/administrative rows that frequently accompany a
        # financing 8-K.  EX-99 numbering is intentionally not interpreted as
        # a fixed release/deck convention; the content/title classifier below
        # identifies the actual material.
        lower = f"{filename} {description} {doc_type}".lower()
        if any(word in lower for word in ("article", "bylaw", "opinion", "underwriting", "security holder", "document and entity", "cover", "graphic")):
            return False
        if doc_type.startswith("EX-99"):
            return True
        return any(word in lower for word in (
            "earnings", "release", "result", "presentation", "supplement",
            "servicing", "mortgage", "financial statement", "acquisition",
            "merger", "impairment", "restatement", "liquidity", "financing",
        ))
    if doc_type.startswith("EX-99"):
        return True
    # A periodic report's primary HTML contains the full filing.  Keep only a
    # small set of named supplemental schedules that materially help a
    # servicing review; generic R*.htm/XBRL support rows are not documents.
    return any(word in f"{filename} {description}" for word in (
        "earnings", "release", "result", "presentation", "supplement", "mortgage servicing", "servicing portfolio", "loan sales and servicing"
    ))


def _primary_fallback(filing: Any) -> tuple[bytes, str]:
    """Fallback representation for unusual filings with no attachment rows."""

    html = getattr(filing, "html", None)
    if callable(html):
        with contextlib.suppress(Exception):
            value = html()
            if value:
                return file_bytes(value), ".html"
    text_method = getattr(filing, "text", None)
    if callable(text_method):
        with contextlib.suppress(Exception):
            value = text_method()
            if value:
                return file_bytes(value), ".txt"
    return b"", ".bin"


def _document_from_attachment(
    config: dict[str, Any],
    company: dict[str, Any],
    filing: Any,
    attachment: Any,
    *,
    reason: str,
    payload_info: tuple[bytes, bool, str] | None = None,
    period_override: str = "",
    period_basis: str = "",
    assets: list[dict[str, Any]] | None = None,
) -> Document | None:
    ticker = company_ticker(company)
    configured_name = company_name(company, fallback=_filing_company(filing, ticker))
    cik = normalize_cik(company.get("cik") or metadata_value(filing, "cik", default=""))
    form = _filing_form(filing)
    accession = _filing_accession(filing)
    filing_date = _filing_date(filing)
    report_date = _filing_report_date(filing)
    filename = _attachment_filename(attachment)
    description = _attachment_description(attachment)
    source_url = _attachment_url(attachment, filing)
    sequence = _attachment_sequence(attachment)
    if payload_info is None:
        payload, original_bytes = _attachment_payload_with_fidelity(attachment)
        extension = _attachment_extension(attachment, source_url, payload)
    else:
        payload, original_bytes, extension = payload_info
    if not payload:
        return None
    title = _sec_document_title(attachment, payload, extension, form)
    own_period = _attachment_period_hint(attachment, payload, extension, form=form)
    period = period_override or own_period
    if not period or period == "unknown":
        period = own_period
    kind = title_kind(title, filename, form=form)
    if form in {"8-K", "8-K/A"}:
        kind = _classify_eight_k_attachment(attachment, payload, extension, title, filename)
    items = sorted(_filing_items(filing))
    # Form 8-K Item 2.02 and 7.01 information is furnished.  Item 9.01
    # financial statements and the primary 8-K remain filed; each exhibit gets
    # its own classification for downstream citations.
    furnished_items = {"2.02", "7.01"}
    classification = "sec-furnished" if form in {"8-K", "8-K/A"} and furnished_items & set(items) and sequence != "1" and _attachment_type(attachment).startswith("EX-99") else "sec-filed"
    path, digest = archive_bytes(
        _storage(config),
        ticker=ticker,
        period=period,
        kind=kind,
        payload=payload,
        extension=extension,
        accession=accession,
    )
    return source_document(
        issuer=configured_name,
        ticker=ticker,
        cik=cik,
        title=title,
        kind=kind,
        source="sec",
        url=source_url,
        published=filing_date,
        period=period,
        path=path,
        content_hash=digest,
        accession=accession,
        accepted=_accepted_text(filing),
        discovered_from=_filing_url(filing),
        classification=classification,
        mime_type=_mime_for_extension(extension),
        metadata={
            "ticker": ticker,
            "form": form,
            "items": items,
            "report_date": report_date,
            "filing_date": filing_date,
            "sequence": sequence,
            "document": filename,
            "description": description,
            "source_reason": reason,
            "filer_company": _filing_company(filing, configured_name),
            "furnished_items": sorted(furnished_items & set(items)),
            "edgartools": True,
            "original_bytes": original_bytes,
            "byte_fidelity": "raw" if original_bytes else "UTF-8 encoding of EdgarTools text payload",
            "period_basis": period_basis or ("filing_period_of_report" if form in {"10-Q", "10-Q/A", "10-K", "10-K/A"} and period != "unknown" else "attachment_title_or_content"),
            "assets": assets or [],
        },
    )


def _collect_filing(config: dict[str, Any], company: dict[str, Any], filing: Any, *, reason: str, result: SourceResult) -> None:
    form = _filing_form(filing)
    ticker = company_ticker(company)
    is_eight_k = form in {"8-K", "8-K/A"}
    all_attachments = _iter_attachments(filing)
    attachments = [att for att in all_attachments if _attachment_is_relevant(att, form, is_eight_k=is_eight_k)]
    if not is_eight_k:
        # The primary 10-Q/10-K already contains the complete filing.  A few
        # servicing schedules are useful for extraction, but archiving every
        # inline-XBRL report row creates a historical flood and does not add
        # authoritative content beyond that primary document.
        primary = [att for att in attachments if _attachment_sequence(att) == "1"]
        supplemental = [att for att in attachments if _attachment_sequence(att) != "1"]
        try:
            max_supplemental = max(0, min(int(_sources_config(config).get("max_periodic_exhibits", 0)), 30))
        except (TypeError, ValueError):
            max_supplemental = 0
        attachments = primary + supplemental[:max_supplemental]
    if not attachments:
        result.pending.append({
            "source": "sec",
            "source_key": f"{ticker}:sec",
            "issuer": company_name(company),
            "ticker": ticker,
            "cik": normalize_cik(company.get("cik")),
            "kind": form,
            "url": _filing_url(filing),
            "accession": _filing_accession(filing),
            "document": "",
            "reason": "SEC attachment metadata/content unavailable; no original bytes archived",
        })
        return

    # Download each selected exhibit at most once.  The payload map lets us
    # determine a verified 8-K earnings period before assigning it to the
    # primary report, release, and presentation in the same accession.
    payloads: dict[int, tuple[bytes, bool, str]] = {}
    failed: set[int] = set()
    blocked_index: int | None = None
    for index, attachment in enumerate(attachments):
        try:
            payload, original_bytes = _attachment_payload_with_fidelity(attachment)
            extension = _attachment_extension(attachment, _attachment_url(attachment, filing), payload)
            payloads[id(attachment)] = (payload, original_bytes, extension)
            if not payload:
                failed.add(id(attachment))
                result.pending.append({
                    "source": "sec",
                    "source_key": f"{ticker}:sec",
                    "issuer": company_name(company),
                    "ticker": ticker,
                    "cik": normalize_cik(company.get("cik")),
                    "kind": form,
                    "url": _attachment_url(attachment, filing),
                    "accession": _filing_accession(filing),
                    "document": _attachment_filename(attachment),
                    "reason": "attachment returned no content",
                })
        except Exception as exc:
            failed.add(id(attachment))
            blocked = _is_blocked(exc)
            result.errors.append({
                "source": "sec",
                "source_key": f"{ticker}:sec",
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": normalize_cik(company.get("cik")),
                "accession": _filing_accession(filing),
                "kind": form,
                "url": _attachment_url(attachment, filing),
                "document": _attachment_filename(attachment),
                "error": _safe_error(exc),
                "blocked": blocked,
            })
            if blocked:
                blocked_index = index
                break

    if blocked_index is not None:
        # Keep the failed and not-yet-inspected exhibits as durable work.  A
        # blocked response must stop the current accession and the outer
        # discovery loop will stop subsequent filings as well.
        for remaining in attachments[blocked_index:]:
            result.pending.append({
                "source": "sec",
                "source_key": f"{ticker}:sec",
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": normalize_cik(company.get("cik")),
                "kind": form,
                "url": _attachment_url(remaining, filing),
                "accession": _filing_accession(filing),
                "document": _attachment_filename(remaining),
                "reason": "deferred after SEC access block",
            })

    if is_eight_k:
        # Item metadata is useful for discovery, but it is too broad to decide
        # which downloaded rows belong in the reporting package.  Re-check the
        # opening content after download and retain the filed primary only when
        # an earnings/servicing/material exhibit (or an equally explicit
        # primary report) is present.  This prevents legal and leadership
        # accessions from becoming unknown-period report documents.
        primary = next((attachment for attachment in attachments if _attachment_sequence(attachment) == "1"), None)
        material_exhibits = []
        for attachment in attachments:
            if attachment is primary or id(attachment) in failed:
                continue
            payload_info = payloads.get(id(attachment))
            if payload_info is None:
                continue
            payload, _original_bytes, extension = payload_info
            if _eight_k_material_attachment(attachment, payload, extension):
                material_exhibits.append(attachment)
        if material_exhibits:
            attachments = ([primary] if primary is not None else []) + material_exhibits
        elif primary is not None:
            primary_info = payloads.get(id(primary))
            if primary_info is not None and _eight_k_material_attachment(primary, primary_info[0], primary_info[2]):
                attachments = [primary]
            else:
                # No reportable exhibit survived content checks.  Any source
                # errors/pending rows recorded above remain visible, while a
                # routine accession is simply excluded from the document set.
                return
        else:
            return

    event_period, conflicts = _filing_event_period(filing, attachments, payloads)
    if conflicts:
        result.pending.append({
            "source": "sec",
            "source_key": f"{ticker}:sec",
            "issuer": company_name(company),
            "ticker": ticker,
            "cik": normalize_cik(company.get("cik")),
            "kind": form,
            "url": _filing_url(filing),
            "accession": _filing_accession(filing),
            "document": "",
            "reason": "conflicting explicit reporting periods in one 8-K accession: " + ", ".join(conflicts),
        })

    # A linked HTML deck can have many image rows.  Retain matching original
    # bytes as related assets while keeping only the HTML exhibit as a
    # reportable Document.  Asset archives are bounded to avoid a malformed
    # document turning one filing into an unbounded download.
    assets_by_name: dict[str, list[Any]] = {}
    for asset in all_attachments:
        if not _attachment_is_asset(asset):
            continue
        name = Path(_attachment_filename(asset)).name.lower()
        if name:
            assets_by_name.setdefault(name, []).append(asset)
    asset_cache: dict[int, tuple[bytes, bool, str]] = {}
    try:
        max_assets = max(1, min(int(_sources_config(config).get("max_assets_per_exhibit", 48)), 100))
    except (TypeError, ValueError):
        max_assets = 48

    for index, attachment in enumerate(attachments):
        if id(attachment) in failed:
            continue
        try:
            payload_info = payloads.get(id(attachment))
            if payload_info is None:
                continue
            payload, _original_bytes, extension = payload_info
            own_period = _attachment_period_hint(attachment, payload, extension, form=form)
            period = own_period
            period_basis = "attachment_title_or_content"
            if is_eight_k and event_period != "unknown" and own_period == "unknown":
                period = event_period
                period_basis = "same_accession_earnings_exhibit"
            elif not is_eight_k and event_period != "unknown":
                period = event_period
                period_basis = "filing_period_of_report"
            asset_records: list[dict[str, Any]] = []
            if is_eight_k and extension in {".html", ".htm", ".xml"}:
                references = _attachment_references(payload, extension)
                related: list[Any] = []
                for name in references:
                    related.extend(assets_by_name.get(name, []))
                # Preserve deterministic attachment order and deduplicate rows
                # that a page references more than once.
                seen_asset_ids: set[int] = set()
                related = [asset for asset in related if not (id(asset) in seen_asset_ids or seen_asset_ids.add(id(asset)))]
                for asset in related[:max_assets]:
                    try:
                        cached = asset_cache.get(id(asset))
                        if cached is None:
                            asset_payload, asset_original = _attachment_payload_with_fidelity(asset)
                            asset_extension = _attachment_extension(asset, _attachment_url(asset, filing), asset_payload)
                            cached = (asset_payload, asset_original, asset_extension)
                            asset_cache[id(asset)] = cached
                        asset_payload, asset_original, asset_extension = cached
                        if not asset_payload:
                            continue
                        asset_path, asset_digest = archive_bytes(
                            _storage(config),
                            ticker=ticker,
                            period=period or "unknown",
                            kind="asset-" + clean_part(_attachment_filename(asset), default="linked"),
                            payload=asset_payload,
                            extension=asset_extension,
                            accession=_filing_accession(filing),
                        )
                        asset_records.append({
                            "document": _attachment_filename(asset),
                            "sequence": _attachment_sequence(asset),
                            "url": _attachment_url(asset, filing),
                            "path": str(asset_path),
                            "content_hash": asset_digest,
                            "original_bytes": asset_original,
                        })
                    except Exception as exc:
                        result.errors.append({
                            "source": "sec",
                            "source_key": f"{ticker}:sec",
                            "issuer": company_name(company),
                            "ticker": ticker,
                            "cik": normalize_cik(company.get("cik")),
                            "accession": _filing_accession(filing),
                            "kind": form,
                            "url": _attachment_url(asset, filing),
                            "document": _attachment_filename(asset),
                            "error": _safe_error(exc),
                            "blocked": _is_blocked(exc),
                        })
                        if _is_blocked(exc):
                            # Asset retrieval is still SEC work.  Preserve the
                            # parent exhibit as pending so it is retried with
                            # its related assets on the next run.
                            result.pending.append({
                                "source": "sec",
                                "source_key": f"{ticker}:sec",
                                "issuer": company_name(company),
                                "ticker": ticker,
                                "cik": normalize_cik(company.get("cik")),
                                "kind": form,
                                "url": _attachment_url(attachment, filing),
                                "accession": _filing_accession(filing),
                                "document": _attachment_filename(attachment),
                                "reason": "parent exhibit deferred after linked asset access block",
                            })
                            break
            document = _document_from_attachment(
                config,
                company,
                filing,
                attachment,
                reason=reason,
                payload_info=payload_info,
                period_override=period,
                period_basis=period_basis,
                assets=asset_records,
            )
            if document is not None:
                result.documents.append(document)
            else:
                result.pending.append({
                    "source": "sec",
                    "source_key": f"{ticker}:sec",
                    "issuer": company_name(company),
                    "ticker": ticker,
                    "cik": normalize_cik(company.get("cik")),
                    "kind": form,
                    "url": _attachment_url(attachment, filing),
                    "accession": _filing_accession(filing),
                    "document": _attachment_filename(attachment),
                    "reason": "attachment returned no content",
                })
        except Exception as exc:
            result.errors.append({
                "source": "sec",
                "source_key": f"{ticker}:sec",
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": normalize_cik(company.get("cik")),
                "accession": _filing_accession(filing),
                "kind": form,
                "url": _attachment_url(attachment, filing),
                "document": _attachment_filename(attachment),
                "error": _safe_error(exc),
                "blocked": _is_blocked(exc),
            })
            if _is_blocked(exc):
                # Stop immediately on a blocked response.  Retain the current
                # and remaining attachment work so a later run can retry after
                # the issuer/SEC access condition changes.
                start_index = index
                for remaining in attachments[start_index + 1 :]:
                    result.pending.append({
                        "source": "sec",
                        "source_key": f"{ticker}:sec",
                        "issuer": company_name(company),
                        "ticker": ticker,
                        "cik": normalize_cik(company.get("cik")),
                        "kind": form,
                        "url": _attachment_url(remaining, filing),
                        "accession": _filing_accession(filing),
                        "document": _attachment_filename(remaining),
                        "reason": "deferred after SEC access block",
                    })
                result.pending.append({
                    "source": "sec",
                    "source_key": f"{ticker}:sec",
                    "issuer": company_name(company),
                    "ticker": ticker,
                    "cik": normalize_cik(company.get("cik")),
                    "kind": form,
                    "url": _attachment_url(attachment, filing),
                    "accession": _filing_accession(filing),
                    "document": _attachment_filename(attachment),
                    "reason": "attachment deferred after SEC access block",
                })
                return


def _bounded_candidates(config: dict[str, Any], candidates: list[Any]) -> list[Any]:
    """Select latest/prior reporting packages plus a small recent 8-K window.

    A bootstrap lookback can span several quarters.  Downloading every 8-K in
    that window would create a historical archive before the first useful
    briefing, so periodic reports are limited to latest, previous, and
    same-quarter prior-year periods while recent substantive 8-Ks remain
    available for event updates.
    """

    periodic_forms = {"10-Q", "10-Q/A", "10-K", "10-K/A"}
    delay_forms = {"NT 10-Q", "NT 10-Q/A", "NT 10-K", "NT 10-K/A"}
    completed_year, completed_quarter = latest_completed_period()

    def completed(period: str) -> bool:
        match = re.fullmatch(r"(20\d{2})-(?:Q([1-4])|FY)", period or "")
        if not match:
            return period == "unknown"
        year = int(match.group(1))
        quarter = int(match.group(2) or 4)
        return (year, quarter) <= (completed_year, completed_quarter)

    periodic = [
        filing
        for filing in candidates
        if _filing_form(filing) in periodic_forms and completed(_candidate_period(filing))
    ]
    periodic.sort(key=lambda filing: (_period_order(_candidate_period(filing)), _filing_date(filing), _filing_accession(filing)), reverse=True)
    period_labels = []
    for filing in periodic:
        label = _candidate_period(filing)
        if label and label != "unknown" and label not in period_labels:
            period_labels.append(label)
    keep_periods: set[str] = set(period_labels[:2])
    if period_labels:
        latest = period_labels[0]
        match = re.fullmatch(r"(20\d{2})-Q([1-4])", latest)
        if match:
            prior_year = f"{int(match.group(1)) - 1}-Q{match.group(2)}"
            if prior_year in period_labels:
                keep_periods.add(prior_year)
        elif latest.endswith("-FY"):
            prior_year = f"{int(latest[:4]) - 1}-FY"
            if prior_year in period_labels:
                keep_periods.add(prior_year)
    # Include a couple of unknown-period periodic reports for issuers whose
    # EdgarTools metadata omits report dates.
    selected_periodic = []
    unknown_periodic = 0
    for filing in periodic:
        period = _candidate_period(filing)
        if period in keep_periods and completed(period):
            selected_periodic.append(filing)
        elif period == "unknown" and unknown_periodic < 2:
            selected_periodic.append(filing)
            unknown_periodic += 1
    # Preserve the latest completed annual report as issuer context even when
    # the quarter comparison set is Q2/Q1/same-quarter-prior-year.  The
    # annual filing carries servicing definitions and year-end balances that
    # are needed to interpret a current quarterly package.
    annual = [
        filing
        for filing in periodic
        if _candidate_period(filing).endswith("-FY") and completed(_candidate_period(filing))
    ]
    annual.sort(key=lambda filing: (_filing_date(filing), _filing_accession(filing)), reverse=True)
    if annual and annual[0] not in selected_periodic:
        selected_periodic.append(annual[0])

    eight_k = [filing for filing in candidates if _filing_form(filing) in {"8-K", "8-K/A"}]
    eight_k.sort(key=lambda filing: (_filing_date(filing), _filing_accession(filing)), reverse=True)
    try:
        max_8k = max(1, min(int(_sources_config(config).get("max_relevant_8k_per_company", 10)), 20))
    except (TypeError, ValueError):
        max_8k = 10
    selected_8k = eight_k[:max_8k]
    delays = [filing for filing in candidates if _filing_form(filing) in delay_forms]
    delays.sort(key=lambda filing: (_filing_date(filing), _filing_accession(filing)), reverse=True)
    selected = selected_periodic + selected_8k + delays[:3]
    selected_ids: set[tuple[str, str]] = set()
    unique: list[Any] = []
    for filing in selected:
        key = (_filing_form(filing), _filing_accession(filing))
        if key in selected_ids:
            continue
        selected_ids.add(key)
        unique.append(filing)
    return unique


def _discover_sec_locked(
    config: dict[str, Any],
    company: dict[str, Any],
    *,
    bootstrap: bool = False,
    checkpoints: dict[str, str] | None = None,
) -> SourceResult:
    """Perform one issuer's SEC work while the caller holds the SEC guard."""

    result = SourceResult()
    ticker = company_ticker(company)
    cik = normalize_cik(company.get("cik"))
    source_key = f"{ticker}:sec"
    if not ticker:
        result.errors.append({"source": "sec", "source_key": source_key, "error": "company ticker is missing"})
        return result
    if not cik:
        result.errors.append({"source": "sec", "source_key": source_key, "ticker": ticker, "error": "company CIK is missing"})
        return result
    start = _lookback_start(config, bootstrap=bootstrap, checkpoints=checkpoints, key=source_key)
    filing_date = f"{start}:"
    try:
        from edgar import Company

        edgar_company = Company(cik)
        if getattr(edgar_company, "not_found", False):
            result.errors.append({"source": "sec", "source_key": source_key, "ticker": ticker, "cik": cik, "error": "CIK not found by EdgarTools", "inactive": True})
            return result
        filings = _call_get_filings(edgar_company, filing_date)
        candidates = _iter_filings(filings)

        # A checkpoint is intentionally narrow for routine incremental runs,
        # but a prior run can advance it after seeing only a routine 8-K (or
        # after a source process was interrupted before its periodic package
        # was persisted).  If the narrowed result has no periodic metadata,
        # refresh the normal bounded lookback once while still holding the
        # shared guard.  This prevents a current earnings package from being
        # silently skipped behind an otherwise healthy checkpoint.
        periodic_forms = {"10-Q", "10-Q/A", "10-K", "10-K/A", "NT 10-Q", "NT 10-Q/A", "NT 10-K", "NT 10-K/A"}
        checkpoint_present = bool((checkpoints or {}).get(source_key))
        has_periodic = any(
            _filing_form(filing) in periodic_forms and _candidate_period(filing) != "unknown"
            for filing in candidates
        )
        if checkpoint_present and not has_periodic:
            fallback_start = _lookback_start(config, bootstrap=bootstrap, checkpoints={}, key=source_key)
            if fallback_start != start:
                try:
                    fallback_filings = _call_get_filings(edgar_company, f"{fallback_start}:")
                    by_accession = {_filing_accession(filing): filing for filing in candidates if _filing_accession(filing)}
                    for filing in _iter_filings(fallback_filings):
                        accession = _filing_accession(filing)
                        if accession and accession in by_accession:
                            continue
                        if accession:
                            by_accession[accession] = filing
                        else:
                            candidates.append(filing)
                    candidates = list(by_accession.values()) if by_accession else candidates
                except Exception as exc:
                    blocked = _is_blocked(exc)
                    result.errors.append({
                        "source": "sec",
                        "source_key": source_key,
                        "ticker": ticker,
                        "cik": cik,
                        "error": "fallback periodic discovery failed: " + _safe_error(exc),
                        "blocked": blocked,
                        "retryable": not blocked,
                    })
                    result.checked.append(ticker)
                    return result
    except Exception as exc:
        result.errors.append({
            "source": "sec",
            "source_key": source_key,
            "ticker": ticker,
            "cik": cik,
            "error": _safe_error(exc),
            "blocked": _is_blocked(exc),
            "retryable": not _is_blocked(exc),
        })
        return result

    # Keep the package bounded while preserving every relevant filing in the
    # overlapping lookback window.  EdgarTools returns newest first, but sort
    # defensively for test doubles and alternate library versions.
    today_text = datetime.now(timezone.utc).date().isoformat()
    # EdgarTools can expose an index row for a future-dated scheduled filing
    # when the SEC index is ahead of the local clock.  Such a row is retained
    # as neither a report nor an event until its filing date is reached.
    candidates = [filing for filing in candidates if not _filing_date(filing) or _filing_date(filing) <= today_text]
    candidates.sort(key=lambda filing: (_filing_date(filing), _filing_accession(filing)), reverse=True)
    pending_rows = config.get("_pending", [])
    pending_resolution_blocked = False
    pending_accessions: list[str] = []
    if isinstance(pending_rows, list):
        for row in pending_rows:
            if not isinstance(row, dict) or row.get("source_key") != source_key:
                continue
            accession = str(row.get("accession") or "").strip()
            if accession and accession not in pending_accessions:
                pending_accessions.append(accession)
    existing_accessions = {_filing_accession(filing) for filing in candidates}
    # A failed attachment can be older than the normal lookback.  Resolve its
    # accession with the documented Company.get_filings(accession_number=...)
    # API so pending work survives moving checkpoints.
    if pending_accessions:
        try:
            from edgar import Company

            edgar_company = Company(cik)
            for accession in pending_accessions:
                if accession in existing_accessions:
                    continue
                try:
                    try:
                        pending_filings = edgar_company.get_filings(
                            accession_number=accession,
                            amendments=True,
                            trigger_full_load=True,
                        )
                    except TypeError as type_error:
                        if "trigger_full_load" not in str(type_error) and "unexpected keyword" not in str(type_error):
                            raise
                        pending_filings = edgar_company.get_filings(accession_number=accession, amendments=True)
                    resolved = _iter_filings(pending_filings)
                    if resolved:
                        candidates.extend(resolved)
                        existing_accessions.update(_filing_accession(filing) for filing in resolved)
                    else:
                        matching = next((row for row in pending_rows if isinstance(row, dict) and row.get("accession") == accession), {})
                        result.pending.append({
                            "source": "sec",
                            "source_key": source_key,
                            "issuer": company_name(company),
                            "ticker": ticker,
                            "cik": cik,
                            "kind": str(matching.get("kind", "")),
                            "url": str(matching.get("url", "")),
                            "accession": accession,
                            "document": str(matching.get("document", "")),
                            "reason": "pending SEC accession could not be resolved; retry on next run",
                        })
                except Exception as exc:
                    result.errors.append({
                        "source": "sec",
                        "source_key": source_key,
                        "issuer": company_name(company),
                        "ticker": ticker,
                        "cik": cik,
                        "accession": accession,
                        "error": _safe_error(exc),
                        "blocked": _is_blocked(exc),
                        "retryable": not _is_blocked(exc),
                    })
                    if _is_blocked(exc):
                        pending_resolution_blocked = True
                        break
        except Exception as exc:
            result.errors.append({
                "source": "sec",
                "source_key": source_key,
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": cik,
                "error": "pending accession resolution failed: " + _safe_error(exc),
                "blocked": _is_blocked(exc),
                "retryable": not _is_blocked(exc),
            })
            pending_resolution_blocked = _is_blocked(exc)
    candidates.sort(key=lambda filing: (_filing_date(filing), _filing_accession(filing)), reverse=True)
    if pending_resolution_blocked:
        # The discovery response itself succeeded, but content retrieval is
        # blocked.  Keep every bounded candidate accession for a later retry
        # and stop before issuing another EdgarTools content request.
        for deferred in candidates:
            result.pending.append({
                "source": "sec",
                "source_key": source_key,
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": cik,
                "kind": _filing_form(deferred),
                "url": _filing_url(deferred),
                "accession": _filing_accession(deferred),
                "document": "",
                "reason": "deferred after SEC access block while resolving pending accession",
            })
        result.checked.append(ticker)
        return result
    bounded = _bounded_candidates(config, candidates)
    # Resolved pending accessions are always included, even if their period is
    # older than the bootstrap package selection.
    pending_set = set(pending_accessions)
    if pending_set:
        for filing in candidates:
            if _filing_accession(filing) in pending_set and filing not in bounded:
                bounded.append(filing)
    candidates = bounded
    try:
        max_filings = max(1, int(_sources_config(config).get("max_sec_filings_per_company", 30)))
    except (TypeError, ValueError):
        max_filings = 30
    selected = 0
    blocked = False
    for filing in candidates:
        form = _filing_form(filing)
        if form in {"10-Q", "10-Q/A", "10-K", "10-K/A", "NT 10-Q", "NT 10-Q/A", "NT 10-K", "NT 10-K/A"}:
            relevant, reason = True, "periodic report or delay notice"
        elif form in {"8-K", "8-K/A"}:
            items = _filing_items(filing)
            relevant, reason = _is_relevant_8k(filing, items)
        else:
            continue
        if not relevant:
            continue
        if selected >= max_filings:
            result.pending.append({
                "source": "sec",
                "source_key": source_key,
                "issuer": company_name(company),
                "ticker": ticker,
                "cik": cik,
                "kind": form,
                "url": _filing_url(filing),
                "reason": f"SEC filing cap reached ({max_filings}); will revisit in the next overlapping run",
                "form": form,
                "accession": _filing_accession(filing),
                "document": "",
            })
            continue
        selected += 1
        before_errors = len(result.errors)
        _collect_filing(config, company, filing, reason=reason, result=result)
        if any(bool(error.get("blocked")) for error in result.errors[before_errors:]):
            blocked = True
            # Preserve all filings not yet inspected as durable pending work.
            for deferred in candidates[candidates.index(filing) + 1 :]:
                result.pending.append({
                    "source": "sec",
                    "source_key": source_key,
                    "issuer": company_name(company),
                    "ticker": ticker,
                    "cik": cik,
                    "kind": _filing_form(deferred),
                    "url": _filing_url(deferred),
                    "accession": _filing_accession(deferred),
                    "document": "",
                    "reason": "deferred after SEC access block",
                })
            break
    result.checked.append(ticker)
    # Do not advance a source checkpoint while material work is pending or an
    # access/content failure occurred; root state will retry durable pending
    # accessions outside the normal lookback window.
    source_pending = any(item.get("source_key") == source_key for item in result.pending)
    source_errors = any(item.get("source_key") == source_key for item in result.errors)
    if not source_pending and not source_errors:
        result.checkpoints[source_key] = utc_now()
    return result


def discover_sec(
    config: dict[str, Any],
    company: dict[str, Any],
    *,
    bootstrap: bool = False,
    checkpoints: dict[str, str] | None = None,
) -> SourceResult:
    """Discover and archive relevant SEC filings for one configured company.

    ``EDGAR_IDENTITY`` is required before importing/using EdgarTools.  The
    value is never returned or logged; status callers should expose only
    ``configured=yes/no``.
    """

    result = SourceResult()
    ticker = company_ticker(company)
    source_key = f"{ticker}:sec"
    if not os.environ.get("EDGAR_IDENTITY", "").strip():
        result.errors.append({
            "source": "sec",
            "source_key": source_key,
            "ticker": ticker,
            "cik": normalize_cik(company.get("cik")),
            "error": "EDGAR_IDENTITY is not configured (configured=no)",
            "credentials": "configured=no",
        })
        return result
    lock_path = _sources_config(config).get("sec_lock_path") or config.get("sec_lock_path")
    with sec_acquisition_guard(_storage(config), timeout=float(_sources_config(config).get("lock_timeout", 0.0) or 0.0), lock_path=lock_path) as lock:
        if not lock.get("acquired"):
            result.errors.append({
                "source": "sec",
                "source_key": source_key,
                "ticker": ticker,
                "error": str(lock.get("error") or "SEC acquisition lock unavailable"),
                "retryable": True,
            })
            return result
        if lock.get("manager_verified") is False:
            result.errors.append({
                "source": "sec",
                "source_key": source_key,
                "ticker": ticker,
                "error": "EdgarTools rate limiter was initialized above the 5 requests/second budget; SEC work stopped",
                "retryable": False,
            })
            return result
        return _discover_sec_locked(config, company, bootstrap=bootstrap, checkpoints=checkpoints)


def doctor_sec(config: dict[str, Any]) -> dict[str, Any]:
    """Return safe SEC setup diagnostics and one bounded live access probe.

    The probe intentionally uses the first enabled issuer only.  It validates
    that the configured identity, EdgarTools installation, shared lock, rate
    setting, and public filing endpoint work together without downloading a
    filing or exhibit.  Importing EdgarTools happens inside the same guard as
    collection so a doctor cannot initialize an unbounded HTTP manager.
    """

    identity_configured = bool(os.environ.get("EDGAR_IDENTITY", "").strip())
    try:
        from importlib.metadata import version as package_version

        version = package_version("edgartools")
    except Exception:
        version = "unknown"
    output = {
        "source": "sec",
        "available": version != "unknown",
        "identity": f"configured={'yes' if identity_configured else 'no'}",
        "edgartools_version": version,
        "rate_limit_per_second": 5,
        "lock_path": str(shared_sec_lock_path(_sources_config(config).get("sec_lock_path") or config.get("sec_lock_path"))),
        "access_tested": False,
        "access_status": "not_tested",
    }
    if not identity_configured:
        output.update({"access_status": "not_configured", "detail": "EDGAR_IDENTITY is not configured"})
        return output
    companies = config.get("companies", [])
    probe = None
    if isinstance(companies, list):
        for company in companies:
            if not isinstance(company, dict):
                continue
            enabled = company.get("enabled", True)
            if isinstance(enabled, str):
                enabled = enabled.strip().lower() not in {"0", "false", "no", "off", "disabled"}
            if enabled:
                probe = company
                break
    if probe is None:
        output.update({"access_status": "not_tested", "detail": "No enabled issuer is configured"})
        return output
    ticker = company_ticker(probe)
    cik = normalize_cik(probe.get("cik"))
    output.update({"probe_ticker": ticker, "probe_cik": cik})
    try:
        timeout = float(_sources_config(config).get("lock_timeout", 0.0) or 0.0)
    except (TypeError, ValueError):
        timeout = 0.0
    with sec_acquisition_guard(_storage(config), timeout=timeout, lock_path=_sources_config(config).get("sec_lock_path") or config.get("sec_lock_path")) as lock:
        if not lock.get("acquired"):
            output.update({"access_tested": True, "access_status": "blocked_lock", "detail": "SEC acquisition lock is held by another process"})
            return output
        if lock.get("manager_verified") is False:
            output.update({"access_tested": True, "access_status": "blocked_rate_configuration", "detail": "EdgarTools rate limiter could not be verified at or below 5 requests/second"})
            return output
        try:
            from edgar import Company

            edgar_company = Company(cik)
            if getattr(edgar_company, "not_found", False):
                output.update({"access_tested": True, "access_status": "not_found"})
                return output
            through = datetime.now(timezone.utc).date()
            since = through - timedelta(days=30)
            filings = edgar_company.get_filings(
                form=["10-Q", "10-K", "8-K"],
                filing_date=(since.isoformat(), through.isoformat()),
                amendments=True,
                trigger_full_load=False,
            )
            # Counting the bounded metadata rows proves the endpoint was
            # reached without downloading filing attachments.
            count = len(_iter_filings(filings))
            output.update({"access_tested": True, "access_status": "ok", "filings_seen": count, "query_days": 30})
        except Exception as exc:
            output.update({"access_tested": True, "access_status": "blocked" if _is_blocked(exc) else "error", "error": _safe_error(exc)})
    return output
