"""Offline regression coverage for explicit storage path relocation."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import json
import shutil
import sqlite3

import pytest
from filelock import FileLock

from servicing_brief import cli, delivery
from servicing_brief.models import Document, SourceResult
from servicing_brief.path_repair import repair_paths
from servicing_brief.state import State


def _digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def _report(body: str) -> dict:
    html = (
        "<html><body data-brief-company='TFC' data-brief-cik='0000092230' "
        "data-brief-event='2026-Q2'><p>" + body + "</p></body></html>"
    )
    text = "Company: Truist Financial Corporation (TFC)\nEarnings period: Q2 2026\n\n" + body
    return {
        "subject": "TFC | Q2 2026 earnings brief",
        "text": text,
        "html": html,
        "company_identity": {
            "ticker": "TFC",
            "cik": "0000092230",
            "event": "2026-Q2",
            "kind": "earnings_brief",
        },
        "company_reports": {"TFC": {"html": html, "text": text}},
        "generated_at": "2026-08-02T12:00:00+00:00",
    }


def _document(old_root: Path) -> tuple[Document, bytes, bytes]:
    archive = old_root / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    wrapper = b'<html><body><img src="slide.jpg"></body></html>'
    image = b"verified slide image"
    wrapper_path = archive / "downloaded-wrapper.htm"
    image_path = archive / "downloaded-image.jpg"
    wrapper_path.write_bytes(wrapper)
    image_path.write_bytes(image)
    document = Document(
        issuer="Truist Financial Corporation",
        cik="0000092230",
        title="Q2 earnings presentation",
        kind="presentation",
        source="sec",
        url="https://www.sec.gov/Archives/edgar/data/92230/000009223026000123/ex99-1.htm",
        published="2026-07-29",
        period="2026-Q2",
        path=str(wrapper_path),
        content_hash=_digest(wrapper),
        accession="0000092230-26-000123",
        retrieved="2026-08-01T12:00:00+00:00",
        discovered_from="https://www.sec.gov/Archives/edgar/data/92230/000009223026000123/0000092230-26-000123-index.html",
        metadata={
            "ticker": "TFC",
            "document": "ex99-1.htm",
            "assets": [{
                "document": "slide.jpg",
                "path": str(image_path),
                "content_hash": _digest(image),
            }],
        },
    )
    return document, wrapper, image


def _write_report_artifacts(directory: Path, report: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (directory / "briefing.html").write_text(report["html"], encoding="utf-8")
    (directory / "briefing.txt").write_text(report["text"], encoding="utf-8")


def _insert_delivery_row(connection: sqlite3.Connection, path: Path, status: str, attempts: int) -> str:
    key, package, subject, part, total, raw, _ = delivery._message_metadata(path)
    now = "2026-08-02T12:00:00+00:00"
    connection.execute(
        """
        INSERT INTO delivery_messages(
            message_key, package_key, path, content_hash, subject,
            part_number, total_parts, status, attempts, provider_response,
            last_error, created_at, updated_at, accepted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            key,
            package,
            str(path),
            _digest(raw),
            subject,
            part,
            total,
            status,
            attempts,
            "provider accepted" if status == "accepted" else "",
            "provider response uncertain" if status == "ambiguous" else "",
            now,
            now,
            now if status == "accepted" else "",
        ),
    )
    connection.execute(
        "INSERT INTO delivery_attempts(message_key,attempt,outcome,detail,created_at) VALUES(?,?,?,?,?)",
        (key, attempts, status, "fixture attempt", now),
    )
    return key


