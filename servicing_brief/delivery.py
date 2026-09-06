"""Email preview and SMTP delivery for servicing briefings.

The module deliberately keeps delivery state separate from collection and
extraction state.  A generated ``.eml`` is the durable unit of delivery: its
message key is stored in SQLite and is acknowledged only after the SMTP
provider returns successfully from ``DATA``/``send_message``.

The normal application path leaves ``email.send_enabled`` false.  The CLI can
set the private ``_send`` runtime flag after the user explicitly requests a
send.  This module never guesses a recipient from a document or from SEC
identity information.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html as html_lib
import json
import mimetypes
import os
import re
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.generator import BytesGenerator
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

try:
    from filelock import FileLock, Timeout as FileLockTimeout
except ImportError:  # pragma: no cover - dependency is declared by the project
    FileLock = None  # type: ignore[assignment,misc]

from .models import Document
from .reader_content import assert_reader_mime
from .branding import BrandRegistryError, NEUTRAL_BRAND, apply_page_theme, lookup_brand, page_theme, validate_page_theme
from .company_boundary import (
    BOUNDARY_HEADER,
    BOUNDARY_VERSION,
    IDENTITY_HEADER,
    TEST_HEADER,
    CompanyBoundaryError,
    has_company_identity,
    report_identity,
    require_company_boundary,
    validate_mime_message,
    validate_report_surfaces,
)


DEFAULT_MAX_MESSAGE_BYTES = 15 * 1024 * 1024
DEFAULT_RETRY_ATTEMPTS = 2
DEFAULT_LOCK_TIMEOUT_SECONDS = 0
_KEY_HEADER = "X-Servicing-Brief-Message-Key"
_PACKAGE_HEADER = "X-Servicing-Brief-Package-Key"
_GENERATED_PRINT_MARKERS = ("generated_print", "generated-print", "generated print")
_SMTP_SECURITY_MODES = {"ssl", "smtps", "implicit_tls", "implicit-tls", "starttls"}
_PLAIN_EMAIL = re.compile(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+\Z")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BRAND_IMG_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_ATTRIBUTE = re.compile(
    r"(?P<lead>[\s<])"
    r"(?P<name>[A-Za-z_:][A-Za-z0-9_.:-]*)\s*=\s*"
    r"(?:(?P<quote>[\"'])(?P<quoted>.*?)(?P=quote)|(?P<bare>[^\s\"'`=<>]+))",
    re.IGNORECASE | re.DOTALL,
)
_BRAND_ATTRIBUTE_MARKER = re.compile(
    r"[\s<]data-brand-ticker(?:\s|=|/?>)", re.IGNORECASE
)
_TICKER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,19}\Z")
_PNG_DATA_URI = re.compile(
    r"data:(?P<media>image/png);base64,(?P<data>[A-Za-z0-9+/=\s]+)\Z",
    re.IGNORECASE,
)


class DeliveryError(RuntimeError):
    """Base exception for local message preparation errors."""


class BrandingError(DeliveryError, ValueError):
    """Raised when an HTML logo cannot be mapped to a verified local asset."""


class DeliveryOverlap(DeliveryError):
    """Raised internally when another process owns the delivery lock."""


@dataclass
class _Attachment:
    """A verified local original ready for MIME encoding."""

    document: Any
    path: Path
    data: bytes
    filename: str
    mime_type: str
    document_id: str
    source_url: str
    local_path: str
    content_hash: str


@dataclass(frozen=True)
class _BrandAsset:
    """Verified local PNG and its stable content identifier."""

    ticker: str
    cid: str
    data: bytes
    public_logo_url: str
    logo_alt: str


def _field(value: Any, name: str, default: Any = "") -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("email", {})
    return value if isinstance(value, Mapping) else {}


def _setting(settings: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in settings and settings[name] not in (None, ""):
            return settings[name]
    return default


def _recipient_error(config: Mapping[str, Any]) -> str:
    """Return a safe validation message for the single configured recipient."""

    settings = _settings(config)
    raw = _setting(settings, "recipient", "to", default=None)
    if raw is None:
        raw = _setting(config, "recipient", "to", default=None)
    if raw is None:
        return ""
    if isinstance(raw, str):
        if not raw.strip():
            return ""
        values = [item.strip() for item in re.split(r"[,;]\s*", raw) if item.strip()]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        values = [str(raw).strip()]
    if len(values) != 1:
        return "exactly one explicit email.recipient is required"
    if not _PLAIN_EMAIL.fullmatch(values[0]):
        return "email.recipient must be one explicit plain email address"
    return ""


def _sender_error(config: Mapping[str, Any]) -> str:
    """Return a safe validation message for the configured sender."""

    sender = _from_address(config)
    if not sender:
        return ""
    if not _PLAIN_EMAIL.fullmatch(sender):
        return "email.from_address must be one explicit plain email address"
    return ""


def _smtp_security_error(config: Mapping[str, Any]) -> str:
    security = str(_smtp_credentials(config).get("security", "") or "").lower()
    if security not in _SMTP_SECURITY_MODES:
        return (
            "unsupported SMTP security mode; use implicit TLS (ssl) or "
            "STARTTLS (starttls)"
        )
    return ""


def _recipients(config: Mapping[str, Any]) -> list[str]:
    """Return configured recipients while preserving their explicit values.

    TOML users commonly choose ``to``, ``recipient`` or ``recipients``.  A
    comma/semicolon separated string is accepted for convenience.  Header
    validation is intentionally left to SMTP for unusual but valid addresses;
    an empty value always means no recipient.
    """

    settings = _settings(config)
    raw = _setting(settings, "recipient", "to", default=None)
    if raw is None:
        raw = _setting(config, "recipient", "to", default=None)
    if raw is None:
        return []
    if isinstance(raw, str):
        values = re.split(r"[,;]\s*", raw)
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        # The product is intentionally single-recipient.  Refuse a list so a
        # config typo cannot unexpectedly fan out a financial briefing.
        if len(raw) != 1:
            return []
        values = [str(raw[0])]
    else:
        values = [str(raw)]
    return [item.strip() for item in values if item and item.strip()]


def _from_address(config: Mapping[str, Any]) -> str:
    settings = _settings(config)
    value = _setting(settings, "from_address", "sender", "from", default=None)
    if value is None:
        value = _setting(config, "from_address", "sender", "from", default=None)
    return str(value).strip() if value else ""


def _send_enabled(config: Mapping[str, Any]) -> bool:
    """Check the explicit runtime send gate.

    ``email.enabled`` is deliberately ignored: it is useful as a doctor's
    configuration indicator but must not turn on recurring sends by itself.
    The CLI can set ``config['_send'] = True`` for an explicit invocation.
    """

    settings = _settings(config)
    return bool(config.get("_send") is True or settings.get("send_enabled") is True)


def _max_message_bytes(config: Mapping[str, Any]) -> int:
    settings = _settings(config)
    raw_bytes = _setting(
        settings,
        "max_message_bytes",
        "max_encoded_bytes",
        "message_size_bytes",
        default=None,
    )
    if raw_bytes is not None:
        try:
            value = int(raw_bytes)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    raw_mib = _setting(settings, "max_message_mib", "max_size_mib", default=15)
    try:
        value = int(float(raw_mib) * 1024 * 1024)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    return DEFAULT_MAX_MESSAGE_BYTES


def _retry_attempts(config: Mapping[str, Any]) -> int:
    settings = _settings(config)
    raw = _setting(settings, "retry_attempts", "max_retries", default=DEFAULT_RETRY_ATTEMPTS)
    try:
        # This is the total number of attempts, rather than an unbounded retry
        # count.  A value of one disables retries while retaining the same API.
        return max(1, min(5, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_RETRY_ATTEMPTS


def _lock_timeout(config: Mapping[str, Any]) -> float:
    settings = _settings(config)
    raw = _setting(settings, "lock_timeout_seconds", default=DEFAULT_LOCK_TIMEOUT_SECONDS)
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_LOCK_TIMEOUT_SECONDS


def _smtp_credentials(config: Mapping[str, Any]) -> dict[str, Any]:
    settings = _settings(config)
    # Gmail's documented authenticated SMTP endpoints are the selected real
    # integration.  Host/port/security remain configurable for test doubles
    # and compatible Gmail relay setups, but no speculative provider adapters
    # are implemented here.
    host = _setting(settings, "smtp_host", "host", default="smtp.gmail.com")
    port = _setting(settings, "smtp_port", "port", default=465)
    username_env = _setting(settings, "smtp_username_env", "username_env", default="SMTP_USERNAME")
    username = os.environ.get(str(username_env), "") or _setting(settings, "smtp_username", "username", default=None)
    password_env = _setting(
        settings,
        "smtp_password_env",
        "password_env",
        default="SMTP_PASSWORD",
    )
    password = os.environ.get(str(password_env), "") if password_env else ""
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 465
    configured_security = _setting(settings, "smtp_security", "security", default=None)
    security = str(configured_security).lower() if configured_security else ("ssl" if port == 465 else "starttls")
    try:
        timeout = max(1.0, float(_setting(settings, "smtp_timeout_seconds", "timeout_seconds", default=30)))
    except (TypeError, ValueError):
        timeout = 30.0
    return {
        "host": str(host).strip() if host else "",
        "port": port,
        "username": str(username).strip() if username else "",
        "password": str(password) if password not in (None, "") else "",
        "username_env": str(username_env) if username_env else "SMTP_USERNAME",
        "password_env": str(password_env) if password_env else "SMTP_PASSWORD",
        "security": security,
        "timeout": timeout,
    }


def _safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    value = re.sub(r"-+", "-", value).strip("-.")
    return value or "document"


def _document_id(document: Any, content_hash: str, url: str, path: str) -> str:
    value = str(_field(document, "id", "") or "")
    if value:
        return value
    return hashlib.sha256(f"{url}|{path}|{content_hash}".encode("utf-8")).hexdigest()[:24]


def _document_filename(document: Any, path: Path) -> str:
    metadata = _field(document, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    configured = metadata.get("filename") or _field(document, "filename", "")
    if configured:
        name = str(configured)
    else:
        issuer = _field(document, "issuer", "issuer") or "issuer"
        period = _field(document, "period", "unknown") or "unknown"
        kind = _field(document, "kind", "document") or "document"
        suffix = path.suffix.lower() or ".bin"
        name = f"{issuer}-{period}-{kind}{suffix}"
    # Keep the source extension when a metadata filename omitted one.
    if not Path(name).suffix and path.suffix:
        name += path.suffix.lower()
    return _safe_name(name)


def _is_unverified_generated_print(document: Any) -> bool:
    metadata = _field(document, "metadata", {})
    if not isinstance(metadata, Mapping):
        metadata = {}
    classification = str(_field(document, "classification", "") or "").lower()
    generated = bool(
        metadata.get("generated_print_copy")
        or metadata.get("generated_print")
        or any(marker in classification for marker in _GENERATED_PRINT_MARKERS)
    )
    if not generated:
        return False
    verified = bool(
        metadata.get("generated_print_copy_verified")
        or metadata.get("generated_print_verified")
        or metadata.get("verified")
    )
    return not verified


def _attachment_priority(document: Any) -> tuple[int, str, str]:
    """Order original copies by the briefing's attachment priority."""

    kind = str(_field(document, "kind", "document") or "document").lower()
    title = str(_field(document, "title", "") or "").lower()
    text = f"{kind} {title}"
    if any(token in text for token in ("presentation", "investor deck", "slides", "deck")):
        rank = 0
    elif any(token in text for token in ("release", "earnings release", "press release")):
        rank = 1
    elif "supplement" in text:
        rank = 2
    elif any(token in text for token in ("10-q", "10-k", "10q", "10k")):
        rank = 3
    elif "8-k" in text or "8k" in text:
        rank = 4
    else:
        rank = 5
    return rank, str(_field(document, "published", "") or ""), str(_field(document, "id", "") or "")


