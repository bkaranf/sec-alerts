from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess

from servicing_brief.scheduling import due, mark_handled, schedule_status, scheduled_run


def _config(**overrides) -> dict:
    schedule = {"timezone": "America/New_York", "times": ["07:00", "18:00"], "catch_up": True}
    schedule.update(overrides)
    return {"schedule": schedule}


def test_due_uses_new_york_dst_and_successful_slots_only(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    # After spring-forward, both standard slots use EDT (-04:00).
    now = datetime(2026, 3, 9, 19, 30, tzinfo=timezone.utc)  # 15:30 New York
    slots = due(_config(), state, now=now)

    assert len(slots) == 1  # newest pending slot by default
    assert slots[0]["local_time"] == "07:00"
    assert slots[0]["scheduled_local"].endswith("-04:00")
    assert mark_handled(state, slots[0], success=False) is False
    assert due(_config(), state, now=now)[0]["run_key"] == slots[0]["run_key"]
    assert mark_handled(state, slots[0], success=True) is True
    assert due(_config(), state, now=now) == []


def test_due_catches_up_newest_missed_slot_without_historical_flood(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)  # 19:30 New York
    slots = due(_config(), state, now=now)

    assert len(slots) == 1
    assert slots[0]["local_time"] == "18:00"
    assert slots[0]["catch_up"] is True


def test_scheduled_run_marks_only_success_and_prevents_overlap(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    calls = []

    def failed(_slot):
        calls.append("failed")
        return {"status": "delivery_incomplete"}

    first = scheduled_run(_config(), state, failed, now=now)
    assert first["status"] == "delivery_incomplete"
    assert calls == ["failed"]
    assert due(_config(), state, now=now)

    def succeeded(_slot):
        calls.append("succeeded")
        return {"status": "provider_accepted"}

    second = scheduled_run(_config(), state, succeeded, now=now)
    assert second["status"] == "provider_accepted"
    assert due(_config(), state, now=now) == []

    # Hold the same lock from a second execution to exercise overlap behavior.
    from servicing_brief.scheduling import FileLock, _schedule_lock_path

    with FileLock(str(_schedule_lock_path(state))):
        overlap = scheduled_run(_config(), state, succeeded, now=now)
    assert overlap["status"] == "overlap_skipped"


def test_schedule_status_reports_local_timezone_and_last_success(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    slot = due(_config(), state, now=now)[0]
    mark_handled(state, slot, success=True, detail="provider_accepted")

    status = schedule_status(_config(), state)
    assert status["timezone"] == "America/New_York"
    assert status["last_success"]["run_key"] == slot["run_key"]


def test_partial_source_failure_remains_due_after_useful_report(tmp_path: Path) -> None:
    state = tmp_path / "state.sqlite"
    now = datetime(2026, 9, 4, 23, 30, tzinfo=timezone.utc)
    result = scheduled_run(_config(), state, lambda: {"status": "prepared", "sources_failed": [{"source": "ir", "blocked": True}]}, now=now)
    assert result["successful"] is False
    assert due(_config(), state, now=now)


def test_windows_installer_whatif_validates_without_registering_task(tmp_path: Path) -> None:
    pwsh = shutil.which("pwsh")
    if not pwsh:
        import pytest

        pytest.skip("PowerShell 7 is unavailable")
    config = tmp_path / "config.toml"
    config.write_text(
        """
[storage]
path = "data"
[email]
sender = "sender@example.com"
recipient = "owner@example.com"
smtp_username = "sender@example.com"
smtp_password_env = "TEST_SCHEDULER_PASSWORD"
[schedule]
enabled = true
timezone = "America/New_York"
times = ["07:00", "18:00"]
""",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        import pytest

        pytest.skip("project virtualenv is unavailable")
    env = os.environ.copy()
    env["SMTP_USERNAME"] = "sender@example.com"
    env["TEST_SCHEDULER_PASSWORD"] = "app-password"
    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-File",
            str(root / "scripts" / "install-schedule.ps1"),
            "-ConfigPath",
            str(config),
            "-PythonExe",
            str(python),
            "-WorkingDirectory",
            str(root),
            "-EnableSending",
            "-WhatIf",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "What if:" in result.stdout
