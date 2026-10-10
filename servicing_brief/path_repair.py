"""Inspect or repair verified storage pointers after an explicit directory move.

Original files and report payloads are never rewritten. Inspection uses a
read-only connection and does not initialize State or logging.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from filelock import FileLock, Timeout

from .archive_paths import ArchivePathMap
from .models import utcnow


def _connect(path, *, readonly=False):
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"), uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _report_matches(directory, report, *, withdrawn=False, destination_root=None):
    """Match an existing artifact package without generating or approving it."""
    try:
        if destination_root is not None:
            # Checking only the directory is insufficient when one artifact
            # is a symlink or Windows reparse point outside the mapped root.
            for name in ("report.json", "briefing.html", "briefing.txt"):
                (directory / name).resolve().relative_to(destination_root)
        saved = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        expected = dict(report)
        if withdrawn:
            # State adds this audit metadata after the unsent files are written.
            expected.pop("withdrawal", None)
        return (saved == expected
                and (directory / "briefing.html").read_text(encoding="utf-8") == report["html"]
                and (directory / "briefing.txt").read_text(encoding="utf-8") == report["text"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def _plan(connection, mapping):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    changes, unresolved, operations = [], [], []
    checked = {"documents": 0, "reports": 0, "delivery": 0}
    if "documents" in tables:
        for row in connection.execute("SELECT id,payload FROM documents ORDER BY id"):
            checked["documents"] += 1
            try:
                restored, resolutions = mapping.document(json.loads(row["payload"]))
            except (ValueError, TypeError, KeyError):
                unresolved.append({"kind": "document", "id": row["id"], "reason": "invalid_record"})
                continue
            for resolution in resolutions:
                if resolution["status"] == "unresolved":
                    unresolved.append({"kind": "document", "id": row["id"], **resolution})
            mapped = [item for item in resolutions if item["status"] == "mapped"]
            if mapped:
                changes.append({"kind": "document", "id": row["id"], "paths": mapped})
                operations.append(("UPDATE documents SET payload=? WHERE id=?", (json.dumps(restored), row["id"])))

    if "briefings" in tables:
        for row in connection.execute("SELECT id,status,output_dir,payload FROM briefings ORDER BY id"):
            checked["reports"] += 1
            try:
                report = json.loads(row["payload"])
                original = Path(row["output_dir"])
                withdrawn = row["status"] == "withdrawn"
                if _report_matches(original, report, withdrawn=withdrawn):
                    continue
                candidate = mapping.candidate(row["output_dir"])
                if candidate is not None and _report_matches(candidate, report, withdrawn=withdrawn, destination_root=mapping.new_root):
                    changes.append({"kind": "report", "id": row["id"], "original_path": row["output_dir"], "path": str(candidate)})
                    operations.append(("UPDATE briefings SET output_dir=? WHERE id=?", (str(candidate), row["id"])))
                else:
                    unresolved.append({"kind": "report", "id": row["id"], "reason": "report_artifacts_unverified"})
            except (ValueError, TypeError, KeyError, OSError):
                unresolved.append({"kind": "report", "id": row["id"], "reason": "invalid_report_record"})

    if "delivery_messages" in tables:
        from .delivery import _message_metadata
        for row in connection.execute("SELECT message_key,package_key,path,content_hash,part_number,total_parts FROM delivery_messages ORDER BY message_key"):
            checked["delivery"] += 1
            resolution = mapping.resolve(row["path"], row["content_hash"])
            if resolution["status"] == "unresolved":
                unresolved.append({"kind": "delivery", "id": row["message_key"], **resolution})
            elif resolution["status"] == "mapped":
                try:
                    key, package, _, number, total, _, _ = _message_metadata(Path(resolution["path"]))
                    if (key, package, number, total) != (row["message_key"], row["package_key"], row["part_number"], row["total_parts"]):
                        raise ValueError("Message identity mismatch")
                except (OSError, ValueError, TypeError):
                    unresolved.append({"kind": "delivery", "id": row["message_key"], "reason": "message_identity_mismatch"})
                    continue
                changes.append({"kind": "delivery", "id": row["message_key"], **resolution})
                operations.append(("UPDATE delivery_messages SET path=? WHERE message_key=?", (resolution["path"], row["message_key"])))
    return {"checked": checked, "changes": changes, "unresolved": unresolved}, operations


def repair_paths(state_db: Path, old_root: Path, new_root: Path, *, apply=False) -> dict:
    """Rebase verified references only; default inspection never writes files."""
    state_db = Path(state_db).resolve()
    if not state_db.is_file():
        raise ValueError("State database does not exist")
    mapping = ArchivePathMap(old_root, new_root)
    if not apply:
        connection = _connect(state_db, readonly=True)
        try:
            connection.execute("BEGIN")
            plan, _ = _plan(connection, mapping)
        finally:
            connection.close()
        return {"status": "incomplete" if plan["unresolved"] else "inspected", "applied": False,
                "successful": not plan["unresolved"], "state": str(state_db), **plan}

    # Same lock order as the normal pipeline. No schedule or provider status
    # changes accompany a filesystem-pointer repair.
    try:
        with FileLock(str(state_db.parent / "run.lock"), timeout=0), FileLock(str(state_db) + ".delivery.lock", timeout=0):
            connection = _connect(state_db)
            try:
                connection.execute("BEGIN IMMEDIATE")
                plan, operations = _plan(connection, mapping)
                result = {"status": "incomplete" if plan["unresolved"] else "applied", "applied": bool(operations),
                          "successful": not plan["unresolved"], "state": str(state_db), **plan}
                if not operations:
                    connection.rollback()
                    return result
                # The reserved write lock prevents changes while a separate
                # read connection takes a consistent backup of the old state.
                audit_dir = state_db.parent / "maintenance" / ("path-repair-" + uuid4().hex)
                audit_dir.mkdir(parents=True)
                backup_path = audit_dir / "state-before.sqlite3"
                before = _connect(state_db, readonly=True)
                backup = sqlite3.connect(backup_path)
                try:
                    before.backup(backup)
                finally:
                    backup.close()
                    before.close()
                audit_path = audit_dir / "audit.json"
                audit = {"recorded": utcnow(), "from_root": str(old_root), "to_root": str(new_root),
                         **result, "backup": str(backup_path), "transaction": "prepared"}
                audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
                for sql, parameters in operations:
                    connection.execute(sql, parameters)
                connection.commit()
                result.update({"backup": str(backup_path), "audit": str(audit_path)})
                audit["transaction"] = "committed"
                try:
                    audit_path.write_text(json.dumps(audit, indent=2), encoding="utf-8")
                except OSError:
                    result.update({"successful": False, "audit_warning": "State changes committed; audit finalization failed."})
                return result
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
    except Timeout:
        return {"status": "overlap_skipped", "successful": False, "applied": False,
                "detail": "Collection or delivery is active; no paths were repaired."}
