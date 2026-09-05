"""Small deterministic collect -> evidence -> preview/send pipeline."""
import hashlib
import json
import logging
import re
from pathlib import Path
from filelock import FileLock, Timeout
from .models import Document, SourceResult, utcnow
from .state import State
from .reader_value_release import refresh_report_review
from .company_boundary import require_company_boundary
from .reader_content import assert_reader_content

log = logging.getLogger(__name__)


def event_key(doc: Document) -> str:
    # Undated events are accession-specific, never forced into the filing quarter.
    # Full-year and fiscal Q4 earnings share the fiscal year-end reporting event
    # while each fact retains its own duration; source periods are authoritative.
    period = doc.period.replace("-FY", "-Q4")
    return f"{doc.cik}:{period}" if re.fullmatch(r"\d{4}-Q[1-4]", period) else f"{doc.cik}:unknown:{doc.accession or doc.id}"


def period_order(period: str):
    match = re.fullmatch(r"(\d{4})-(Q([1-4])|FY)", period)
    return (int(match[1]), int(match[3] or 4)) if match else (0, 0)


def select_updates(documents: list[Document], covered: set[str], baselined: set[str]):
    """First coverage is one latest package per issuer, with previous docs kept for comparisons."""
    selected, historical = [], []
    latest = {}
    for doc in documents:
        latest[doc.cik] = max(latest.get(doc.cik, (0, 0)), period_order(doc.period))
    latest_event_dates = {}
    for doc in documents:
        if doc.published and period_order(doc.period) == latest[doc.cik] and latest[doc.cik] != (0, 0):
            latest_event_dates[doc.cik] = min(latest_event_dates.get(doc.cik, doc.published[:10]), doc.published[:10])
    for doc in documents:
        if doc.id in covered:
            continue
        previous_version = any(old.cik == doc.cik and old.url == doc.url and old.id in covered and old.content_hash != doc.content_hash for old in documents)
        amendment = doc.kind.upper().endswith("/A") or bool(re.search(r"amend|restat|corrected|revision", doc.title, re.I))
        old_period = period_order(doc.period) not in ((0, 0), latest[doc.cik])
        old_publication = not doc.published or doc.published[:10] <= latest_event_dates.get(doc.cik, "")
        if doc.cik in baselined and old_period and old_publication and not amendment and not previous_version:
            # Later acquisition of an old comparison document is backfill,
            # while actual amendments, revisions and newer publications alert.
            historical.append(doc)
            continue
        if doc.cik not in baselined and period_order(doc.period) not in ((0, 0), latest[doc.cik]):
            historical.append(doc)
        elif doc.cik not in baselined and period_order(doc.period) == (0, 0) and doc.published[:10] < latest_event_dates.get(doc.cik, ""):
            historical.append(doc)
        else:
            selected.append(doc)
    return selected, historical


def write_report(report, output: Path):
    require_company_boundary(report)
    assert_reader_content(report['html'], report['text'], subject=report.get('subject', ''))
    for company in report.get('company_reports', {}).values():
        assert_reader_content(company['html'], company['text'])
    output.mkdir(parents=True, exist_ok=True)
    (output / "briefing.html").write_text(report["html"], encoding="utf-8")
    (output / "briefing.txt").write_text(report["text"], encoding="utf-8")
    (output / "evidence.json").write_text(json.dumps(report.get("evidence", []), indent=2), encoding="utf-8")
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    for ticker, company in report.get("company_reports", {}).items():
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", ticker)
        (output / f"{safe}-briefing.html").write_text(company["html"], encoding="utf-8")
        (output / f"{safe}-briefing.txt").write_text(company["text"], encoding="utf-8")


def run(config: dict, *, bootstrap=False, send=False, offline=False, collector=None):
    ai = {**config.get("ai", {})}
    ai["_usage_budget"] = {"max_requests": ai.get("max_requests_per_run", 4), "used": 0}
    config = {**config, "ai": ai, "_send": bool(send)}
    storage = Path(config["_storage"])
    storage.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(storage / "run.lock"), timeout=0):
            return _run_locked(config, bootstrap=bootstrap, send=send, offline=offline, collector=collector)
    except Timeout:
        return {"status": "overlap_skipped", "detail": "Another run holds the storage lock; no work or sends performed."}


