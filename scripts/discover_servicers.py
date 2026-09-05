"""Metadata-only SEC EFTS discovery for mortgage-servicing candidates.

This script intentionally does not download filings.  It snapshots the public
EdgarTools ticker references and paginates the SEC full-text search index for
the configured phrase/form/date matrix.  Text hits are candidate signals only;
eligibility and operating boundaries require a separate review.

The script is designed to be rerunnable.  Each query/form result is written as
soon as its pagination completes, and the final manifest records the exact
inputs, totals, page counts, raw metadata paths, and coverage limits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Allow the documented ``python scripts/discover_servicers.py`` invocation to
# resolve the repository package when Python places only ``scripts/`` on
# sys.path.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from servicing_brief.sources.ratelimit import sec_acquisition_guard


DEFAULT_QUERIES = [
    '"mortgage servicing"',
    '"subservicing"',
    '"special servicing"',
    '"master servicing"',
    '"servicing retained"',
    '"servicing rights"',
]
DEFAULT_FORMS = [
    "10-K",
    "10-Q",
    "20-F",
    "40-F",
    "10-K/A",
    "10-Q/A",
    "20-F/A",
    "40-F/A",
]
PAGINATION_CAP = 10_000
PAGE_LIMIT = 100
DISPLAY_NAME_SUFFIX = re.compile(r"\s{2,}\([^)]*\)")
NAME_SIGNAL_WORDS = (
    "mortgage",
    "servic",
    "loan",
    "home",
    "bank",
    "financial",
    "finance",
    "capital",
    "realty",
    "trust",
    "lending",
    "funding",
    "credit",
    "property",
    "asset",
    "commercial",
    "invest",
    "management",
    "specialty",
    "residential",
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    """Keep the archive serializable without losing scalar metadata."""

    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8")


def clean_display_name(value: Any) -> str:
    raw = str(value or "").strip()
    return DISPLAY_NAME_SUFFIX.sub("", raw).strip()


def normalized_cik(value: Any) -> str:
    text = str(value or "").strip()
    digits = re.sub(r"\D", "", text)
    return digits.zfill(10) if digits else ""


def result_dict(result: Any) -> dict[str, Any]:
    """Extract only public EFTSResult fields, preserving parsed raw metadata."""

    fields = (
        "accession_number",
        "form",
        "filed",
        "company",
        "cik",
        "period",
        "score",
        "file_type",
        "file_description",
        "document_id",
        "items",
        "sic",
        "location",
        "state",
        "inc_state",
    )
    return {field: getattr(result, field, None) for field in fields}


def error_text(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def blocked_error(exc: BaseException) -> bool:
    text = error_text(exc).lower()
    return any(token in text for token in ("403", "429", "503", "blocked", "too many requests", "rate limit"))


def name_signals(name: str) -> list[str]:
    lowered = name.lower()
    return [word for word in NAME_SIGNAL_WORDS if word in lowered]


def ticker_records(frame: Any) -> list[dict[str, Any]]:
    # DataFrame is part of the documented get_company_tickers return contract.
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def snapshot_ticker_references(output: Path, get_company_tickers: Any, get_company_ticker_name_exchange: Any) -> dict[str, Any]:
    """Save both the bundled mapping and the live SEC exchange directory."""

    bundled = get_company_tickers(as_dataframe=True, clean_name=False, clean_suffix=False)
    bundled_rows = ticker_records(bundled)
    bundled_json = output / "ticker-reference-bundled.json"
    bundled_csv = output / "ticker-reference-bundled.csv"
    write_json(
        bundled_json,
        {
            "snapshot_at": now_utc(),
            "api": "edgar.get_company_tickers",
            "arguments": {"as_dataframe": True, "clean_name": False, "clean_suffix": False},
            "source": "EdgarTools bundled parquet when available; this public API may fall back to SEC company_tickers.json",
            "rows": len(bundled_rows),
            "columns": list(bundled.columns),
            "records": bundled_rows,
        },
    )
    bundled.to_csv(bundled_csv, index=False)

    live = get_company_ticker_name_exchange()
    live_rows = ticker_records(live)
    live_json = output / "ticker-reference-live-exchange.json"
    live_csv = output / "ticker-reference-live-exchange.csv"
    write_json(
        live_json,
        {
            "snapshot_at": now_utc(),
            "api": "edgar.reference.tickers.get_company_ticker_name_exchange",
            "arguments": {},
            "source": "Live SEC company_tickers_exchange.json retrieved through the installed public EdgarTools API",
            "rows": len(live_rows),
            "columns": list(live.columns),
            "records": live_rows,
        },
    )
    live.to_csv(live_csv, index=False)

    def files() -> list[dict[str, Any]]:
        return [
            {"path": str(path.resolve()), "sha256": sha256(path), "bytes": path.stat().st_size}
            for path in (bundled_json, bundled_csv, live_json, live_csv)
        ]

    return {
        "bundled": {"json": str(bundled_json.resolve()), "csv": str(bundled_csv.resolve()), "rows": len(bundled_rows), "columns": list(bundled.columns)},
        "live_exchange": {"json": str(live_json.resolve()), "csv": str(live_csv.resolve()), "rows": len(live_rows), "columns": list(live.columns)},
        "files": files(),
        "bundled_records": bundled_rows,
        "live_records": live_rows,
    }


def collect_pages(search: Any, query: str, form: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Consume every public EFTSSearch page until ``next()`` is exhausted."""

    initial_total = int(search.total)
    if initial_total > PAGINATION_CAP:
        raise RuntimeError(f"{query} / {form} reports {initial_total} hits, above EFTS deep-pagination cap; split date windows")

    records: list[dict[str, Any]] = []
    pages = 0
    offsets: list[int] = []
    current = search
    consumed = 0
    seen_keys: set[str] = set()
    while current is not None:
        pages += 1
        offsets.append(consumed)
        page_results = list(current.results)
        for page_index, result in enumerate(page_results):
            raw = result_dict(result)
            key = f"{raw.get('accession_number') or ''}:{raw.get('document_id') or ''}:{raw.get('file_type') or ''}"
            seen_keys.add(key)
            raw.update({"query": query, "form_request": form, "page": pages, "offset": consumed, "page_index": page_index})
            records.append(raw)
        consumed += len(page_results)
        if consumed >= initial_total:
            current = None
            break
        if not page_results:
            current = None
            break
        time.sleep(0.12)
        current = current.next()

    audit = {
        "query": query,
        "form": form,
        "total_reported": initial_total,
        "pages_fetched": pages,
        "page_limit": PAGE_LIMIT,
        "page_offsets": offsets,
        "results_returned_by_pages": consumed,
        # Keep every public EFTS result in the raw archive.  The same
        # accession/document can appear more than once in an index response;
        # candidate grouping later deduplicates by CIK, while this audit keeps
        # both the server return count and an exact-hit key count.
        "unique_metadata_hits": len(seen_keys),
        "truncated": consumed < initial_total,
        "pagination_complete": consumed == initial_total,
        "duplicate_exact_hit_count": max(0, consumed - len(seen_keys)),
        "pagination_cap": PAGINATION_CAP,
    }
    return records, audit


