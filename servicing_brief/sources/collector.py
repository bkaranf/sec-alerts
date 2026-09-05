"""Orchestration for independent SEC and issuer-source discovery."""

from __future__ import annotations

import os
from typing import Any

from servicing_brief.models import SourceResult

from .ir import discover_ir, doctor_ir
from .sec import discover_sec, doctor_sec


def _companies(config: dict[str, Any]) -> list[dict[str, Any]]:
    value = config.get("companies", [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _enabled(company: dict[str, Any]) -> bool:
    value = company.get("enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return bool(value)


def collect(
    config: dict[str, Any],
    *,
    bootstrap: bool = False,
    checkpoints: dict[str, str] | None = None,
) -> SourceResult:
    """Collect all enabled companies with failure isolation.

    SEC and IR calls are intentionally sequential.  SEC calls each acquire a
    shared cross-process guard, and IR calls run independently afterward, so a
    blocked SEC request or missing identity cannot suppress a useful official
    issuer release discovered from the IR site.
    """

    combined = SourceResult()
    checkpoints = dict(checkpoints or {})
    for company in _companies(config):
        if not _enabled(company):
            continue
        ticker = str(company.get("ticker") or company.get("symbol") or "").strip().upper()
        if ticker:
            combined.checked.append(ticker)
        # Keep each source invocation in its own try/except.  This protects
        # other issuers even when an unexpected adapter or test double raises.
        for source_name, worker in (("sec", discover_sec), ("ir", discover_ir)):
            try:
                result = worker(config, company, bootstrap=bootstrap, checkpoints=checkpoints)
            except Exception as exc:
                message = str(exc).strip() or type(exc).__name__
                identity = os.environ.get("EDGAR_IDENTITY", "")
                if identity:
                    message = message.replace(identity, "[identity]")
                result = SourceResult(errors=[{
                    "source": source_name,
                    "source_key": f"{ticker}:{source_name}",
                    "ticker": ticker,
                    "error": " ".join(message.split())[:500],
                    "retryable": False,
                }])
            combined.documents.extend(result.documents)
            combined.errors.extend(result.errors)
            combined.pending.extend(result.pending)
            combined.checked.extend(result.checked)
            combined.checkpoints.update(result.checkpoints)
            checkpoints.update(result.checkpoints)
    # Source workers may rediscover a URL from two configured pages.  Keep the
    # first provenance record; root persistence still owns cross-run hash
    # deduplication and revision tracking.
    unique = []
    seen: set[tuple[str, str, str]] = set()
    for document in combined.documents:
        key = (document.source, document.url, document.content_hash)
        if key in seen:
            continue
        seen.add(key)
        unique.append(document)
    combined.documents = unique
    # checked is a compact issuer list rather than a per-document count.
    combined.checked = list(dict.fromkeys(combined.checked))
    return combined


def doctor_sources(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Return non-secret source diagnostics for all configured companies."""

    output: list[dict[str, Any]] = [doctor_sec(config)]
    for company in _companies(config):
        ticker = str(company.get("ticker") or company.get("symbol") or "").strip().upper()
        row = {
            "ticker": ticker,
            "name": str(company.get("name") or company.get("legal_name") or ""),
            "cik": str(company.get("cik") or ""),
            "enabled": _enabled(company),
            "ir_url_configured": bool(company.get("ir_url") or company.get("ir_pages")),
            "reporting_boundary": company.get("reporting_boundary") or company.get("notes") or "",
        }
        # Probe only enabled issuers during the live doctor.  Disabled
        # candidates stay configuration-only until explicitly enabled, while
        # the enabled set gets a polite primary-page access check.
        row.update({"ir": doctor_ir(config, company, live=_enabled(company))})
        output.append(row)
    return output


__all__ = ["collect", "doctor_sources"]