def _mime_type(document: Any, path: Path) -> str:
    value = str(_field(document, "mime_type", "") or "").strip()
    if value and "/" in value and value != "application/octet-stream":
        return value
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or value or "application/octet-stream"


def _load_attachments(documents: Iterable[Any]) -> tuple[list[_Attachment], list[dict[str, str]]]:
    """Read and hash originals, returning omission records for unusable copies."""

    attachments: list[_Attachment] = []
    omissions: list[dict[str, str]] = []
    seen: set[str] = set()
    for document in sorted(documents, key=_attachment_priority):
        path_value = str(_field(document, "path", "") or "")
        url = str(_field(document, "url", "") or "")
        document_id = str(_field(document, "id", "") or "")
        period = str(_field(document, "period", "unknown") or "unknown")
        kind = str(_field(document, "kind", "document") or "document")
        label = f"{_field(document, 'issuer', 'issuer')} {period} {kind}".strip()
        if _is_unverified_generated_print(document):
            omissions.append(
                {
                    "label": label,
                    "url": url,
                    "local_path": path_value,
                    "reason": "unverified generated print copy; original source link retained",
                }
            )
            continue
        if not path_value:
            omissions.append(
                {
                    "label": label,
                    "url": url,
                    "local_path": "",
                    "reason": "no locally archived original",
                }
            )
            continue
        path = Path(path_value)
        try:
            data = path.read_bytes()
        except OSError as exc:
            omissions.append(
                {
                    "label": label,
                    "url": url,
                    "local_path": path_value,
                    "reason": f"local archive unavailable ({exc.__class__.__name__})",
                }
            )
            continue
        content_hash = hashlib.sha256(data).hexdigest()
        expected_hash = str(_field(document, "content_hash", "") or "").lower()
        if expected_hash and expected_hash != content_hash.lower():
            omissions.append(
                {
                    "label": label,
                    "url": url,
                    "local_path": path_value,
                    "reason": "local archive hash differs from document metadata",
                }
            )
            continue
        document_id = _document_id(document, content_hash, url, path_value)
        dedupe_key = expected_hash or content_hash
        if dedupe_key in seen:
            # Duplicate source rows are retained in collection state but only
            # one original copy is attached to an email package.
            continue
        seen.add(dedupe_key)
        filename = _document_filename(document, path)
        mime_type = _mime_type(document, path)
        metadata = _field(document, "metadata", {})
        if path.suffix.lower() in {".html", ".htm"} and metadata.get("assets"):
            try:
                from .document_copies import html_bundle
                path, data = html_bundle(document, data)
                filename = Path(filename).stem + "-html-with-assets.zip"
                mime_type = "application/zip"
                content_hash = hashlib.sha256(data).hexdigest()
                path_value = str(path)
            except (OSError, ValueError, KeyError) as exc:
                omissions.append({"label": label, "url": url, "local_path": path_value,
                                  "reason": "Complete HTML/image bundle unavailable; archived HTML attached with source link (" + type(exc).__name__ + ")"})
        attachments.append(
            _Attachment(
                document=document,
                path=path,
                data=data,
                filename=filename,
                mime_type=mime_type,
                document_id=document_id,
                source_url=url,
                local_path=path_value,
                content_hash=content_hash,
            )
        )
    return attachments, omissions


def _append_omission(omissions: list[dict[str, str]], item: Mapping[str, str]) -> bool:
    """Append one omission row unless the same source/reason is already shown."""

    identity = (item.get("label", ""), item.get("local_path", ""), item.get("reason", ""))
    if any((row.get("label", ""), row.get("local_path", ""), row.get("reason", "")) == identity for row in omissions):
        return False
    omissions.append(dict(item))
    return True


