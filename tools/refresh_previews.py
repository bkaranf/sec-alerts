"""Rebuild unsent design previews from archived sources, retaining old versions.

This maintenance tool never collects or sends. Accepted or attempted packages
are immutable here; ambiguous/pending delivery requires operator reconciliation.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from filelock import FileLock
from servicing_brief.config import load_config
from servicing_brief.delivery import prepare_messages
from servicing_brief.models import Document
from servicing_brief.pipeline import write_report
from servicing_brief.reporting import build_report
from servicing_brief.state import State


def refresh(config, report_ids=None):
    storage = Path(config["_storage"]).resolve()
    results = []
    with FileLock(str(storage / "run.lock"), timeout=0), FileLock(str(storage / "state.sqlite3") + ".delivery.lock", timeout=0):
        state = State(storage / "state.sqlite3")
        try:
            documents = state.documents()
            by_id = {d.id: d for d in documents}
            for row in state.outstanding():
                if row["status"] != "prepared":
                    continue
                if report_ids and row["id"] not in report_ids:
                    continue
                old = json.loads(row["payload"])
                output = Path(row["output_dir"]).resolve()
                if not output.is_relative_to(storage / "reports"):
                    raise ValueError("Report path is outside the configured reports directory")
                # No message from this directory may have entered SMTP.
                if state.db.execute("SELECT 1 FROM sqlite_master WHERE name='delivery_messages'").fetchone():
                    for message in state.db.execute("SELECT * FROM delivery_messages"):
                        record = dict(message)
                        if Path(record["path"]).resolve().parent == output and (record.get("attempts", 0) > 0 or record.get("status") in {"accepted", "ambiguous"}):
                            raise ValueError("A package in this report directory has delivery history; preserve it")
                current = [by_id.get(d["id"], Document(**d)) for d in json.loads((output / "documents.json").read_text(encoding="utf-8"))]
                current_ids = {d.id for d in current}
                cik = current[0].cik
                previous = [d for d in documents if d.cik == cik and d.id not in current_ids]
                report = build_report(config, current, baseline=bool(row["baseline"]), coverage=old["coverage"], previous_documents=previous)
                report.update({"id": old["id"], "document_ids": old["document_ids"]})
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                version = output / "versions" / stamp
                version.mkdir(parents=True)
                for path in list(output.iterdir()):
                    if path.is_file():
                        path.rename(version / path.name)
                try:
                    write_report(report, output)
                    (output / "documents.json").write_text(json.dumps([d.to_dict() for d in current], indent=2), encoding="utf-8")
                    messages = prepare_messages(config, report, state.report_documents(row["id"]), output)
                except Exception:
                    # Keep the old durable payload and restore its files on failure.
                    failed = version / "failed-refresh"
                    failed.mkdir()
                    for path in list(output.iterdir()):
                        if path.is_file():
                            path.rename(failed / path.name)
                    for path in list(version.iterdir()):
                        if path.is_file():
                            path.rename(output / path.name)
                    raise
                with state.db:
                    state.db.execute("UPDATE briefings SET payload=? WHERE id=? AND status='prepared'", (json.dumps(report), row["id"]))
                results.append({"report_id": row["id"], "output": str(output), "messages": [str(p) for p in messages], "previous_version": str(version)})
        finally:
            state.close()
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--report-id", action="append", help="Refresh only this unsent report; repeat to select several")
    args = parser.parse_args()
    print(json.dumps(refresh(load_config(args.config), args.report_id), indent=2))