@pytest.fixture
def relocated_workspace(tmp_path: Path) -> dict:
    """Create a state DB whose pointers reference a moved old workspace."""

    old_root = tmp_path / "old-workspace"
    new_root = tmp_path / "new-workspace"
    state_db = tmp_path / "state.sqlite3"
    old_root.mkdir()
    document, wrapper, image = _document(old_root)
    state = State(state_db)
    result = SourceResult(
        documents=[document],
        checkpoints={"TFC:sec": "2026-08-02T12:00:00+00:00"},
        checked=["TFC"],
    )
    state.ingest(result)
    admitted = result.documents[0]
    reports: dict[str, dict] = {}
    delivery_paths: dict[str, Path] = {}
    for report_id, status, body in (
        ("accepted-report", "accepted", "Accepted historical brief"),
        ("ambiguous-report", "ambiguous", "Ambiguous historical brief"),
    ):
        report = _report(body)
        reports[report_id] = report
        directory = old_root / "reports" / report_id
        _write_report_artifacts(directory, report)
        paths = delivery.prepare_messages(
            {"email": {"from_address": "sender@example.com", "recipient": "owner@example.com"}},
            report,
            [admitted],
            directory,
        )
        assert len(paths) == 1
        delivery_paths[report_id] = paths[0]
        state.save_report(report_id, report, [admitted], directory, True)
        state.set_report_status(report_id, status)

    # State-only withdrawal metadata is appended after the report artifacts
    # were written. Repair must ignore that audit field while still requiring
    # the actual report.json and reader-facing files to match.
    withdrawn_report = _report("Withdrawn historical brief")
    reports["withdrawn-report"] = withdrawn_report
    withdrawn_directory = old_root / "reports" / "withdrawn-report"
    _write_report_artifacts(withdrawn_directory, withdrawn_report)
    state.save_report("withdrawn-report", withdrawn_report, [admitted], withdrawn_directory, True)
    state.withdraw_unsent_report("withdrawn-report", "fixture withdrawal")

    delivery._ensure_delivery_schema(state.db)
    delivery_keys = {
        report_id: _insert_delivery_row(
            state.db,
            path,
            "accepted" if report_id == "accepted-report" else "ambiguous",
            1,
        )
        for report_id, path in delivery_paths.items()
    }
    state.db.commit()
    state.close()

    # A real move leaves the durable database available while every referenced
    # archive/report/EML path is recreated below the new root.
    shutil.copytree(old_root, new_root)
    shutil.rmtree(old_root)
    return {
        "state_db": state_db,
        "old_root": old_root,
        "new_root": new_root,
        "document_id": admitted.id,
        "document": admitted,
        "wrapper": wrapper,
        "image": image,
        "reports": reports,
        "delivery_keys": delivery_keys,
    }


def _snapshot_database(state_db: Path) -> dict:
    connection = sqlite3.connect(state_db)
    try:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        schema = [tuple(row) for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        )]
        rows = {
            table: [tuple(row) for row in connection.execute(f"SELECT * FROM [{table}]")]
            for table in tables
        }
        return {"schema": schema, "rows": rows}
    finally:
        connection.close()


def _mapped(root: Path, relative: str) -> Path:
    return root / relative


def _write_cli_config(path: Path, storage: Path) -> None:
    """Write the smallest valid config needed by the repair command."""

    path.write_text(
        "[storage]\npath = " + json.dumps(storage.as_posix()) + "\n",
        encoding="utf-8",
    )