def split_date_bounds(start: date, end: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """Split an inclusive date range into adjacent, non-overlapping windows."""

    midpoint = start + (end - start) // 2
    return (start, midpoint), (midpoint + timedelta(days=1), end)


def collect_query_form(
    search_filings: Any,
    query: str,
    form: str,
    start: date,
    end: date,
    final_dir: Path,
    initial_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Collect a query/form, splitting any incomplete deep-pagination range.

    Some EFTS result sets return an accurate total but an empty page before
    exhaustion.  The public ``next()`` contract then cannot finish that range;
    adjacent date windows avoid the server deep-pagination failure while
    retaining all metadata returned by each completed window.
    """

    final_rows: list[dict[str, Any]] = []
    final_audits: list[dict[str, Any]] = []
    initial_audits: list[dict[str, Any]] = []
    split_used = False

    def recurse(window_start: date, window_end: date, depth: int) -> None:
        nonlocal split_used
        start_text, end_text = window_start.isoformat(), window_end.isoformat()
        search = search_filings(query, forms=form, start_date=start_text, end_date=end_text, limit=PAGE_LIMIT)
        reported_total = int(search.total)
        if reported_total > PAGINATION_CAP:
            partial_audit = {
                "query": query,
                "form": form,
                "window_start": start_text,
                "window_end": end_text,
                "split_depth": depth,
                "total_reported": reported_total,
                "pagination_cap": PAGINATION_CAP,
                "pagination_complete": False,
                "reason": "reported total exceeds deep-pagination cap; date window split before page traversal",
            }
            initial_audits.append(partial_audit)
            if window_start >= window_end:
                raise RuntimeError(f"{query} / {form} remains above pagination cap on {start_text}")
            split_used = True
            for child_start, child_end in split_date_bounds(window_start, window_end):
                recurse(child_start, child_end, depth + 1)
            return

        rows, page_audit = collect_pages(search, query, form)
        page_audit.update({"window_start": start_text, "window_end": end_text, "split_depth": depth})
        slug = re.sub(r"[^a-z0-9]+", "-", f"{query}-{form}-{start_text}-{end_text}".lower()).strip("-")
        if page_audit["pagination_complete"]:
            page_path = final_dir / f"{slug}.json"
            write_json(page_path, {"query": query, "form": form, "start_date": start_text, "end_date": end_text, "audit": page_audit, "results": rows})
            page_audit["path"] = str(page_path.resolve())
            page_audit["sha256"] = sha256(page_path)
            final_audits.append(page_audit)
            final_rows.extend(rows)
            return

        # Keep the incomplete public page traversal as a diagnostic archive,
        # then split the range if possible.  It is never included in final
        # candidate hits, so partial rows cannot be mistaken for complete data.
        partial_path = initial_dir / f"{slug}-partial.json"
        write_json(partial_path, {"query": query, "form": form, "start_date": start_text, "end_date": end_text, "audit": page_audit, "results": rows})
        page_audit["path"] = str(partial_path.resolve())
        page_audit["sha256"] = sha256(partial_path)
        initial_audits.append(page_audit)
        if window_start >= window_end:
            raise RuntimeError(f"{query} / {form} remains incomplete on {start_text}")
        split_used = True
        for child_start, child_end in split_date_bounds(window_start, window_end):
            recurse(child_start, child_end, depth + 1)

    recurse(start, end, 0)
    return final_rows, final_audits, initial_audits, split_used


def build_candidates(hits: list[dict[str, Any]], ticker_meta: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for hit in hits:
        cik = normalized_cik(hit.get("cik"))
        if not cik:
            continue
        grouped.setdefault(cik, []).append(hit)

    candidates: list[dict[str, Any]] = []
    for cik, rows in grouped.items():
        names = sorted({clean_display_name(row.get("company")) for row in rows if clean_display_name(row.get("company"))})
        company_name = names[0] if names else ""
        refs = ticker_meta.get(cik, [])
        tickers = sorted({str(row.get("ticker") or "").strip() for row in refs if row.get("ticker")})
        exchanges = sorted({str(row.get("exchange") or "").strip() for row in refs if row.get("exchange")})
        sics = sorted({str(row.get("sic") or "").strip() for row in rows if row.get("sic")})
        forms = sorted({str(row.get("form") or "").strip() for row in rows if row.get("form")})
        query_counts = {}
        for row in rows:
            q = str(row.get("query") or "")
            query_counts[q] = query_counts.get(q, 0) + 1
        filed_dates = sorted({str(row.get("filed") or "") for row in rows if row.get("filed")})
        sample_rows = sorted(rows, key=lambda row: (str(row.get("filed") or ""), float(row.get("score") or 0)), reverse=True)[:8]
        name_words = sorted({word for name in names for word in name_signals(name)})
        passive_signal = any(sic == "6189" for sic in sics) or any(any(word in name.lower() for word in ("trust", "securit", "mortgage-backed", "pass-through")) for name in names)
        candidates.append(
            {
                "cik": cik,
                "company_name": company_name,
                "alternate_names": names,
                "ticker_reference": {"tickers": tickers, "exchanges": exchanges, "rows": refs},
                "name_candidate_signals": name_words,
                "name_signal_is_evidence": False,
                "sic_values": sics,
                "passive_securitization_signal": passive_signal,
                "hit_count": len(rows),
                "query_hit_counts": query_counts,
                "forms": forms,
                "latest_filed": filed_dates[-1] if filed_dates else "",
                "earliest_filed": filed_dates[0] if filed_dates else "",
                "accession_count": len({str(row.get("accession_number") or "") for row in rows}),
                "sample_hits": [
                    {
                        "query": row.get("query"),
                        "form": row.get("form"),
                        "accession_number": row.get("accession_number"),
                        "filed": row.get("filed"),
                        "period": row.get("period"),
                        "file_type": row.get("file_type"),
                        "document_id": row.get("document_id"),
                        "sic": row.get("sic"),
                    }
                    for row in sample_rows
                ],
                "classification": "unclassified_text_hit",
            }
        )

    def rank(candidate: dict[str, Any]) -> tuple[Any, ...]:
        # Ranking only helps human review throughput.  It is not an eligibility decision.
        operating_sic = any(sic in {"6162", "6021", "6022", "6035", "6159", "6282", "6799"} for sic in candidate["sic_values"])
        signal_count = len(candidate["name_candidate_signals"])
        return (not operating_sic, not bool(candidate["name_candidate_signals"]), -signal_count, -candidate["hit_count"], candidate["company_name"].lower(), candidate["cik"])

    return sorted(candidates, key=rank)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2025-01-01")
    parser.add_argument("--end-date", default="2026-09-05")
    parser.add_argument("--output", default="output/servicer-universe")
    return parser.parse_args()


def validate_date(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    args = parse_args()
    start = validate_date(args.start_date)
    end = validate_date(args.end_date)
    if start > end:
        raise SystemExit("start date must be on or before end date")
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / "efts-pages"
    initial_dir = output / "efts-initial-pages"
    raw_dir.mkdir(parents=True, exist_ok=True)
    initial_dir.mkdir(parents=True, exist_ok=True)
    queries = list(DEFAULT_QUERIES)
    forms = list(DEFAULT_FORMS)
    run_started = now_utc()
    audit: dict[str, Any] = {
        "schema_version": "servicer-universe-v1",
        "status": "running",
        "started_at": run_started,
        "as_of_date": args.end_date,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "queries": queries,
        "query_strategy": "distinct phrase requests because installed EFTS search_filings documentation does not document an OR operator",
        "forms": forms,
        "form_strategy": "individual form requests; a comma-separated multi-form request was validated as incomplete and is retained only in probe artifacts",
        "pagination": {"page_limit": PAGE_LIMIT, "cap": PAGINATION_CAP, "date_window_splitting_used": False},
        "raw_metadata": {"page_files": [], "initial_page_files": [], "aggregate_file": ""},
        "errors": [],
        "blocked": False,
    }
    if not os.environ.get("EDGAR_IDENTITY", "").strip():
        audit.update({"status": "blocked", "blocked": True, "errors": [{"error": "EDGAR_IDENTITY is not configured (configured=no)"}], "finished_at": now_utc()})
        write_json(output / "manifest.json", audit)
        return 2

    all_hits: list[dict[str, Any]] = []
    query_audits: list[dict[str, Any]] = []
    with sec_acquisition_guard(output, timeout=0) as guard:
        audit["guard"] = {key: value for key, value in guard.items() if key != "lock_path"}
        if not guard.get("acquired") or guard.get("manager_verified") is False:
            audit.update({"status": "blocked", "blocked": True, "errors": [{"error": str(guard.get("error") or "SEC guard unavailable")}], "finished_at": now_utc()})
            write_json(output / "manifest.json", audit)
            return 3

        # Import EdgarTools only after the guard configures the <=5 RPS setting.
        from edgar import get_company_tickers, search_filings
        from edgar.reference.tickers import get_company_ticker_name_exchange

        ticker_snapshot = snapshot_ticker_references(output, get_company_tickers, get_company_ticker_name_exchange)
        audit["ticker_references"] = {key: value for key, value in ticker_snapshot.items() if key.endswith("bundled") or key.endswith("exchange") or key == "files"}
        live_records = ticker_snapshot["live_records"]
        bundled_records = ticker_snapshot["bundled_records"]
        ticker_meta: dict[str, list[dict[str, Any]]] = {}
        for record in live_records + bundled_records:
            cik = normalized_cik(record.get("cik"))
            if cik:
                ticker_meta.setdefault(cik, []).append(record)

        for query in queries:
            for form in forms:
                try:
                    rows, window_audits, initial_audits, split_used = collect_query_form(
                        search_filings,
                        query,
                        form,
                        start,
                        end,
                        raw_dir,
                        initial_dir,
                    )
                    query_audits.extend(window_audits)
                    audit["pagination"]["date_window_splitting_used"] = bool(audit["pagination"]["date_window_splitting_used"] or split_used)
                    for query_audit in window_audits:
                        audit["raw_metadata"]["page_files"].append({"path": query_audit["path"], "sha256": query_audit["sha256"], "query": query, "form": form, "window_start": query_audit["window_start"], "window_end": query_audit["window_end"], "hits": query_audit["results_returned_by_pages"]})
                    for initial_audit in initial_audits:
                        if initial_audit.get("path"):
                            audit["raw_metadata"]["initial_page_files"].append({"path": initial_audit["path"], "sha256": initial_audit.get("sha256", ""), "query": query, "form": form, "window_start": initial_audit.get("window_start", ""), "window_end": initial_audit.get("window_end", ""), "hits": initial_audit.get("results_returned_by_pages", 0), "complete": False})
                    all_hits.extend(rows)
                    # This checkpoint lets a reviewer see progress while the matrix runs.
                    write_json(output / "manifest.json", {**audit, "query_audits": query_audits, "initial_probes": audit["raw_metadata"]["initial_page_files"], "raw_hits_collected": len(all_hits), "candidate_count_so_far": len(build_candidates(all_hits, ticker_meta))})
                except Exception as exc:
                    entry = {"query": query, "form": form, "error": error_text(exc), "blocked": blocked_error(exc)}
                    audit["errors"].append(entry)
                    if entry["blocked"]:
                        audit["blocked"] = True
                        audit["status"] = "blocked"
                        write_json(output / "manifest.json", {**audit, "query_audits": query_audits, "raw_hits_collected": len(all_hits)})
                        return 4
                    # A non-blocking query error remains visible and the matrix continues.

    aggregate_path = output / "efts-metadata-hits.json"
    write_json(aggregate_path, {"as_of_date": args.end_date, "start_date": args.start_date, "end_date": args.end_date, "queries": queries, "forms": forms, "results": all_hits})
    audit["raw_metadata"]["aggregate_file"] = {"path": str(aggregate_path.resolve()), "sha256": sha256(aggregate_path), "hits": len(all_hits)}
    candidates = build_candidates(all_hits, ticker_meta)
    candidates_path = output / "candidates.json"
    write_json(candidates_path, {"as_of_date": args.end_date, "selection_status": "unclassified_text_hits", "candidate_count": len(candidates), "not_eligibility_evidence": True, "candidates": candidates})
    audit.update({
        "status": "completed" if not audit["errors"] else "completed_with_errors",
        "finished_at": now_utc(),
        "query_audits": query_audits,
        "raw_hits_collected": len(all_hits),
        "unique_accession_document_hits": len({f"{r.get('accession_number')}:{r.get('document_id')}:{r.get('file_type')}" for r in all_hits}),
        "candidate_count": len(candidates),
        "candidate_file": str(candidates_path.resolve()),
        "coverage": {
            "all_edgar_full_text_index": True,
            "forms_requested": forms,
            "date_range_inclusive": [args.start_date, args.end_date],
            "foreign_reporter_forms_included": ["20-F", "40-F", "20-F/A", "40-F/A"],
            "metadata_only": True,
            "filing_documents_downloaded": False,
            "date_window_splitting_required": bool(audit["pagination"]["date_window_splitting_used"]),
            "initial_incomplete_traversals_archived": len(audit["raw_metadata"]["initial_page_files"]),
        },
        "limitations": [
            "EFTS phrase hits show indexed text matches only; they do not establish mortgage-servicing eligibility, operating control, MSR ownership, or current activity.",
            "Securitization trusts and pass-through/depositor filings, including SIC 6189, are retained as candidate signals for separate exclusion review and are not silently removed.",
            "Ticker reference names and keyword signals are routing aids, not evidence of servicing operations.",
            "Foreign annual reports may have incomplete EFTS text indexing; 20-F and 40-F hits are included, but issuer annual reports remain a separate cross-check.",
            "The installed search_filings documentation does not document OR syntax, so six phrase queries were executed independently and deduplicated only for candidate CIK grouping.",
        ],
    })
    write_json(output / "manifest.json", audit)
    print(json.dumps({"status": audit["status"], "raw_hits": len(all_hits), "candidates": len(candidates), "manifest": str((output / "manifest.json").resolve()), "candidate_file": str(candidates_path.resolve())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