def _document_links_html(attachments: Sequence[_Attachment], omissions: Sequence[Mapping[str, str]], theme: Mapping[str, str] | None = None) -> str:
    # This fragment is appended after the report HTML in the multipart
    # alternative.  Keep its layout self-contained because some mail clients
    # strip the report stylesheet when they render the MIME alternative.
    section_style = (
        "box-sizing:border-box;width:100%;max-width:720px;margin:0 auto;"
        "padding:16px;background:#fcfbf8;border-top:1px solid #dce1e6;"
        "font-family:'Segoe UI',Arial,Helvetica,sans-serif;color:#171c22;"
        "font-size:13px;line-height:1.45;overflow-wrap:anywhere;word-break:break-word;"
    )
    heading_style = (
        "margin:0 0 6px;font-family:'Segoe UI',Arial,Helvetica,sans-serif;"
        "font-size:13px;line-height:1.35;font-weight:600;color:#171c22;"
    )
    list_style = "margin:0 0 12px;padding:0 0 0 19px;"
    item_style = "margin:0 0 5px;padding:0;overflow-wrap:anywhere;word-break:break-word;"
    link_style = (
        "color:#243f56;text-decoration:underline;overflow-wrap:anywhere;"
        "word-break:break-word;"
    )
    muted_style = "color:#606b75;font-size:12px;"
    theme = theme or page_theme(NEUTRAL_BRAND)
    section_style = section_style.replace('#fcfbf8', theme['paper_bg']).replace('#dce1e6', theme['rule_color']).replace('#171c22', theme['body_color'])
    heading_style = heading_style.replace('#171c22', theme['heading_color'])
    link_style = link_style.replace('#243f56', theme['link_color'])
    muted_style = muted_style.replace('#606b75', theme['secondary_color'])
    rows: list[str] = [f'<section class="document-links" aria-label="Original documents" style="{section_style}">']
    if attachments:
        rows.append(f'<h3 style="{heading_style}">Documents in this message</h3>')
        rows.append(f'<ul style="{list_style}">')
        for item in attachments:
            # Keep the useful attachment filename in the tooltip. Absolute
            # archive paths belong only in internal provenance records.
            document_title = str(_field(item.document, "title", "") or "").strip()
            display_label = document_title or item.filename
            label = html_lib.escape(display_label)
            provenance = f"Attached filename: {item.filename}"
            provenance_attr = html_lib.escape(provenance, quote=True)
            if item.source_url:
                link = (
                    f'<a href="{html_lib.escape(item.source_url, quote=True)}" '
                    f'title="{provenance_attr}" style="{link_style}">{label}</a>'
                )
            else:
                link = f'<span title="{provenance_attr}">{label}</span>'
            rows.append(
                f'<li style="{item_style}">{link} '
                f'<span style="{muted_style}">(original archived)</span></li>'
            )
        rows.append("</ul>")
    if omissions:
        rows.append(f'<h3 style="{heading_style}">Attachment omissions</h3>')
        rows.append(f'<ul style="{list_style}">')
        for item in omissions:
            label = html_lib.escape(item.get("label", "document"))
            url = item.get("url", "")
            link = (
                f'; <a href="{html_lib.escape(url, quote=True)}" '
                f'style="{link_style}">source link</a>'
                if url
                else ""
            )
            local = item.get("local_path", "")
            if local:
                local_note = (
                    f'; <span '
                    f'style="{muted_style}">archive retained locally</span>'
                )
            else:
                local_note = ""
            rows.append(
                f'<li style="{item_style}">{label}{link}; omitted: '
                f'{html_lib.escape(item.get("reason", "unavailable"))}'
                f"{local_note}</li>"
            )
        rows.append("</ul>")
    if not attachments and not omissions:
        rows.append(
            f'<p style="{muted_style};margin:0">'
            "No original archived documents were available for attachment.</p>"
        )
    rows.append("</section>")
    return "\n".join(rows)


def _document_links_text(attachments: Sequence[_Attachment], omissions: Sequence[Mapping[str, str]]) -> str:
    rows: list[str] = []
    if attachments:
        rows.append("Documents in this message:")
        rows.extend(
            f"- {str(_field(item.document, 'title', '') or item.filename)} (attached)"
            + (f"; source: {item.source_url}" if item.source_url else "")
            for item in attachments
        )
    if omissions:
        rows.append("Attachment omissions:")
        for item in omissions:
            row = f"- {item.get('label', 'document')}: {item.get('reason', 'unavailable')}"
            if item.get("url"):
                row += f"; source: {item['url']}"
            if item.get("local_path"):
                row += "; original copy retained locally"
            rows.append(row)
    if not rows:
        rows.append("No original archived documents were available for attachment.")
    return "\n".join(rows)


def _report_bodies(report: Mapping[str, Any]) -> tuple[str, str]:
    text_body = str(report.get("text", "") or "")
    html_body = str(report.get("html", "") or "")
    if not text_body and html_body:
        text_body = re.sub(r"<[^>]+>", " ", html_body)
        text_body = re.sub(r"\s+", " ", html_lib.unescape(text_body)).strip()
    if not html_body:
        html_body = f"<pre>{html_lib.escape(text_body)}</pre>"
    return text_body, html_body


def _attribute_value(match: re.Match[str]) -> str:
    value = match.group("quoted")
    if value is None:
        value = match.group("bare")
    return value or ""


def _tag_attributes(tag: str, name: str) -> list[re.Match[str]]:
    wanted = name.casefold()
    return [
        match
        for match in _HTML_ATTRIBUTE.finditer(tag)
        if match.group("name").casefold() == wanted
    ]


def _html_part_and_parent(message: EmailMessage) -> tuple[EmailMessage | None, EmailMessage | None]:
    """Find the text/html leaf and its immediate parent in ``message``."""

    if not getattr(message, "is_multipart", lambda: False)():
        return (message, None) if message.get_content_type() == "text/html" else (None, None)

    def descend(parent: EmailMessage) -> tuple[EmailMessage | None, EmailMessage | None]:
        for child in parent.iter_parts():
            if not child.is_multipart() and child.get_content_type() == "text/html":
                return child, parent
            if child.is_multipart():
                found, found_parent = descend(child)
                if found is not None:
                    return found, found_parent
        return None, None

    return descend(message)


def _brand_asset(ticker: str) -> _BrandAsset:
    """Resolve one registry record to a verified, local PNG asset."""

    try:
        record = lookup_brand(ticker)
    except Exception as exc:  # pragma: no cover - defensive registry boundary
        raise BrandingError(f"brand lookup failed for {ticker}: {exc}") from exc

    if _field(record, "verified", False) is not True:
        reason = str(_field(record, "gap_reason", "brand record is not verified") or "brand record is not verified")
        raise BrandingError(f"cannot embed {ticker}: {reason}")

    returned_ticker = str(_field(record, "ticker", ticker) or ticker).strip().upper()
    if returned_ticker and returned_ticker != ticker:
        raise BrandingError(f"brand lookup returned {returned_ticker} for {ticker}")

    public_url = _field(record, "public_logo_url", "")
    if not isinstance(public_url, str) or not public_url.strip():
        raise BrandingError(f"cannot embed {ticker}: verified record has no public_logo_url")
    public_url = public_url.strip()
    try:
        parsed_url = urlparse(public_url)
    except ValueError as exc:
        raise BrandingError(f"cannot embed {ticker}: malformed public_logo_url") from exc
    if (
        parsed_url.scheme.casefold() != "https"
        or not parsed_url.netloc
        or parsed_url.username is not None
        or parsed_url.password is not None
    ):
        raise BrandingError(f"cannot embed {ticker}: public_logo_url must be an HTTPS URL")

    logo_alt = _field(record, "logo_alt", "")
    if not isinstance(logo_alt, str) or not logo_alt.strip() or "<" in logo_alt or ">" in logo_alt:
        raise BrandingError(f"cannot embed {ticker}: verified record has no safe logo_alt")

    raw_path = _field(record, "local_asset_path", None)
    if isinstance(raw_path, Path):
        asset_path = raw_path
    elif isinstance(raw_path, str) and raw_path.strip():
        asset_path = Path(raw_path.strip())
    else:
        raise BrandingError(f"cannot embed {ticker}: verified record has no local_asset_path")
    if not asset_path.is_absolute():
        # The registry returns absolute paths.  Resolving a relative value only
        # against this package keeps HTML from ever selecting an arbitrary path.
        asset_path = Path(__file__).resolve().parent / asset_path
    try:
        asset_path = asset_path.resolve(strict=True)
        if asset_path.suffix.casefold() != ".png":
            raise BrandingError(f"cannot embed {ticker}: local asset is not PNG")
        data = asset_path.read_bytes()
    except BrandingError:
        raise
    except (OSError, RuntimeError) as exc:
        raise BrandingError(f"cannot embed {ticker}: local PNG cannot be read") from exc
    if not data.startswith(_PNG_SIGNATURE):
        raise BrandingError(f"cannot embed {ticker}: local asset is not a valid PNG")

    digest = hashlib.sha256(data).hexdigest()[:24]
    return _BrandAsset(
        ticker=ticker,
        cid=f"brand-{ticker.lower()}-{digest}@servicing-brief.local",
        data=data,
        public_logo_url=public_url,
        logo_alt=logo_alt.strip(),
    )


