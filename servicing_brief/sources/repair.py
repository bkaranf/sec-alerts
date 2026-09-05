"""Offline metadata repair manifests for already archived source records.

The repair helpers intentionally do not open a network connection or mutate
State.  They read the State snapshot and its local archive, apply the same
period/kind rules used by the production IR collector, and emit a reviewable
record that a caller can replay into State after inspection.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

from .common import infer_content_period, infer_period, title_kind


_NAMED_QUARTER = re.compile(
    r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s*(?:of\s*)?(20\d{2})\b",
    re.IGNORECASE,
)
_SHORT_QUARTER = re.compile(
    r"(?<![A-Za-z0-9])([1-4])\s*[Qq]\s*(20\d{2}|\d{2})(?![A-Za-z0-9])|"
    r"(?<![A-Za-z0-9])[Qq]([1-4])\s*(20\d{2}|\d{2})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_QUARTER_END = re.compile(
    r"\b(?:for\s+)?(?:the\s+)?(?:quarter|three\s+months?)\s+(?:ended|ending)\s+"
    r"(?:on\s+)?(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+[0-3]?\d(?:st|nd|rd|th)?(?:,|\s+)\s*(20\d{2})\b",
    re.IGNORECASE,
)
_FORM_QUARTER_END = re.compile(
    r"\bquarterly\s+period\s+ended\s*:?\s*(january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+[0-3]?\d(?:st|nd|rd|th)?(?:,|\s+)\s*(20\d{2})\b",
    re.IGNORECASE,
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
_URL_PERIOD_TOKEN = re.compile(
    r"(?i)(?:20\d{2}[-_/ ]?q[1-4]|q[1-4][-_/ ]?20\d{2}|[1-4]q(?:20)?\d{2}|fy[-_ ]?20\d{2})"
)


def _safe_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


def _normalise_visible(value: str, limit: int = 1200) -> str:
    return " ".join(str(value or "").split())[:limit]


def _read_opening(path: Path) -> tuple[str, str]:
    """Return ``(format, visible opening text)`` from a local archive copy."""

    if not path.exists() or not path.is_file():
        return "missing", ""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF, the project's sole PDF reader.

            with fitz.open(path) as document:
                if len(document):
                    return "pdf_page_1", _normalise_visible(document[0].get_text("text"))
        except Exception as exc:  # pragma: no cover - depends on damaged files
            return "pdf_error", f"PDF opening could not be read: {type(exc).__name__}"
        return "pdf_page_1", ""
    raw = path.read_bytes()[:200_000]
    if suffix in {".html", ".htm"}:
        try:
            from bs4 import BeautifulSoup

            visible = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
        except Exception:
            visible = raw.decode("utf-8", errors="replace")
        return "html_opening", _normalise_visible(visible)
    return "bytes_opening", _normalise_visible(raw.decode("utf-8", errors="replace"))


def _period_sort_key(period: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2})-Q([1-4])", period or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"(20\d{2})-FY", period or "")
    if match:
        return int(match.group(1)), 4
    return (0, 0)


def _cover_period_support(text: str) -> tuple[str, str]:
    """Choose the latest explicit quarter in a PDF's opening page.

    Earnings decks frequently show a prior conference-call title before the
    current title, while releases show current and comparison columns.  The
    latest explicit quarter in page one is therefore a useful cover check;
    this result is evidence only and does not replace the production URL/title
    period decision.
    """

    candidates: list[tuple[tuple[int, int], str, str]] = []
    ordinal = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}
    for match in _NAMED_QUARTER.finditer(text or ""):
        quarter = ordinal[match.group(1).lower()]
        label = f"{int(match.group(2))}-Q{quarter}"
        candidates.append((_period_sort_key(label), label, match.group(0)))
    for match in _SHORT_QUARTER.finditer(text or ""):
        quarter = match.group(1) or match.group(3)
        year_text = match.group(2) or match.group(4)
        year = int(year_text)
        if year < 100:
            year += 2000
        label = f"{year}-Q{quarter}"
        candidates.append((_period_sort_key(label), label, match.group(0)))
    for match in _QUARTER_END.finditer(text or ""):
        year = int(match.group(2))
        month = _MONTHS[match.group(1).lower()]
        label = f"{year}-Q{((month - 1) // 3) + 1}"
        candidates.append((_period_sort_key(label), label, match.group(0)))
    for match in _FORM_QUARTER_END.finditer(text or ""):
        year = int(match.group(2))
        month = _MONTHS[match.group(1).lower()]
        label = f"{year}-Q{((month - 1) // 3) + 1}"
        candidates.append((_period_sort_key(label), label, match.group(0)))
    if not candidates:
        return "unknown", ""
    _key, label, support = max(candidates, key=lambda item: item[0])
    return label, support


def _url_support(url: str) -> str:
    matches = list(_URL_PERIOD_TOKEN.finditer(unquote(url or "")))
    if not matches:
        return ""
    return matches[0].group(0)


def _is_navigation_record(
    title: str,
    requested_url: str,
    final_url: str,
    opening_format: str,
    opening_text: str = "",
) -> bool:
    """Identify cached IR navigation/event pages that are not source documents."""

    if opening_format != "html_opening":
        return False
    text = f"{title} {requested_url} {final_url}".lower()
    generic = (
        "events & presentations",
        "events-and-presentation",
        "annual report & proxy",
        "annual-reports",
        "sec filings",
        "financial information",
        "quarterly earnings",
        "press releases",
    )
    if any(value in text for value in generic):
        return True
    # An event detail page is useful discovery evidence, but its HTML is an
    # event landing page rather than a release, deck, or transcript.  Keep it
    # out of report documents when the opening advertises only a webcast and
    # linked materials.  A real transcript HTML page remains eligible because
    # it will carry an explicit transcript title/URL and text.
    query_page = "item=" in text or "event=" in text
    return query_page and "webcast" in str(opening_text or "").lower() and not re.search(
        r"transcript|prepared[ -]?remarks", f"{title} {requested_url} {final_url}", re.IGNORECASE
    )


def _manifest_entry(manifest: dict[str, Any], requested: str, final: str) -> dict[str, Any]:
    for key in (requested, final):
        value = manifest.get(key)
        if isinstance(value, dict):
            return value
    # A cached manifest key may differ only by an omitted fragment.
    for key, value in manifest.items():
        if not isinstance(value, dict):
            continue
        if value.get("final_url") in {requested, final}:
            return value
    return {}


def _state_documents(state_db: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(str(state_db))
    try:
        rows = connection.execute("SELECT payload FROM documents ORDER BY id").fetchall()
    finally:
        connection.close()
    documents: list[dict[str, Any]] = []
    for (payload,) in rows:
        with contextlib.suppress(TypeError, ValueError):
            value = json.loads(payload)
            if isinstance(value, dict):
                documents.append(value)
    return documents


def _diagnostic_by_hash(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in paths:
        value = _safe_json(path, {})
        docs = value.get("documents", []) if isinstance(value, dict) else []
        for document in docs if isinstance(docs, list) else []:
            if isinstance(document, dict) and document.get("content_hash"):
                records.setdefault(str(document["content_hash"]), document)
    return records


def build_tfc_ir_metadata_repair(
    state_db: str | Path,
    source_manifest: str | Path,
    output: str | Path,
    *,
    withdrawn_report_docs: str | Path | None = None,
    diagnostic_manifests: Iterable[str | Path] = (),
    ciks: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build an offline TFC IR metadata repair manifest.

    The returned records preserve State IDs, content hashes, and existing
    archive paths.  No bytes are copied and no State table is written.  The
    optional diagnostic manifests let the output show the earlier broad run's
    metadata alongside the current State snapshot.  ``ciks`` is optional for
    backward compatibility (the default is TFC); callers can pass several
    normalized CIKs to repair all selected issuer IR records in one manifest.
    """

    state_path = Path(state_db).resolve()
    manifest_path = Path(source_manifest).resolve()
    output_path = Path(output).resolve()
    target_ciks = {str(value).strip().zfill(10) for value in (ciks or ("0000092230",)) if str(value).strip()}
    if not target_ciks:
        target_ciks = {"0000092230"}
    manifest = _safe_json(manifest_path, {})
    if not isinstance(manifest, dict):
        manifest = {}
    diagnostic = _diagnostic_by_hash(Path(path).resolve() for path in diagnostic_manifests)

    withdrawn_ids: set[str] = set()
    withdrawn_period = "2026-Q2"
    if withdrawn_report_docs:
        report_docs = _safe_json(Path(withdrawn_report_docs).resolve(), [])
        if isinstance(report_docs, dict) and isinstance(report_docs.get("document_ids"), list):
            # A report payload carries exactly the documents attached to the
            # withdrawn event.  Its sibling ``documents.json`` is a broader
            # State snapshot and must not mark every snapshot row as attached.
            withdrawn_ids = {str(value) for value in report_docs["document_ids"] if value}
        elif isinstance(report_docs, list):
            withdrawn_ids = {str(item.get("id")) for item in report_docs if isinstance(item, dict) and item.get("id")}

    all_documents = _state_documents(state_path)
    state_ir = [
        item
        for item in all_documents
        if str(item.get("cik", "")) in target_ciks and str(item.get("source", "")) == "ir"
    ]
    records: list[dict[str, Any]] = []
    for document in sorted(state_ir, key=lambda value: str(value.get("id", ""))):
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        requested_url = str(metadata.get("discovery_url") or document.get("url") or "")
        final_url = str(metadata.get("final_url") or document.get("url") or "")
        title = str(document.get("title") or "")
        path = Path(str(document.get("path") or ""))
        opening_format, opening_text = _read_opening(path)
        title_url_period = infer_period(title, unquote(requested_url), unquote(final_url))
        period = title_url_period
        period_basis = "title_or_url"
        navigation = _is_navigation_record(title, requested_url, final_url, opening_format, opening_text)
        if navigation:
            # A navigation page can contain current and historical links.  Its
            # visible text is not a document's reporting period.
            period = "unknown"
            period_basis = "navigation_page_deferred"
        elif period == "unknown" and opening_format in {"html_opening", "bytes_opening"}:
            content_period = infer_content_period(opening_text, max_chars=30_000)
            if content_period != "unknown":
                period = content_period
                period_basis = "opening_earnings_content"
        kind = title_kind(title, f"{requested_url} {final_url}")
        cover_period, cover_support = _cover_period_support(opening_text) if opening_format == "pdf_page_1" else ("unknown", "")
        url_period = infer_period("", unquote(requested_url), unquote(final_url))
        if url_period == "unknown":
            url_period = title_url_period
        if url_period != "unknown" and cover_period != "unknown":
            url_disagrees: bool | None = url_period != cover_period
        else:
            url_disagrees = None
        bytes_ok = False
        size = 0
        actual_hash = ""
        if path.exists() and path.is_file():
            raw = path.read_bytes()
            size = len(raw)
            actual_hash = hashlib.sha256(raw).hexdigest()
            bytes_ok = actual_hash == str(document.get("content_hash") or "")
        source_entry = _manifest_entry(manifest, requested_url, final_url)
        historical = diagnostic.get(str(document.get("content_hash") or ""), {})
        selected_withdrawn = str(document.get("id") or "") in withdrawn_ids
        flags: list[str] = []
        if navigation:
            flags.append("navigation_or_event_page_excluded_from_document_set")
        if selected_withdrawn:
            flags.append("withdrawn_report_attached_to_2026-Q2_event")
        if historical and str(historical.get("period") or "") != period:
            if str(historical.get("period") or "") == "unknown" and period != "unknown":
                flags.append("period_resolved_from_initial_broad_run")
            else:
                flags.append("period_changed_from_broad_diagnostic")
        if historical and str(historical.get("kind") or "") != kind:
            flags.append("kind_changed_from_broad_diagnostic")
        if url_disagrees is True:
            flags.append("requested_url_disagrees_with_pdf_cover")
        if not bytes_ok:
            flags.append("archive_hash_unverified")
        if not opening_text:
            flags.append("opening_support_unavailable")
        records.append(
            {
                "id": str(document.get("id") or ""),
                "cik": str(document.get("cik") or ""),
                "issuer": document.get("issuer", ""),
                "content_hash": document.get("content_hash", ""),
                "archive": {
                    "path": str(path),
                    "exists": path.exists(),
                    "size_bytes": size,
                    "sha256": actual_hash,
                    "hash_matches_state": bytes_ok,
                    "bytes_changed": False,
                },
                "requested_url": requested_url,
                "final_url": final_url,
                "old_metadata": {
                    "state_period": document.get("period", "unknown"),
                    "state_kind": document.get("kind", ""),
                    "state_title": title,
                    "state_published": document.get("published", ""),
                    "state_path": document.get("path", ""),
                    "state_period_basis": metadata.get("period_basis", ""),
                    "broad_diagnostic_period": historical.get("period", "") if historical else "",
                    "broad_diagnostic_kind": historical.get("kind", "") if historical else "",
                    "withdrawn_event_period": withdrawn_period if selected_withdrawn else "",
                },
                "new_metadata": {
                    "period": period,
                    "kind": kind,
                    "title": title,
                    "published": "",
                    "publication_date_source": "not_established",
                    "period_basis": period_basis,
                    "http_last_modified": source_entry.get("last_modified", ""),
                    "http_date": source_entry.get("date", ""),
                    "id_preserved": True,
                    "content_hash_preserved": True,
                    "path_preserved": True,
                    "exclude_from_reporting": navigation,
                    "exclusion_reason": "IR navigation/event landing page" if navigation else "",
                },
                "support": {
                    "title": title,
                    "requested_url": requested_url,
                    "requested_url_period_hint": infer_period("", unquote(requested_url)),
                    "requested_url_period_token": _url_support(requested_url),
                    "final_url_period_hint": infer_period("", unquote(final_url)),
                    "final_url_period_token": _url_support(final_url),
                    "opening_format": opening_format,
                    "opening_text": opening_text,
                    "pdf_cover_period": cover_period,
                    "pdf_cover_match": cover_support,
                    "requested_url_disagrees_with_pdf_cover": url_disagrees,
                },
                "flags": flags,
            }
        )
    result = {
        "schema_version": "tfc-ir-metadata-repair-v1" if target_ciks == {"0000092230"} else "ir-metadata-repair-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "offline_local_archive_only",
        "network_requests": 0,
        "state": {
            "path": str(state_path),
            "documents_total": len(all_documents),
            "target_ciks": sorted(target_ciks),
            "target_documents_total": sum(1 for item in all_documents if str(item.get("cik", "")) in target_ciks),
            "target_ir_documents": len(records),
        },
        "withdrawn_report": {
            "document_snapshot": str(Path(withdrawn_report_docs).resolve()) if withdrawn_report_docs else "",
            "attached_ids": sorted(withdrawn_ids),
            "attached_event_period": withdrawn_period,
        },
        "rules": {
            "period": "production title/requested/final URL inference, then opening visible HTML only; PDF cover is verification evidence",
            "kind": "production title_kind using title plus requested/final URL",
            "publication": "HTTP Last-Modified/Date are transport metadata; published remains empty without issuer-explicit evidence",
            "bytes": "No bytes copied or changed; archive hash and existing State ID are verified",
        },
        "records": records,
        "summary": {
            "records": len(records),
            "period_changes_from_state": sum(1 for record in records if record["old_metadata"]["state_period"] != record["new_metadata"]["period"]),
            "kind_changes_from_state": sum(1 for record in records if record["old_metadata"]["state_kind"] != record["new_metadata"]["kind"]),
            "published_cleared_as_transport_only": sum(1 for record in records if record["old_metadata"]["state_published"]),
            "pdf_url_cover_mismatches": sum(1 for record in records if record["support"]["requested_url_disagrees_with_pdf_cover"] is True),
            "hash_failures": sum(1 for record in records if not record["archive"]["hash_matches_state"]),
            "withdrawn_event_records": sum(1 for record in records if record["old_metadata"]["withdrawn_event_period"]),
            "navigation_records_excluded": sum(1 for record in records if record["new_metadata"]["exclude_from_reporting"]),
        },
    }
    if target_ciks == {"0000092230"}:
        # Preserve the original field names consumed by the TFC-only review
        # artifact while exposing the normalized generic counts above.
        result["state"]["tfc_documents_total"] = result["state"]["target_documents_total"]
        result["state"]["tfc_ir_documents"] = result["state"]["target_ir_documents"]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def build_ir_metadata_repair(
    state_db: str | Path,
    source_manifest: str | Path,
    output: str | Path,
    *,
    withdrawn_report_docs: str | Path | None = None,
    diagnostic_manifests: Iterable[str | Path] = (),
    ciks: Iterable[str] = ("0000092230", "0000072971"),
) -> dict[str, Any]:
    """Build one offline repair manifest for the selected issuer IR records."""

    return build_tfc_ir_metadata_repair(
        state_db,
        source_manifest,
        output,
        withdrawn_report_docs=withdrawn_report_docs,
        diagnostic_manifests=diagnostic_manifests,
        ciks=ciks,
    )


__all__ = ["build_tfc_ir_metadata_repair", "build_ir_metadata_repair"]
