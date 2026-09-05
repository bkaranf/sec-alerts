"""Local artifact audit. Does not contact sources or send mail."""
import argparse
from decimal import Decimal
from email import policy
from email.parser import BytesParser
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile
import json
from pathlib import Path
import sys
import pymupdf
from bs4 import BeautifulSoup

from servicing_brief.config import load_config
from servicing_brief.state import State
from servicing_brief.pipeline import event_key
from servicing_brief.models import Document


def audit(config, report_dir):
    state = State(Path(config["_storage"]) / "state.sqlite3")
    documents = state.documents()
    state.close()
    known_hashes = {d.content_hash: d for d in documents}
    asset_hashes = {a["content_hash"] for d in documents for a in d.metadata.get("assets", [])}
    known_ids = {d.id: d for d in documents}
    errors, archived, messages = [], [], []
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    if "—" in BeautifulSoup(report["html"], "html.parser").get_text() or "—" in report["text"]:
        errors.append("Displayed brief contains an em dash")
    report_docs = [Document(**d) for d in json.loads((report_dir / "documents.json").read_text(encoding="utf-8"))]
    report_ciks = {d.cik for d in report_docs}
    if len(report_ciks) != 1 or len({event_key(d) for d in report_docs}) != 1:
        errors.append("Draft mixes companies or reporting events")
    context_docs = [Document(**d) for d in report.get("context_documents", [])]
    for doc in context_docs:
        if doc.cik not in report_ciks or doc.kind.upper() not in {"10-K", "10-K/A"}:
            errors.append("Invalid annual context attachment")
    own_hashes = {d.content_hash for d in [*report_docs, *context_docs]}
    own_assets = {a["content_hash"] for d in report_docs for a in d.metadata.get("assets", [])}
    for doc in documents:
        path = Path(doc.path)
        if not path.exists() or sha256(path.read_bytes()).hexdigest() != doc.content_hash:
            errors.append(f"Archive integrity failed: {doc.id}")
            continue
        item = {"id": doc.id, "issuer": doc.issuer, "kind": doc.kind, "period": doc.period, "bytes": path.stat().st_size, "hash_verified": True}
        if path.read_bytes()[:5] == b"%PDF-":
            with pymupdf.open(path) as pdf:
                item["pdf_pages"] = len(pdf)
                if not len(pdf):
                    errors.append(f"Empty PDF: {doc.id}")
        archived.append(item)
    for path in sorted(report_dir.glob("*.eml")):
        raw = path.read_bytes()
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        item = {"path": str(path), "encoded_bytes": len(raw), "attachments": [], "has_plain": False, "has_html": False,
                "recipient_configured": bool(msg.get("To")), "message_id": msg.get("Message-ID", "")}
        if len(raw) > config["email"]["max_message_bytes"]:
            errors.append(f"Message exceeds encoded limit: {path.name}")
        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                data = part.get_payload(decode=True)
                digest = sha256(data).hexdigest()
                match = known_hashes.get(digest)
                detail = {"filename": part.get_filename(), "bytes": len(data), "hash": digest, "matches_archived_original": bool(match)}
                bundle_valid = False
                if part.get_content_type() == "application/zip":
                    with ZipFile(BytesIO(data)) as bundle:
                        members = [{"name": name, "hash": sha256(bundle.read(name)).hexdigest()} for name in bundle.namelist()]
                        bundle_valid = bool(members) and all(m["hash"] in known_hashes or m["hash"] in asset_hashes for m in members)
                        detail.update({"bundle_members": members, "all_members_match_archived_sources": bundle_valid})
                        if any(m["hash"] not in own_hashes | own_assets for m in members):
                            errors.append(f"ZIP includes another company/event: {part.get_filename()}")
                elif digest not in own_hashes:
                    errors.append(f"Attachment is outside this company/event: {part.get_filename()}")
                item["attachments"].append(detail)
                if not match and not bundle_valid:
                    errors.append(f"Attachment not matched to archive: {part.get_filename()}")
            elif part.get_content_type() == "text/plain":
                item["has_plain"] = True
                if report["text"].replace("\r\n", "\n").strip() not in part.get_content().replace("\r\n", "\n"):
                    errors.append(f"Plain text does not contain the current draft: {path.name}")
            elif part.get_content_type() == "text/html":
                item["has_html"] = True
        if not item["has_plain"] or not item["has_html"]:
            errors.append(f"Missing multipart alternative: {path.name}")
        messages.append(item)
    if not messages:
        errors.append("No EML previews found")
    raw_evidence = json.loads((report_dir / "evidence.json").read_text(encoding="utf-8"))
    evidence = raw_evidence if isinstance(raw_evidence, list) else raw_evidence.get("facts", [])
    checked = 0
    evidence_by_id = {f["id"]: f for f in evidence if isinstance(f, dict) and "id" in f}
    chart = report.get("chart")
    if chart:
        for bar in chart["bars"]:
            fact = evidence_by_id.get(bar["evidence_id"])
            if not fact or str(fact["value"]) != bar["raw_value"] or fact["unit"] != bar["unit"]:
                errors.append("Chart column is not tied to its exact source evidence")
            expected_height = int(abs(Decimal(bar["raw_value"])) / Decimal(chart["scale_max"]) * chart["scale_height_px"])
            if expected_height != bar["height"] or bar["negative"] != (Decimal(bar["raw_value"]) < 0):
                errors.append("Chart scale or sign is incorrect")
        if report["html"].index('class="earnings-chart"') > report["html"].index('class="financial-table figures"'):
            errors.append("Chart appears below the financial table")
        table = BeautifulSoup(report["html"], "html.parser").select_one("table.figures")
        headers = [cell.get_text(strip=True) for cell in table.select("thead th")][1:]
        first_values = [cell.get_text(strip=True) for cell in table.select("tbody tr")[0].find_all("td")][1:]
        if [h for h in headers if h in {bar["period"] for bar in chart["bars"]}] != [bar["period"] for bar in chart["bars"]]:
            errors.append("Chart and table do not share chronological column order")
        for bar in chart["bars"]:
            if first_values[headers.index(bar["period"])] != bar["amount"]:
                errors.append("Table value is under the wrong quarter header")
    for entry in report.get("reviewed_context", []):
        for proof in entry["sources"]:
            doc = known_ids.get(proof["document_id"])
            if not doc or doc.content_hash != proof["content_hash"] or doc.period != proof["period"] or doc.cik not in report_ciks:
                errors.append("Reviewed context is not tied to the exact issuer/source version")
    for fact in evidence:
        if not isinstance(fact, dict) or not fact.get("value"):
            continue
        value = Decimal(str(fact["value"]))
        if not value.is_finite():
            errors.append(f"Nonfinite value: {fact.get('id')}")
        if fact.get("document_id") not in known_ids:
            errors.append(f"Unknown evidence document: {fact.get('id')}")
        elif known_ids[fact["document_id"]].cik not in report_ciks:
            errors.append(f"Evidence is from another company: {fact.get('id')}")
        for required in ("unit", "period", "scope", "definition", "location", "excerpt", "source_url"):
            if not fact.get(required):
                errors.append(f"Evidence missing {required}: {fact.get('id')}")
        checked += 1
    result = {"status": "passed" if not errors else "failed", "archive_documents": len(archived), "numerical_evidence_checked": checked,
              "messages": messages, "archived": archived, "errors": errors,
              "limits": "Mechanical integrity/provenance audit only; financial interpretation requires original-source review. No network or email delivery performed."}
    (report_dir / "artifact-audit.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("report_dir", type=Path)
    args = parser.parse_args()
    result = audit(load_config(args.config), args.report_dir)
    print(json.dumps({key: result[key] for key in ("status", "archive_documents", "numerical_evidence_checked", "errors")}, indent=2))
    sys.exit(0 if result["status"] == "passed" else 1)