def _file_snapshot(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_inspection_is_read_only_and_reports_verified_changes(relocated_workspace):
    state_db = relocated_workspace["state_db"]
    before_bytes = state_db.read_bytes()
    before = _snapshot_database(state_db)

    result = repair_paths(
        state_db,
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=False,
    )

    assert result["status"] == "inspected"
    assert result["applied"] is False
    assert result["successful"] is True
    assert result["unresolved"] == []
    assert result["checked"] == {"documents": 1, "reports": 3, "delivery": 2}
    assert {item["kind"] for item in result["changes"]} == {"document", "report", "delivery"}
    assert sum(item["kind"] == "document" for item in result["changes"]) == 1
    assert sum(item["kind"] == "report" for item in result["changes"]) == 3
    assert sum(item["kind"] == "delivery" for item in result["changes"]) == 2
    assert state_db.read_bytes() == before_bytes
    assert _snapshot_database(state_db) == before
    assert not (state_db.parent / "maintenance").exists()


def test_apply_rebases_only_verified_pointers_and_keeps_history_and_payloads(relocated_workspace, monkeypatch):
    state_db = relocated_workspace["state_db"]
    before = _snapshot_database(state_db)
    with sqlite3.connect(state_db) as before_connection:
        before_connection.row_factory = sqlite3.Row
        before_document = dict(before_connection.execute("SELECT * FROM documents").fetchone())
        before_reports = {
            row["id"]: dict(row)
            for row in before_connection.execute("SELECT * FROM briefings ORDER BY id")
        }
        before_delivery = {
            row["message_key"]: dict(row)
            for row in before_connection.execute("SELECT * FROM delivery_messages ORDER BY message_key")
        }
        before_attempts = [tuple(row) for row in before_connection.execute(
            "SELECT message_key,attempt,outcome,detail,created_at FROM delivery_attempts ORDER BY id"
        )]
    before_files = {
        path.relative_to(relocated_workspace["new_root"]): path.read_bytes()
        for path in relocated_workspace["new_root"].rglob("*")
        if path.is_file()
    }
    monkeypatch.setattr(delivery.smtplib, "SMTP_SSL", lambda *args, **kwargs: pytest.fail("repair must not send"))
    monkeypatch.setattr(delivery.smtplib, "SMTP", lambda *args, **kwargs: pytest.fail("repair must not send"))

    result = repair_paths(
        state_db,
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=True,
    )

    assert result["status"] == "applied"
    assert result["applied"] is True
    assert result["successful"] is True
    assert result["unresolved"] == []
    assert Path(result["backup"]).is_file()
    assert Path(result["audit"]).is_file()
    audit = json.loads(Path(result["audit"]).read_text(encoding="utf-8"))
    assert audit["transaction"] == "committed"
    assert Path(audit["backup"]).is_file()
    assert _snapshot_database(Path(result["backup"])) == before
    with sqlite3.connect(result["backup"]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

    with sqlite3.connect(state_db) as connection:
        connection.row_factory = sqlite3.Row
        document_row = connection.execute("SELECT * FROM documents").fetchone()
        document_payload = json.loads(document_row["payload"])
        assert document_payload["id"] == relocated_workspace["document"].id
        assert document_payload["path"] == str(_mapped(relocated_workspace["new_root"], "archive/downloaded-wrapper.htm"))
        assert document_payload["metadata"]["assets"][0]["path"] == str(
            _mapped(relocated_workspace["new_root"], "archive/downloaded-image.jpg")
        )
        assert document_payload["content_hash"] == relocated_workspace["document"].content_hash
        assert document_payload["retrieved"] == relocated_workspace["document"].retrieved

        report_rows = connection.execute("SELECT * FROM briefings ORDER BY id").fetchall()
        assert {row["status"] for row in report_rows} == {"accepted", "ambiguous", "withdrawn"}
        for row in report_rows:
            assert row["output_dir"] == str(_mapped(relocated_workspace["new_root"], f"reports/{row['id']}"))
            assert row["payload"] == before_reports[row["id"]]["payload"]

        delivery_rows = connection.execute(
            "SELECT message_key,package_key,path,content_hash,subject,part_number,total_parts,status,attempts,provider_response,last_error,created_at,updated_at,accepted_at FROM delivery_messages ORDER BY message_key"
        ).fetchall()
        assert {row["status"] for row in delivery_rows} == {"accepted", "ambiguous"}
        for row in delivery_rows:
            report_id = next(
                report_id
                for report_id, key in relocated_workspace["delivery_keys"].items()
                if key == row["message_key"]
            )
            eml_name = next((relocated_workspace["new_root"] / "reports" / report_id).glob("*.eml")).name
            assert row["path"] == str(_mapped(relocated_workspace["new_root"], f"reports/{report_id}/{eml_name}"))
            prior = before_delivery[row["message_key"]]
            for column in before_delivery[row["message_key"]]:
                if column != "path":
                    assert row[column] == prior[column]

        attempts = [tuple(row) for row in connection.execute(
            "SELECT message_key,attempt,outcome,detail,created_at FROM delivery_attempts ORDER BY id"
        )]
        assert attempts == before_attempts

        assert document_row["id"] == before_document["id"]
        for column in before_document:
            if column not in {"payload"}:
                assert document_row[column] == before_document[column]
        old_payload = json.loads(before_document["payload"])
        for key, value in old_payload.items():
            if key not in {"path", "metadata"}:
                assert document_payload[key] == value
        old_assets = old_payload["metadata"]["assets"]
        new_assets = document_payload["metadata"]["assets"]
        assert len(old_assets) == len(new_assets)
        for old_asset, new_asset in zip(old_assets, new_assets):
            assert {key: value for key, value in new_asset.items() if key != "path"} == {
                key: value for key, value in old_asset.items() if key != "path"
            }
        for report_id, prior in before_reports.items():
            current = next(row for row in report_rows if row["id"] == report_id)
            for column in prior:
                if column != "output_dir":
                    assert current[column] == prior[column]

    # Files are never rewritten by the maintenance operation.
    after_files = {
        path.relative_to(relocated_workspace["new_root"]): path.read_bytes()
        for path in relocated_workspace["new_root"].rglob("*")
        if path.is_file()
    }
    assert after_files == before_files
    after = _snapshot_database(state_db)
    assert after["schema"] == before["schema"]
    for table in before["rows"]:
        if table not in {"documents", "briefings", "delivery_messages"}:
            assert after["rows"][table] == before["rows"][table]


def test_apply_is_idempotent_and_noop_creates_no_second_backup(relocated_workspace):
    first = repair_paths(
        relocated_workspace["state_db"],
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=True,
    )
    maintenance = Path(first["audit"]).parent.parent
    audit_dirs = sorted(maintenance.glob("path-repair-*"))
    state_after_first = relocated_workspace["state_db"].read_bytes()

    second = repair_paths(
        relocated_workspace["state_db"],
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=True,
    )

    assert second["status"] == "applied"
    assert second["applied"] is False
    assert second["successful"] is True
    assert second["changes"] == []
    assert second["unresolved"] == []
    assert relocated_workspace["state_db"].read_bytes() == state_after_first
    assert sorted(maintenance.glob("path-repair-*")) == audit_dirs


@pytest.mark.parametrize("lock_name", ["run.lock", "state.sqlite3.delivery.lock"])
def test_apply_skips_when_collection_or_delivery_lock_is_held(relocated_workspace, lock_name):
    state_db = relocated_workspace["state_db"]
    before = state_db.read_bytes()
    lock_path = state_db.parent / lock_name if lock_name == "run.lock" else Path(str(state_db) + ".delivery.lock")
    with FileLock(str(lock_path)):
        result = repair_paths(
            state_db,
            relocated_workspace["old_root"],
            relocated_workspace["new_root"],
            apply=True,
        )
    assert result["status"] == "overlap_skipped"
    assert result["successful"] is False
    assert result["applied"] is False
    assert state_db.read_bytes() == before


def test_corrupt_image_report_and_message_are_refused_while_safe_rows_apply(relocated_workspace):
    new_root = relocated_workspace["new_root"]
    # The image is part of the document's verified HTML bundle.
    (new_root / "archive" / "downloaded-image.jpg").write_bytes(b"corrupt image bytes")
    # A changed report JSON must never be used to authorize a relocated output.
    report_json = new_root / "reports" / "accepted-report" / "report.json"
    tampered_report = json.loads(report_json.read_text(encoding="utf-8"))
    tampered_report["text"] = "tampered report payload"
    report_json.write_text(json.dumps(tampered_report, indent=2), encoding="utf-8")
    # Keep the MIME message parseable but make its raw digest differ from the
    # durable content hash, so the accepted row cannot be moved.
    accepted_eml = next((new_root / "reports" / "accepted-report").glob("*.eml"))
    accepted_eml.write_bytes(accepted_eml.read_bytes() + b"\n tampered")

    result = repair_paths(
        relocated_workspace["state_db"],
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=True,
    )

    assert result["status"] == "incomplete"
    assert result["successful"] is False
    assert result["applied"] is True
    unresolved = {(item["kind"], item["id"]) for item in result["unresolved"]}
    assert ("document", relocated_workspace["document_id"]) in unresolved
    assert ("report", "accepted-report") in unresolved
    assert ("delivery", relocated_workspace["delivery_keys"]["accepted-report"]) in unresolved
    assert all(item["kind"] in {"document", "report", "delivery"} for item in result["unresolved"])

    with sqlite3.connect(relocated_workspace["state_db"]) as connection:
        connection.row_factory = sqlite3.Row
        document_payload = json.loads(connection.execute("SELECT payload FROM documents").fetchone()[0])
        assert document_payload["path"] == str(new_root / "archive" / "downloaded-wrapper.htm")
        assert document_payload["metadata"]["assets"][0]["path"] == str(
            relocated_workspace["old_root"] / "archive" / "downloaded-image.jpg"
        )
        accepted_report = connection.execute(
            "SELECT output_dir FROM briefings WHERE id='accepted-report'"
        ).fetchone()[0]
        assert accepted_report.startswith(str(relocated_workspace["old_root"]))
        accepted_delivery = connection.execute(
            "SELECT path,status,attempts FROM delivery_messages WHERE message_key=?",
            (relocated_workspace["delivery_keys"]["accepted-report"],),
        ).fetchone()
        assert accepted_delivery["path"].startswith(str(relocated_workspace["old_root"]))
        assert accepted_delivery["status"] == "accepted"
        assert accepted_delivery["attempts"] == 1

        # The unmodified ambiguous report and its message are safe to move in
        # the same partial apply, even though the overall result is incomplete.
        assert connection.execute(
            "SELECT output_dir FROM briefings WHERE id='ambiguous-report'"
        ).fetchone()[0] == str(new_root / "reports" / "ambiguous-report")
        assert connection.execute(
            "SELECT path FROM delivery_messages WHERE message_key=?",
            (relocated_workspace["delivery_keys"]["ambiguous-report"],),
        ).fetchone()[0].startswith(str(new_root))


def test_missing_target_is_reported_without_schema_or_state_writes(relocated_workspace):
    missing = relocated_workspace["new_root"] / "reports" / "ambiguous-report"
    shutil.rmtree(missing)
    before = _snapshot_database(relocated_workspace["state_db"])
    before_bytes = relocated_workspace["state_db"].read_bytes()

    result = repair_paths(
        relocated_workspace["state_db"],
        relocated_workspace["old_root"],
        relocated_workspace["new_root"],
        apply=False,
    )

    assert result["status"] == "incomplete"
    assert result["applied"] is False
    assert result["successful"] is False
    assert any(item["kind"] == "report" and item["id"] == "ambiguous-report" for item in result["unresolved"])
    assert any(item["kind"] == "delivery" and item["id"] == relocated_workspace["delivery_keys"]["ambiguous-report"] for item in result["unresolved"])
    assert relocated_workspace["state_db"].read_bytes() == before_bytes
    assert _snapshot_database(relocated_workspace["state_db"]) == before


def test_report_artifact_symlink_escape_is_unresolved(relocated_workspace, monkeypatch):
    """A byte-identical report outside the destination root cannot authorize a move."""

    new_root = relocated_workspace["new_root"]
    report_json = new_root / "reports" / "accepted-report" / "report.json"
    original_bytes = report_json.read_bytes()
    outside = relocated_workspace["state_db"].parent / "outside-report.json"
    outside.write_bytes(original_bytes)
    report_json.unlink()
    try:
        report_json.symlink_to(outside)
    except (OSError, NotImplementedError):
        # Windows hosts without symlink privileges still exercise the same
        # containment branch by making resolve() report the escaped target.
        report_json.write_bytes(original_bytes)
        real_resolve = Path.resolve

        def escaped_resolve(path, *args, **kwargs):
            if path == report_json:
                return real_resolve(outside, *args, **kwargs)
            return real_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", escaped_resolve)

    result = repair_paths(
        relocated_workspace["state_db"],
        relocated_workspace["old_root"],
        new_root,
        apply=False,
    )

    assert result["status"] == "incomplete"
    assert result["applied"] is False
    assert result["successful"] is False
    assert any(
        item["kind"] == "report" and item["id"] == "accepted-report"
        for item in result["unresolved"]
    )


def test_cli_repair_paths_inspection_emits_summary_without_writes(relocated_workspace, tmp_path, capsys):
    storage = tmp_path / "cli-storage"
    storage.mkdir()
    state_db = storage / "state.sqlite3"
    shutil.copy2(relocated_workspace["state_db"], state_db)
    config_path = tmp_path / "repair-config.toml"
    _write_cli_config(config_path, storage)
    before_database = state_db.read_bytes()
    before_snapshot = _snapshot_database(state_db)
    before_storage_files = _file_snapshot(storage)
    before_workspace_files = _file_snapshot(relocated_workspace["new_root"])

    code = cli.main([
        "--config", str(config_path),
        "repair-paths",
        "--from-root", str(relocated_workspace["old_root"]),
        "--to-root", str(relocated_workspace["new_root"]),
    ])

    captured = capsys.readouterr()
    assert code == 0
    summary = json.loads(captured.out)
    assert summary["status"] == "inspected"
    assert summary["applied"] is False
    assert summary["successful"] is True
    assert state_db.read_bytes() == before_database
    assert _snapshot_database(state_db) == before_snapshot
    assert _file_snapshot(storage) == before_storage_files
    assert _file_snapshot(relocated_workspace["new_root"]) == before_workspace_files
    assert not (storage / "brief.log").exists()
    assert not (storage / "maintenance").exists()


def test_cli_repair_paths_missing_state_does_not_create_database(tmp_path, capsys):
    storage = tmp_path / "empty-storage"
    storage.mkdir()
    config_path = tmp_path / "repair-config.toml"
    _write_cli_config(config_path, storage)
    old_root = tmp_path / "old-workspace"
    new_root = tmp_path / "new-workspace"
    old_root.mkdir()
    new_root.mkdir()
    state_db = storage / "state.sqlite3"
    assert not state_db.exists()

    code = cli.main([
        "--config", str(config_path),
        "repair-paths",
        "--from-root", str(old_root),
        "--to-root", str(new_root),
    ])

    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.err)["status"] == "failed"
    assert not state_db.exists()
    assert not (storage / "brief.log").exists()