def _data_uri_bytes(value: str) -> bytes | None:
    match = _PNG_DATA_URI.fullmatch(value.strip())
    if match is None:
        return None
    encoded = re.sub(r"\s+", "", match.group("data"))
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None


def _existing_related_parts(container: EmailMessage | None) -> dict[str, tuple[str, bytes]]:
    if container is None or not container.is_multipart() or container.get_content_type() != "multipart/related":
        return {}
    existing: dict[str, tuple[str, bytes]] = {}
    for part in container.iter_parts():
        raw_cid = part.get("Content-ID")
        if not raw_cid:
            continue
        cid = raw_cid.strip().strip("<>")
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = b""
        current = (part.get_content_type(), payload)
        previous = existing.get(cid)
        if previous is not None and previous != current:
            raise BrandingError(f"related MIME contains conflicting Content-ID {cid}")
        existing[cid] = current
    return existing


def embed_brand_logos(message: EmailMessage, html_body: str) -> str:
    """Replace verified brand image sources and attach deterministic PNG CIDs.

    Only ``src`` values on ``img[data-brand-ticker]`` tags are changed.  The
    source must be the registry's HTTPS URL or a data URI for that registry's
    local PNG.  A missing or unverified registry record fails preparation so a
    preview cannot contain a broken or guessed issuer identity.
    """

    if not isinstance(html_body, str):
        raise BrandingError("HTML body must be text before branding is embedded")
    image_tags = list(_BRAND_IMG_TAG.finditer(html_body))
    branded_tags: list[tuple[re.Match[str], list[re.Match[str]], list[re.Match[str]]]] = []
    for tag_match in image_tags:
        tag = tag_match.group(0)
        ticker_attrs = _tag_attributes(tag, "data-brand-ticker")
        if not ticker_attrs:
            if _BRAND_ATTRIBUTE_MARKER.search(tag):
                raise BrandingError("branded img is missing a data-brand-ticker value")
            continue
        src_attrs = _tag_attributes(tag, "src")
        alt_attrs = _tag_attributes(tag, "alt")
        branded_tags.append((tag_match, ticker_attrs, src_attrs + alt_attrs))
        if len(ticker_attrs) != 1:
            raise BrandingError("branded img must have exactly one data-brand-ticker attribute")
        if len(src_attrs) != 1:
            raise BrandingError("branded img must have exactly one src attribute")
        if len(alt_attrs) != 1:
            raise BrandingError("branded img must have exactly one alt attribute")

    if not branded_tags:
        return html_body

    html_part, parent = _html_part_and_parent(message)
    if html_part is None:
        raise BrandingError("cannot embed brand logos without an HTML alternative")
    related_container = parent if parent is not None and parent.get_content_type() == "multipart/related" else None
    existing = _existing_related_parts(related_container)
    assets: dict[str, _BrandAsset] = {}
    replacements: list[tuple[int, int, str]] = []

    for tag_match, ticker_attrs, attrs in branded_tags:
        tag = tag_match.group(0)
        ticker_value = html_lib.unescape(_attribute_value(ticker_attrs[0])).strip().upper()
        if not _TICKER.fullmatch(ticker_value):
            raise BrandingError(f"invalid brand ticker {ticker_value!r}")
        asset = assets.get(ticker_value)
        if asset is None:
            asset = _brand_asset(ticker_value)
            assets[ticker_value] = asset

        src_attrs = [match for match in attrs if match.group("name").casefold() == "src"]
        alt_attrs = [match for match in attrs if match.group("name").casefold() == "alt"]
        src_attr = src_attrs[0]
        alt_attr = alt_attrs[0]
        source = html_lib.unescape(_attribute_value(src_attr)).strip()
        actual_alt = html_lib.unescape(_attribute_value(alt_attr)).strip()
        decorative_identity = False
        if actual_alt == '':
            from bs4 import BeautifulSoup
            image_node = BeautifulSoup(html_body, 'html.parser').find('img', attrs={'data-brand-ticker': ticker_value})
            masthead = image_node.find_parent('table', class_='issuer-masthead') if image_node else None
            identity_node = image_node.find_parent(attrs={'data-brief-company': ticker_value}) if image_node else None
            name_node = masthead.select_one('.issuer-name') if masthead else None
            decorative_identity = bool(
                image_node and image_node.get('aria-hidden') == 'true'
                and image_node.get('data-brand-logo-description') == asset.logo_alt
                and identity_node and name_node and name_node.get_text(strip=True)
                and not name_node.has_attr('hidden') and name_node.get('aria-hidden') != 'true'
                and not re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', str(name_node.get('style','')), re.I)
            )
        if actual_alt != asset.logo_alt and not decorative_identity:
            raise BrandingError(f"branded img alt does not match the verified {ticker_value} logo_alt")

        if source == f"cid:{asset.cid}":
            related = existing.get(asset.cid)
            if related != ("image/png", asset.data):
                raise BrandingError(f"branded img {ticker_value} references an unvalidated Content-ID")
            replacement = source
        else:
            if source != asset.public_logo_url and _data_uri_bytes(source) != asset.data:
                raise BrandingError(
                    f"branded img {ticker_value} src must match its verified public_logo_url or local PNG data URI"
                )
            replacement = f"cid:{asset.cid}"
        value_group = "quoted" if src_attr.group("quote") is not None else "bare"
        value_start, value_end = src_attr.span(value_group)
        replacements.append((tag_match.start() + value_start, tag_match.start() + value_end, replacement))

    rewritten = html_body
    for start, end, value in reversed(replacements):
        rewritten = rewritten[:start] + value + rewritten[end:]
    if rewritten != html_body:
        html_part.set_content(rewritten, subtype="html")

    container = related_container or html_part
    for asset in assets.values():
        current = existing.get(asset.cid)
        if current is not None:
            if current != ("image/png", asset.data):
                raise BrandingError(f"related MIME asset for {asset.ticker} does not match the verified PNG")
            continue
        container.add_related(
            asset.data,
            maintype="image",
            subtype="png",
            cid=f"<{asset.cid}>",
        )
        existing[asset.cid] = ("image/png", asset.data)

    seed = str(message.get(_KEY_HEADER, "") or "").strip()
    if not seed:
        seed = hashlib.sha256(
            (rewritten + "\x00" + "|".join(sorted(assets))).encode("utf-8")
        ).hexdigest()
    safe_seed = re.sub(r"[^A-Za-z0-9_.-]", "", seed)[:24]
    if not safe_seed:
        safe_seed = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()[:24]
    container.set_boundary(f"=_servicing_brief_related_{safe_seed}")
    return rewritten


def _message_bytes(message: EmailMessage) -> bytes:
    return message.as_bytes(policy=policy.SMTP)


def _message_size(message: EmailMessage) -> int:
    """Count the exact SMTP encoding without retaining another complete copy."""
    class Counter:
        size = 0
        def write(self, data: bytes) -> None:
            self.size += len(data)
    counter = Counter()
    BytesGenerator(counter, policy=policy.SMTP).flatten(message)
    return counter.size


def _email_compatible_html(html_body: str) -> str:
    """Keep styled section containers when Gmail sanitizes received mail.

    Gmail can preserve these containers in compose and remove them, including
    their inline colors, in the received message. Only the email copy changes;
    standalone reports retain their semantic section elements.
    """
    line_starts = [0] + [match.end() for match in re.finditer("\n", html_body)]
    replacements: list[tuple[int, int]] = []

    class SectionTags(HTMLParser):
        def replace_section(self, tag: str) -> None:
            if tag != "section":
                return
            line, column = self.getpos()
            start = line_starts[line - 1] + column
            match = re.match(r"</?\s*(section)\b", html_body[start:], re.IGNORECASE)
            if match:
                replacements.append((start + match.start(1), start + match.end(1)))

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.replace_section(tag)

        handle_startendtag = handle_starttag

        def handle_endtag(self, tag: str) -> None:
            self.replace_section(tag)

    parser = SectionTags(convert_charrefs=False)
    parser.feed(html_body)
    parser.close()
    # Preserve every other byte, including attributes, URLs, CSS and comments.
    for start, end in reversed(replacements):
        html_body = html_body[:start] + "div" + html_body[end:]
    return html_body


