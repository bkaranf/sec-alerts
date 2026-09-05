"""Apply a reviewed local metadata correction without altering source bytes."""
import json
from hashlib import sha256
from pathlib import Path
from filelock import FileLock
from servicing_brief.state import State
from servicing_brief.models import utcnow


def apply(manifest_path=Path("data/all-ir-metadata-repair.json")):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    state_path = Path(manifest["state"]["path"])
    audit = {"manifest": str(manifest_path), "applied_at": utcnow(), "changes": []}
    with FileLock(str(state_path.parent / "run.lock"), timeout=0):
        state = State(state_path)
        try:
            by_id = {d.id: d for d in state.documents()}
            changed = []
            for record in manifest["records"]:
                doc = by_id[record["id"]]
                if doc.source != "ir" or doc.content_hash != record["content_hash"] or sha256(Path(doc.path).read_bytes()).hexdigest() != doc.content_hash:
                    raise ValueError("Source integrity failed; no repair applied")
                before = doc.to_dict()
                update = record["new_metadata"]
                for key in ("kind", "period", "title", "published"):
                    setattr(doc, key, update[key])
                for key in ("publication_date_source", "period_basis", "http_last_modified", "http_date", "exclude_from_reporting", "exclusion_reason", "kind_basis"):
                    if key in update:
                        doc.metadata[key] = update[key]
                if Path(doc.path).suffix.lower() == ".pdf" and "UTF-8" in str(doc.metadata.get("byte_fidelity", "")):
                    doc.metadata["legacy_byte_fidelity"] = {k: doc.metadata.pop(k) for k in ("original_bytes", "byte_fidelity") if k in doc.metadata}
                    doc.metadata["byte_fidelity"] = "Archived binary PDF; local hash verified. Original-download fidelity was not reverified by this repair."
                doc.metadata["metadata_repair"] = str(manifest_path)
                changed.append(doc)
                audit["changes"].append({"id": doc.id, "before": before, "after": doc.to_dict()})
            with state.db:
                for doc in changed:
                    state.db.execute("UPDATE documents SET period=?,payload=? WHERE id=?", (doc.period, json.dumps(doc.to_dict()), doc.id))
        finally:
            state.close()
    Path("data/applied-ir-metadata-repair.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return {"repaired": len(changed), "source_bytes_changed": False, "audit": "data/applied-ir-metadata-repair.json"}


if __name__ == "__main__":
    print(json.dumps(apply(), indent=2))