def _run_locked(config, *, bootstrap=False, send=False, offline=False, collector=None):
    from .reporting import build_report
    from .delivery import prepare_messages, deliver_messages
    state = State(Path(config["_storage"]) / "state.sqlite3")
    run_id = state.start_run()
    try:
        if offline:
            result = SourceResult(checked=[c["ticker"] for c in config["companies"] if c.get("enabled")], pending=state.pending())
        else:
            if collector is None:
                from .sources import collect
                collector = collect
            config["_pending"] = state.pending()
            result = collector(config, bootstrap=bootstrap or not state.baseline_ciks(), checkpoints=state.checkpoints())
            state.ingest(result)
        all_documents = state.documents()
        enabled = {c["cik"] for c in config["companies"] if c.get("enabled")}
        documents = [d for d in all_documents if d.cik in enabled and not d.metadata.get("exclude_from_reporting")]
        updates, historical = select_updates(documents, state.covered_ids(), state.baseline_ciks())
        with state.db:
            state.db.executemany("INSERT OR IGNORE INTO historical_documents VALUES(?)", [(d.id,) for d in historical])
        outputs = []
        preparation_errors = []
        delivery_errors = []
        first_error = None
        # Keep an older-period correction separate from a new earnings event,
        # even when both belong to the same company and arrive in one check.
        events = sorted({(d.cik, event_key(d)) for d in updates},
                        key=lambda item: (item[0], max(period_order(d.period) for d in updates if event_key(d) == item[1])),
                        reverse=True)
        for cik, changed_event in events:
            issuer_updates = [d for d in updates if d.cik == cik and event_key(d) == changed_event]
            baseline = cik not in state.baseline_ciks()
            current_docs = [d for d in documents if d.cik == cik and event_key(d) == changed_event]
            previous_docs = [d for d in documents if d.cik == cik and d not in current_docs]
            coverage = {"checked": result.checked, "companies_checked": result.checked, "errors": result.errors,
                        "pending": result.pending, "as_of": utcnow(), "offline": offline,
                        "new_document_ids": [d.id for d in issuer_updates], "new_companies": sorted({d.issuer for d in issuer_updates}),
                        "previously_briefed_ids": sorted(state.covered_ids()),
                        "notice": "Local archive replay; source freshness not checked." if offline else ""}
            try:
                report = build_report(config, current_docs, baseline=baseline, coverage=coverage, previous_documents=previous_docs)
                report["document_ids"] = [d.id for d in issuer_updates]
                report["coverage"] = coverage
                identity = {"event": changed_event, "documents": sorted((d.id, d.period, d.kind) for d in issuer_updates),
                            "design_version": report.get("design_version", "")}
                report["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
                output = Path(config["_storage"]) / "reports" / report["id"]
                refresh_report_review(report, output)
                write_report(report, output)
                (output / "documents.json").write_text(json.dumps([d.to_dict() for d in current_docs], indent=2), encoding="utf-8")
                message_paths = prepare_messages(config, report, issuer_updates, output)
                # One company is durable only after its complete preview package.
                state.save_report(report["id"], report, issuer_updates, output, baseline, [d for d in historical if d.cik == cik])
                outputs.append({"report_id": report["id"], "issuer": issuer_updates[0].issuer, "cik": cik, "output": str(output), "messages": [str(p) for p in message_paths], "baseline": baseline})
            except Exception as exc:
                first_error = first_error or exc
                issue = {"source": "preparation", "cik": cik, "issuer": issuer_updates[0].issuer, "status": "failed", "error_type": type(exc).__name__}
                preparation_errors.append(issue)
                state.ingest(SourceResult(errors=[issue]))
        # A failed new disclosure must not suppress delivery of an older
        # durable draft that the caller explicitly requested.  Preserve the
        # historical all-failed behavior when there is no outstanding package
        # to send, while allowing the send loop to isolate each prior report.
        outstanding = state.outstanding() if send else []
        if first_error and not outputs and not outstanding:
            raise first_error
        deliveries = []
        if send:
            for row in outstanding:
                # A durable report is independent of every other issuer's
                # package.  Keep the row outstanding if preparation or
                # delivery raises: the delivery layer may already have
                # recorded an accepted/ambiguous message before an
                # unexpected exception reached this boundary.
                phase = "preparation"
                docs = []
                try:
                    report = json.loads(row["payload"])
                    docs = state.report_documents(row["id"])
                    value_review = refresh_report_review(report, Path(row["output_dir"]))
                    if value_review['status'] != 'approved':
                        deliveries.append({'report_id': row['id'], 'status': 'editorial_hold',
                                           'detail': 'P0 reader-value review is missing, stale or unresolved; no send attempted.'})
                        continue
                    paths = prepare_messages(config, report, docs, Path(row["output_dir"]))
                    phase = "delivery"
                    responses = deliver_messages(config, paths, state.path)
                    # Surface provider outcomes before updating the aggregate
                    # report row.  If that bookkeeping update itself fails,
                    # an accepted response remains visible and the report is
                    # retried conservatively on a later run.
                    deliveries.extend(responses)
                    statuses = {r.get("status") for r in responses}
                    status = "accepted" if statuses and statuses <= {"accepted", "already_accepted"} else "ambiguous" if "ambiguous" in statuses else "pending"
                    state.set_report_status(row["id"], status)
                except Exception as exc:
                    # External exception text can contain credentials, raw
                    # SMTP payloads, or provider response bodies.  Persist
                    # only safe classification metadata and log the type.
                    issue = {
                        "source": "delivery",
                        "phase": phase,
                        "report_id": str(row.get("id", "")),
                        "cik": str(docs[0].cik) if docs else "",
                        "issuer": str(docs[0].issuer) if docs else "",
                        "status": "failed",
                        "error_type": type(exc).__name__,
                    }
                    delivery_errors.append(issue)
                    log.error(
                        "run %s report %s %s failed (%s)",
                        run_id,
                        issue["report_id"],
                        phase,
                        issue["error_type"],
                    )
        result_status = "prepared" if outputs else "no_new_disclosures"
        if not outputs and result.errors:
            result_status = "collection_incomplete"
        if send and (deliveries or delivery_errors):
            result_status = (
                "provider_accepted"
                if deliveries
                and not delivery_errors
                and all(d.get("status") in {"accepted", "already_accepted"} for d in deliveries)
                else "delivery_incomplete"
            )
        if preparation_errors:
            result_status = "partial_failure"
        response = {"status": result_status, "reports": outputs, "delivery": deliveries, "delivery_errors": delivery_errors, "sources_failed": result.errors, "preparation_errors": preparation_errors, "pending": result.pending}
        state.finish_run(run_id, result_status, json.dumps(response))
        log.info("run %s: %s; %d reports; %d source failures", run_id, result_status, len(outputs), len(result.errors))
        return response
    except Exception as exc:
        # Never log credentials or raw exception payloads from external services.
        state.finish_run(run_id, "failed", type(exc).__name__)
        log.error("run %s failed (%s)", run_id, type(exc).__name__)
        raise
    finally:
        state.close()