def _make_message(
    config: Mapping[str, Any],
    report: Mapping[str, Any],
    attachments: Sequence[_Attachment],
    omissions: Sequence[Mapping[str, str]],
    *,
    subject: str,
    package_key: str,
    message_key: str,
    part_number: int,
    total_parts: int,
) -> EmailMessage:
    settings = _settings(config)
    recipient_error = _recipient_error(config)
    if recipient_error:
        raise DeliveryError(recipient_error)
    sender_error = _sender_error(config)
    if sender_error:
        raise DeliveryError(sender_error)
    recipients = _recipients(config)
    sender = _from_address(config)
    text_body, html_body = _report_bodies(report)
    identity = None
    if has_company_identity(report):
        # Preparation validates source documents.  Recheck the actual report
        # surfaces here as well because this helper is also used by the MIME
        # size-packaging pass and is a public seam for callers/tests.
        identity = report_identity(report, strict=True)
        validate_report_surfaces(report, identity)
    part_note_text = (
        f"\n\nThis package is message {part_number} of {total_parts}.\n"
        if total_parts > 1
        else ""
    )
    part_note_html = (
        f"<p>This package is message {part_number} of {total_parts}.</p>"
        if total_parts > 1
        else ""
    )
    docs_text = _document_links_text(attachments, omissions)
    theme = page_theme(lookup_brand(report['company_identity']['ticker'])) if has_company_identity(report) else None
    docs_html = _document_links_html(attachments, omissions, theme)
    msg = EmailMessage(policy=policy.SMTP)
    msg["Subject"] = subject
    if sender:
        msg["From"] = sender
    if recipients:
        msg["To"] = ", ".join(recipients)
    generated = report.get("generated_at") or report.get("as_of")
    if isinstance(generated, str):
        try:
            date_value = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            if date_value.tzinfo is None:
                date_value = date_value.replace(tzinfo=timezone.utc)
        except ValueError:
            date_value = datetime.now(timezone.utc)
    else:
        date_value = datetime.now(timezone.utc)
    msg["Date"] = format_datetime(date_value.astimezone(timezone.utc), usegmt=True)
    msg["Message-ID"] = f"<{message_key}@servicing-brief.local>"
    msg[_KEY_HEADER] = message_key
    msg[_PACKAGE_HEADER] = package_key
    if identity is not None:
        msg[BOUNDARY_HEADER] = BOUNDARY_VERSION
        msg[IDENTITY_HEADER] = identity.token()
    elif report.get("kind") == "smtp_test":
        # ``send-test`` is deliberately nonfinancial and has no reporting
        # company.  Keep its exception explicit in the MIME artifact.
        msg[TEST_HEADER] = "smtp"
    msg["X-Servicing-Brief-Part"] = f"{part_number}/{total_parts}"
    msg["X-Servicing-Brief-Attachment-Count"] = str(len(attachments))
    if 'reader_value' in report:
        msg['X-Servicing-Reader-Value'] = report['reader_value'].get('status', 'blocked')
    msg.set_content(text_body + part_note_text + "\n\n" + docs_text)
    supplement = part_note_html + docs_html
    # Keep the MIME alternative a valid document when the report already
    # supplies an HTML body. Fragments used by diagnostic mail still work.
    body_close = re.search(r"</body\s*>", html_body, re.IGNORECASE)
    full_html = (html_body[:body_close.start()] + supplement + html_body[body_close.start():]
                 if body_close else html_body + supplement)
    full_html = _email_compatible_html(full_html)
    full_html = apply_page_theme(full_html, theme or page_theme(NEUTRAL_BRAND))
    msg.add_alternative(full_html, subtype="html")
    embed_brand_logos(msg, full_html)
    for item in attachments:
        maintype, subtype = item.mime_type.split("/", 1) if "/" in item.mime_type else ("application", "octet-stream")
        msg.add_attachment(item.data, maintype=maintype, subtype=subtype, filename=item.filename)
    # Python's email package otherwise chooses random MIME boundaries.  A
    # deterministic boundary and Message-ID make previews reproducible and
    # keep provider-side deduplication meaningful across restarts.
    if msg.is_multipart():
        try:
            msg.set_boundary(f"=_servicing_brief_{message_key[:24]}")
            payload = msg.get_payload()
            if isinstance(payload, list) and payload and getattr(payload[0], "is_multipart", lambda: False)():
                payload[0].set_boundary(f"=_servicing_brief_alt_{message_key[24:48] or message_key[:24]}")
        except (ValueError, AttributeError):
            # A future email-policy implementation may reject explicit
            # boundaries; message-key idempotency remains sufficient then.
            pass
    assert_reader_mime(msg)
    return msg


def _package_key(
    config: Mapping[str, Any],
    report: Mapping[str, Any],
    attachments: Sequence[_Attachment],
    omissions: Sequence[Mapping[str, str]] = (),
) -> str:
    payload = {
        "subject": str(report.get("subject", "") or ""),
        "html": str(report.get("html", "") or ""),
        "text": str(report.get("text", "") or ""),
        "reader_value": report.get('reader_value', {}),
        # Recipient changes must create a new idempotency key.  Never include
        # SMTP credentials in this digest.
        "recipient": _recipients(config),
        "sender": _from_address(config),
        "documents": [
            {
                "id": item.document_id,
                "hash": item.content_hash,
                "url": item.source_url,
            }
            for item in attachments
        ],
        "omissions": [
            {
                "label": str(item.get("label", "")),
                "url": str(item.get("url", "")),
                "local_path": str(item.get("local_path", "")),
                "reason": str(item.get("reason", "")),
            }
            for item in omissions
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _part_message_key(
    package_key: str,
    group: Sequence[_Attachment],
    *,
    disambiguate_document_ids: bool = False,
) -> str:
    """Return a stable key for one MIME part.

    Document IDs normally identify a source copy.  Include the content hash
    when a malformed or legacy source set reuses an ID for different bytes so
    two distinct attachment groups cannot share one persisted delivery row.
    """

    tokens = []
    for item in group:
        token = item.document_id
        if disambiguate_document_ids:
            token += ":" + item.content_hash
        tokens.append(token)
    return hashlib.sha256((package_key + "|" + "|".join(tokens)).encode()).hexdigest()[:32]


def _pack_attachments(
    config: Mapping[str, Any],
    report: Mapping[str, Any],
    attachments: Sequence[_Attachment],
    omissions: list[dict[str, str]],
    package_key: str,
) -> list[list[_Attachment]]:
    """Greedily pack attachments using the fully encoded MIME size.

    The temporary 9999/9999 subject marker is longer than any practical part
    count and therefore makes final numbering unable to push a message above
    the configured limit.
    """

    limit = _max_message_bytes(config)
    groups: list[list[_Attachment]] = []
    current: list[_Attachment] = []
    base_subject = str(report.get("subject", "Mortgage Servicing Brief") or "Mortgage Servicing Brief")
    disambiguate_document_ids = len({item.document_id for item in attachments}) != len(attachments)

    def candidate(group: Sequence[_Attachment]) -> EmailMessage:
        marker_subject = f"{base_subject} (part 9999 of 9999)" if group or groups else base_subject
        key = _part_message_key(
            package_key,
            group,
            disambiguate_document_ids=disambiguate_document_ids,
        )
        return _make_message(
            config,
            report,
            group,
            omissions,
            subject=marker_subject,
            package_key=package_key,
            message_key=key,
            part_number=9999,
            total_parts=9999,
        )

    # Most earnings packets fit one message. Measure that packet once instead
    # of repeatedly encoding all of its growing prefixes. The base64 lower
    # bound avoids this extra attempt for packets that clearly need splitting.
    if len(attachments) > 1 and sum(4 * ((len(item.data) + 2) // 3) for item in attachments) < limit:
        if _message_size(candidate(attachments)) <= limit:
            return [list(attachments)]

    for item in attachments:
        proposed = current + [item]
        if _message_size(candidate(proposed)) <= limit:
            current = proposed
            continue
        if current:
            groups.append(current)
            current = []
        single = candidate([item])
        if _message_size(single) <= limit:
            current = [item]
        else:
            _append_omission(
                omissions,
                {
                    "label": f"{_field(item.document, 'issuer', 'issuer')} {_field(item.document, 'period', 'unknown')} {_field(item.document, 'kind', 'document')}",
                    "url": item.source_url,
                    "local_path": item.local_path,
                    "reason": "attachment exceeds configured fully-encoded message limit",
                },
            )
    if current:
        groups.append(current)
    if not groups:
        groups = [[]]
    return groups


def prepare_messages(
    config: dict,
    report: dict,
    documents: list[Document],
    output_dir: Path,
) -> list[Path]:
    """Render one or more fully encoded ``.eml`` previews.

    The originals at ``Document.path`` are attached byte-for-byte.  The
    function never sends and does not write to the delivery state database.
    Missing or oversized documents remain visible as source links and local
    archive paths in every split message.
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    documents = list(documents)
    primary_documents = list(documents)
    context_documents: list[Document] = []
    ciks = {d.cik.lstrip("0") for d in documents}
    for raw in report.get("context_documents", []):
        context = Document(**raw)
        if context.cik.lstrip("0") not in ciks or context.kind.upper() not in {"10-K", "10-K/A"}:
            raise ValueError("Annual context attachments must belong to this reporting company")
        if not any(d.content_hash == context.content_hash for d in documents):
            context_documents.append(context)
            documents.append(context)
    if has_company_identity(report):
        # Every normal publication requires explicit identity. Historical
        # archives remain readable, but cannot bypass this prepare/send gate.
        require_company_boundary(
            report,
            primary_documents,
            context_documents=context_documents,
            require_surfaces=True,
        )
        if 'finding-band' in str(report.get('html', '')):
            validate_page_theme(str(report['html']), report_identity(report).ticker)
    elif report.get("kind") != "smtp_test":
        raise CompanyBoundaryError("normal brief requires company_identity metadata")
    attachments, omissions = _load_attachments(documents)
    package_key = _package_key(config, report, attachments, omissions)
    # A second pass accounts for omission text discovered while packing.  In
    # practice this only matters for messages configured very near the limit.
    for _ in range(len(attachments) + 2):
        before = len(omissions)
        groups = _pack_attachments(config, report, attachments, omissions, package_key)
        if len(omissions) == before:
            break
    package_key = _package_key(config, report, attachments, omissions)
    total_parts = len(groups)
    subject = str(report.get("subject", "Mortgage Servicing Brief") or "Mortgage Servicing Brief")
    disambiguate_document_ids = len({item.document_id for item in attachments}) != len(attachments)
    paths: list[Path] = []
    for index, group in enumerate(groups, start=1):
        part_key = _part_message_key(
            package_key,
            group,
            disambiguate_document_ids=disambiguate_document_ids,
        )
        part_subject = f"{subject} (part {index} of {total_parts})" if total_parts > 1 else subject
        message = _make_message(
            config,
            report,
            group,
            omissions,
            subject=part_subject,
            package_key=package_key,
            message_key=part_key,
            part_number=index,
            total_parts=total_parts,
        )
        encoded = _message_bytes(message)
        # A very large report body can itself exceed the budget.  Keep the
        # preview useful and explicit rather than silently dropping content.
        if len(encoded) > _max_message_bytes(config):
            message["X-Servicing-Brief-Size-Warning"] = (
                f"fully encoded message is {len(encoded)} bytes; limit is {_max_message_bytes(config)} bytes"
            )
            encoded = _message_bytes(message)
        filename = f"servicing-brief-{package_key[:12]}-part-{index:02d}-of-{total_parts:02d}.eml"
        path = output_dir / filename
        path.write_bytes(encoded)
        paths.append(path)
    return paths


def _ensure_delivery_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS delivery_messages (
            message_key TEXT PRIMARY KEY,
            package_key TEXT NOT NULL DEFAULT '',
            path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            part_number INTEGER NOT NULL DEFAULT 1,
            total_parts INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            provider_response TEXT NOT NULL DEFAULT '',
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            accepted_at TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_messages_status
            ON delivery_messages(status);
        CREATE TABLE IF NOT EXISTS delivery_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_key TEXT NOT NULL,
            attempt INTEGER NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    connection.commit()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_metadata(path: Path) -> tuple[str, str, str, int, int, bytes, Any]:
    raw = path.read_bytes()
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    message_key = str(parsed.get(_KEY_HEADER, "") or "").strip()
    if not message_key:
        message_key = hashlib.sha256(raw).hexdigest()
    package_key = str(parsed.get(_PACKAGE_HEADER, "") or "").strip()
    subject = str(parsed.get("Subject", "") or "")
    part_number, total_parts = 1, 1
    marker = str(parsed.get("X-Servicing-Brief-Part", "") or "")
    match = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", marker)
    if match:
        part_number, total_parts = int(match.group(1)), int(match.group(2))
    return message_key, package_key, subject, part_number, total_parts, raw, parsed


def _validate_mime_boundary(parsed: Any, *, allow_smtp_test: bool = False) -> str | None:
    """Return a safe hold reason for a strict identity-bearing MIME message."""

    identity_values = parsed.get_all(IDENTITY_HEADER, []) if hasattr(parsed, "get_all") else []
    boundary_values = parsed.get_all(BOUNDARY_HEADER, []) if hasattr(parsed, "get_all") else []
    if not identity_values and not boundary_values:
        if allow_smtp_test and parsed.get(TEST_HEADER, "") == "smtp":
            return None
        return "company-boundary MIME identity is missing"
    if boundary_values != [BOUNDARY_VERSION]:
        return "company-boundary MIME header is missing or invalid"
    try:
        boundary = validate_mime_message(parsed)
        html = parsed.get_body(preferencelist=('html',)).get_content()
        if 'finding-band' in html:
            validate_page_theme(html, boundary['identity']['ticker'])
    except (CompanyBoundaryError, BrandRegistryError) as exc:
        return f"company-boundary MIME validation failed: {exc}"
    try:
        assert_reader_mime(parsed)
    except ValueError as exc:
        return f"P0 reader-display MIME validation failed: {exc}"
    return None


def _record_attempt(
    connection: sqlite3.Connection,
    message_key: str,
    attempt: int,
    outcome: str,
    detail: str,
) -> None:
    connection.execute(
        "INSERT INTO delivery_attempts(message_key, attempt, outcome, detail, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_key, attempt, outcome, detail[:2000], _now_iso()),
    )


def _configuration_result(config: Mapping[str, Any]) -> tuple[str | None, str]:
    if not _send_enabled(config):
        return "sending_disabled", "sending is disabled; use an explicit send command/runtime gate"
    recipient_error = _recipient_error(config)
    if recipient_error:
        return "invalid_recipient", recipient_error
    recipients = _recipients(config)
    if not recipients:
        return "missing_recipient", "no explicit recipient is configured"
    sender = _from_address(config)
    if not sender:
        return "missing_sender", "email.from_address is not configured"
    sender_error = _sender_error(config)
    if sender_error:
        return "invalid_sender", sender_error
    security_error = _smtp_security_error(config)
    if security_error:
        return "invalid_smtp_security", security_error
    credentials = _smtp_credentials(config)
    missing: list[str] = []
    if not credentials["host"]:
        missing.append("email.smtp_host")
    if not credentials["username"]:
        missing.append("email.smtp_username")
    if not credentials["password"]:
        missing.append(f"environment variable {credentials['password_env']}")
    if missing:
        return "missing_credentials", "SMTP setup incomplete: " + ", ".join(missing)
    return None, ""


def _lock_path(state_db: Path) -> Path:
    return Path(str(state_db) + ".delivery.lock")


def _open_smtp(credentials: Mapping[str, Any]):
    security = str(credentials.get("security", "starttls")).lower()
    timeout = float(credentials.get("timeout", 30))
    if security in {"ssl", "smtps", "implicit_tls", "implicit-tls"}:
        return smtplib.SMTP_SSL(
            str(credentials["host"]),
            int(credentials["port"]),
            timeout=timeout,
            context=ssl.create_default_context(),
        )
    if security == "starttls":
        return smtplib.SMTP(str(credentials["host"]), int(credentials["port"]), timeout=timeout)
    raise ValueError("unsupported SMTP security mode")


def _smtp_send_one(config: Mapping[str, Any], parsed: Any, recipients: list[str]) -> tuple[str, str]:
    """Send one message and return (outcome, detail).

    A disconnect or timeout while ``send_message`` is in progress is
    inherently ambiguous: the provider may have accepted the message before
    the client observed the failure.  Such a result is persisted as ambiguous
    and is never automatically retried.
    """

    credentials = _smtp_credentials(config)
    smtp = None
    send_started = False
    try:
        smtp = _open_smtp(credentials)
        smtp.ehlo()
        security = str(credentials.get("security", "starttls")).lower()
        if security not in {"ssl", "smtps", "implicit_tls", "implicit-tls"}:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        smtp.login(str(credentials["username"]), str(credentials["password"]))
        send_started = True
        refused = smtp.send_message(
            parsed,
            from_addr=_from_address(config),
            to_addrs=recipients,
        )
        if refused:
            return "failed", f"provider refused recipients: {', '.join(sorted(str(k) for k in refused))}"
        return "accepted", "provider accepted the SMTP message"
    except smtplib.SMTPAuthenticationError:
        return "failed", "SMTP authentication failed"
    except smtplib.SMTPConnectError as exc:
        if send_started:
            return "ambiguous", f"SMTP connection failed after send began ({exc.__class__.__name__}); review provider before retry"
        return "failed", f"SMTP connection failed before send ({exc.__class__.__name__})"
    except smtplib.SMTPRecipientsRefused as exc:
        return "failed", f"provider refused recipients ({len(getattr(exc, 'recipients', {}))})"
    except smtplib.SMTPDataError as exc:
        return "failed", f"provider rejected message data ({getattr(exc, 'smtp_code', '')})"
    except (smtplib.SMTPServerDisconnected, TimeoutError, OSError) as exc:
        if send_started:
            return "ambiguous", f"connection failed after send began ({exc.__class__.__name__}); review provider before retry"
        return "failed", f"SMTP connection failed before send ({exc.__class__.__name__})"
    except smtplib.SMTPException as exc:
        if send_started:
            return "ambiguous", f"SMTP error after send began ({exc.__class__.__name__}); review provider before retry"
        return "failed", f"SMTP setup error ({exc.__class__.__name__})"
    except Exception as exc:  # pragma: no cover - defensive transport boundary
        if send_started:
            return "ambiguous", f"unexpected error after send began ({exc.__class__.__name__}); review provider before retry"
        return "failed", f"unexpected SMTP setup error ({exc.__class__.__name__})"
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                # ``send_message`` already returned acceptance before this
                # point; a QUIT failure cannot change provider acceptance.
                pass


def deliver_messages(
    config: dict,
    message_paths: list[Path],
    state_db: Path,
    *,
    _allow_smtp_test: bool = False,
) -> list[dict]:
    """Deliver prepared messages with persistent, ambiguity-aware state.

    Every returned row contains a stable ``message_key`` and a status.  The
    function returns local configuration statuses instead of attempting a send
    when credentials or the explicit runtime gate are absent.
    """

    if not message_paths:
        return []
    state_db = Path(state_db)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    config_status, config_detail = _configuration_result(config)
    if config_status:
        rows: list[dict] = []
        for path_value in message_paths:
            path = Path(path_value)
            row_status, row_detail = config_status, config_detail
            try:
                key, package, subject, part, total, raw, parsed = _message_metadata(path)
                digest = hashlib.sha256(raw).hexdigest()
                boundary_error = _validate_mime_boundary(parsed, allow_smtp_test=_allow_smtp_test)
                if boundary_error:
                    row_status = 'company_boundary_hold'
                    row_detail = boundary_error
                elif parsed.get('X-Servicing-Reader-Value', '') == 'blocked':
                    row_status = 'editorial_hold'
                    row_detail = 'P0 reader-value review has not approved this draft; no send attempted.'
            except OSError as exc:
                key = hashlib.sha256(str(path).encode()).hexdigest()
                package = subject = ""
                part = total = 1
                digest = ""
                row_detail = f"{config_detail}; preview unavailable ({exc.__class__.__name__})"
            rows.append(
                {
                    "message_key": key,
                    "package_key": package,
                    "path": str(path),
                    "subject": subject,
                    "part_number": part,
                    "total_parts": total,
                    "status": row_status,
                    "detail": row_detail,
                    "content_hash": digest,
                }
            )
        return rows

    lock = None
    if FileLock is not None:
        lock = FileLock(str(_lock_path(state_db)))
        try:
            lock.acquire(timeout=_lock_timeout(config))
        except FileLockTimeout:
            return [
                {
                    "message_key": "",
                    "path": str(path),
                    "status": "overlap",
                    "detail": "another delivery process currently owns the delivery lock",
                }
                for path in message_paths
            ]
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_delivery_schema(connection)
        credentials = _smtp_credentials(config)
        recipients = _recipients(config)
        results: list[dict] = []
        max_attempts = _retry_attempts(config)
        for path_index, path_value in enumerate(message_paths):
            path = Path(path_value)
            try:
                key, package, subject, part, total, raw, parsed = _message_metadata(path)
            except (OSError, ValueError) as exc:
                results.append({"message_key": "", "path": str(path), "status": "invalid_message", "detail": str(exc)})
                continue
            digest = hashlib.sha256(raw).hexdigest()
            boundary_error = _validate_mime_boundary(parsed, allow_smtp_test=_allow_smtp_test)
            if boundary_error:
                results.append({'message_key': key, 'path': str(path), 'status': 'company_boundary_hold',
                                'detail': boundary_error})
                continue
            if parsed.get('X-Servicing-Reader-Value', '') == 'blocked':
                results.append({'message_key': key, 'path': str(path), 'status': 'editorial_hold',
                                'detail': 'P0 reader-value review has not approved this draft; no send attempted.'})
                continue
            row = connection.execute(
                "SELECT status, attempts, last_error, provider_response, content_hash FROM delivery_messages WHERE message_key = ?",
                (key,),
            ).fetchone()
            if row and str(row[4] or "") != digest:
                # A persisted key is also bound to the exact MIME bytes.  A
                # preview can be edited or replaced while an earlier attempt
                # is ambiguous; never send those new bytes under the old key
                # after an operator enables a controlled retry.
                state_name = str(row[0] or "delivery")
                status = (
                    "message_changed_after_acceptance"
                    if state_name == "accepted"
                    else "message_changed_after_delivery_state"
                )
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": status,
                        "detail": (
                            f"stored message key is in {state_name} state for different bytes; "
                            "provider send not attempted"
                        ),
                    }
                )
                continue
            if row and row[0] == "accepted":
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": "already_accepted",
                        "detail": "provider acceptance already recorded; no resend attempted",
                    }
                )
                continue
            if row and row[0] in {"ambiguous", "pending"}:
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": "ambiguous",
                        "detail": row[2] or "prior attempt may have been accepted; review provider before retry",
                    }
                )
                continue
            if row and row[0] == "oversize":
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": "oversize_refused",
                        "detail": row[2] or "fully encoded message exceeds configured limit",
                    }
                )
                continue
            if len(raw) > _max_message_bytes(config):
                detail = (
                    f"fully encoded message is {len(raw)} bytes; configured limit is "
                    f"{_max_message_bytes(config)} bytes; provider send not attempted"
                )
                timestamp = _now_iso()
                connection.execute(
                    """
                    INSERT INTO delivery_messages(
                        message_key, package_key, path, content_hash, subject,
                        part_number, total_parts, status, attempts,
                        provider_response, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'oversize', 0, '', ?, ?, ?)
                    ON CONFLICT(message_key) DO UPDATE SET
                        package_key=excluded.package_key,
                        path=excluded.path,
                        content_hash=excluded.content_hash,
                        subject=excluded.subject,
                        part_number=excluded.part_number,
                        total_parts=excluded.total_parts,
                        status='oversize',
                        last_error=excluded.last_error,
                        updated_at=excluded.updated_at
                    """,
                    (key, package, str(path), digest, subject, part, total, detail, timestamp, timestamp),
                )
                _record_attempt(connection, key, 0, "oversize_refused", detail)
                connection.commit()
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": "oversize_refused",
                        "detail": detail,
                    }
                )
                continue
            attempts = int(row[1]) if row else 0
            if attempts >= max_attempts:
                results.append(
                    {
                        "message_key": key,
                        "package_key": package,
                        "path": str(path),
                        "subject": subject,
                        "part_number": part,
                        "total_parts": total,
                        "status": "retry_exhausted",
                        "detail": row[2] if row else "retry limit reached",
                    }
                )
                continue
            timestamp = _now_iso()
            connection.execute(
                """
                INSERT INTO delivery_messages(
                    message_key, package_key, path, content_hash, subject,
                    part_number, total_parts, status, attempts,
                    provider_response, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, '', '', ?, ?)
                ON CONFLICT(message_key) DO UPDATE SET
                    package_key=excluded.package_key,
                    path=excluded.path,
                    content_hash=excluded.content_hash,
                    subject=excluded.subject,
                    part_number=excluded.part_number,
                    total_parts=excluded.total_parts,
                    status='pending',
                    attempts=excluded.attempts,
                    updated_at=excluded.updated_at
                """,
                (key, package, str(path), digest, subject, part, total, attempts + 1, timestamp, timestamp),
            )
            connection.commit()
            attempt_number = attempts + 1
            outcome, detail = "failed", "SMTP send did not run"
            while attempt_number <= max_attempts:
                outcome, detail = _smtp_send_one(config, parsed, recipients)
                _record_attempt(connection, key, attempt_number, outcome, detail)
                if outcome != "failed" or "before send" not in detail:
                    break
                if attempt_number >= max_attempts:
                    break
                # A new connection is created on each bounded pre-DATA retry.
                # Persist the attempt count before trying again so an
                # interrupted process cannot reset the retry budget.
                attempt_number += 1
                connection.execute(
                    "UPDATE delivery_messages SET attempts=?, updated_at=? WHERE message_key=?",
                    (attempt_number, _now_iso(), key),
                )
                connection.commit()
            now = _now_iso()
            if outcome == "accepted":
                connection.execute(
                    "UPDATE delivery_messages SET status='accepted', provider_response=?, last_error='', updated_at=?, accepted_at=? WHERE message_key=?",
                    (detail, now, now, key),
                )
            elif outcome == "ambiguous":
                connection.execute(
                    "UPDATE delivery_messages SET status='ambiguous', last_error=?, updated_at=? WHERE message_key=?",
                    (detail, now, key),
                )
            else:
                connection.execute(
                    "UPDATE delivery_messages SET status='failed', last_error=?, updated_at=? WHERE message_key=?",
                    (detail, now, key),
                )
            connection.commit()
            results.append(
                {
                    "message_key": key,
                    "package_key": package,
                    "path": str(path),
                    "subject": subject,
                    "part_number": part,
                    "total_parts": total,
                    "status": outcome,
                    "detail": detail,
                    "attempt": attempt_number,
                }
            )
            if outcome == "ambiguous":
                # Do not continue through a package after an uncertain SMTP
                # outcome: the connection/provider state is unknown.
                for pending_path in message_paths[path_index + 1 :]:
                    results.append(
                        {
                            "message_key": "",
                            "path": str(pending_path),
                            "status": "not_attempted_after_ambiguous",
                            "detail": "earlier message outcome was ambiguous; inspect provider before continuing",
                        }
                    )
                break
        return results
    finally:
        connection.close()
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def send_test(config: dict, state_db: Path) -> list[dict]:
    """Prepare and (when explicitly enabled) deliver a small test message."""

    storage = config.get("_storage") if isinstance(config, Mapping) else None
    output_dir = Path(storage) / "previews" if storage else Path.cwd() / "previews"
    report = {
        "kind": "smtp_test",
        "subject": "Mortgage Servicing Brief | SMTP test",
        "text": "SMTP test message from Mortgage Servicing Brief.\nNo financial content is included.",
        "html": "<p>SMTP test message from <strong>Mortgage Servicing Brief</strong>.</p><p>No financial content is included.</p>",
        "generated_at": _now_iso(),
    }
    paths = prepare_messages(config, report, [], output_dir)
    return deliver_messages(config, paths, Path(state_db), _allow_smtp_test=True)


def doctor_email(config: dict) -> list[dict]:
    """Return safe email configuration checks without exposing credentials."""

    settings = _settings(config)
    credentials = _smtp_credentials(config)
    recipient_error = _recipient_error(config)
    recipient_values = _recipients(config)
    configured_password = bool(credentials.get("password"))
    return [
        {
            "check": "recipient",
            "configured": bool(recipient_values) and not recipient_error,
            "detail": recipient_error or ("one explicit recipient configured" if recipient_values else "email.recipient is unset"),
        },
        {
            "check": "sender",
            "configured": bool(_from_address(config)) and not _sender_error(config),
            "detail": _sender_error(config) or ("email.sender configured" if _from_address(config) else "email.sender is unset"),
        },
        {
            "check": "smtp_host",
            "configured": bool(credentials["host"]),
            "detail": credentials["host"] or "smtp.gmail.com default is unavailable",
        },
        {
            "check": "smtp_username",
            "configured": bool(credentials["username"]),
            "detail": f"value supplied via {credentials['username_env']} or email.smtp_username" if credentials["username"] else f"set {credentials['username_env']} (the Gmail account address)",
        },
        {
            "check": "gmail_app_password",
            "configured": configured_password,
            "detail": f"value supplied via {credentials['password_env']}" if configured_password else f"set {credentials['password_env']}; never use a normal Gmail password",
        },
        {
            "check": "security",
            "configured": not _smtp_security_error(config),
            "detail": _smtp_security_error(config) or f"authenticated SMTP over {credentials['security']}",
        },
        {
            "check": "send_gate",
            "configured": _send_enabled(config),
            "detail": "explicit runtime send gate is on" if _send_enabled(config) else "sending remains disabled until an explicit send command",
        },
    ]


def delivery_status(state_db: Path) -> dict:
    """Return aggregate delivery state without reading message bodies."""

    state_db = Path(state_db)
    if not state_db.exists():
        return {"counts": {}, "recent": []}
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_delivery_schema(connection)
        counts = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT status, COUNT(*) FROM delivery_messages GROUP BY status ORDER BY status"
            )
        }
        recent = [
            {
                "message_key": row[0],
                "subject": row[1],
                "status": row[2],
                "attempts": row[3],
                "updated_at": row[4],
                "last_error": row[5],
            }
            for row in connection.execute(
                "SELECT message_key, subject, status, attempts, updated_at, last_error FROM delivery_messages ORDER BY updated_at DESC LIMIT 20"
            )
        ]
        return {"counts": counts, "recent": recent}
    finally:
        connection.close()


def reconcile_message(
    state_db: Path,
    message_key: str,
    *,
    decision: str,
    detail: str = "",
) -> dict:
    """Apply an explicit human decision to an ambiguous delivery.

    ``decision='accepted'`` records provider acceptance without another SMTP
    call.  ``decision='retry'`` clears the bounded attempt counter only after
    the operator has checked the provider and confirmed that no message was
    accepted.  This is the sole supported path out of an ambiguous state.
    """

    decision = str(decision or "").strip().lower()
    if decision not in {"accepted", "retry"}:
        raise ValueError("decision must be 'accepted' or 'retry'")
    message_key = str(message_key or "").strip()
    if not message_key:
        raise ValueError("message_key is required")
    state_db = Path(state_db)
    if not state_db.exists():
        raise DeliveryError("delivery state database does not exist")
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_delivery_schema(connection)
        row = connection.execute(
            "SELECT status, path, subject, attempts FROM delivery_messages WHERE message_key = ?",
            (message_key,),
        ).fetchone()
        if not row:
            raise DeliveryError(f"message key not found: {message_key}")
        if row[0] == "accepted" and decision == "retry":
            raise DeliveryError("an accepted message cannot be reset for retry")
        now = _now_iso()
        if decision == "accepted":
            connection.execute(
                "UPDATE delivery_messages SET status='accepted', provider_response=?, last_error='', accepted_at=?, updated_at=? WHERE message_key=?",
                ("operator reconciled provider acceptance: " + str(detail)[:1800], now, now, message_key),
            )
        else:
            connection.execute(
                "UPDATE delivery_messages SET status='failed', attempts=0, provider_response='', last_error=?, updated_at=? WHERE message_key=?",
                ("operator confirmed no provider acceptance; controlled retry enabled: " + str(detail)[:1600], now, message_key),
            )
        _record_attempt(connection, message_key, int(row[3]), "reconciled_" + decision, str(detail))
        connection.commit()
        return {
            "message_key": message_key,
            "path": row[1],
            "subject": row[2],
            "previous_status": row[0],
            "status": "accepted" if decision == "accepted" else "retry_ready",
            "detail": str(detail),
        }
    finally:
        connection.close()


__all__ = [
    "DeliveryError",
    "DeliveryOverlap",
    "prepare_messages",
    "deliver_messages",
    "send_test",
    "doctor_email",
    "delivery_status",
    "reconcile_message",
]
