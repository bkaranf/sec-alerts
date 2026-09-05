"""Timezone-aware due gating and Windows scheduler integration helpers.

Task Scheduler invokes the application on a frequent, machine-local trigger;
the Python gate below decides whether the configured America/New_York local
slot is due.  This keeps daylight-saving behavior correct even when the
computer's Windows timezone differs from the reporting timezone.
"""

from __future__ import annotations

import inspect
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from filelock import FileLock, Timeout as FileLockTimeout
except ImportError:  # pragma: no cover - dependency is declared by the project
    FileLock = None  # type: ignore[assignment,misc]


DEFAULT_TIMEZONE = "America/New_York"
DEFAULT_TIMES = ("07:00", "18:00")
DEFAULT_CATCH_UP_DAYS = 2
DEFAULT_MAX_PENDING = 1


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("schedule", {})
    return value if isinstance(value, Mapping) else {}


def _zone(config: Mapping[str, Any]) -> ZoneInfo:
    settings = _settings(config)
    name = str(settings.get("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown schedule timezone: {name}") from exc


def _parse_time(value: Any) -> time | None:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    text = str(value or "").strip()
    try:
        parts = text.split(":")
        if len(parts) not in {2, 3}:
            return None
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            return None
        return time(hour, minute, second)
    except (TypeError, ValueError):
        return None


def configured_times(config: Mapping[str, Any]) -> list[time]:
    """Return valid sorted local schedule times from the plain TOML config."""

    settings = _settings(config)
    raw = settings.get("times")
    if raw is None:
        raw = [settings.get("morning", "07:00"), settings.get("evening", "18:00")]
    if isinstance(raw, str):
        raw_values: Sequence[Any] = [item.strip() for item in raw.replace(";", ",").split(",")]
    elif isinstance(raw, Sequence):
        raw_values = raw
    else:
        raw_values = DEFAULT_TIMES
    parsed = [item for value in raw_values if (item := _parse_time(value)) is not None]
    unique = sorted({item.isoformat() for item in parsed})
    return [time.fromisoformat(item) for item in unique]


def _as_local(now: datetime | None, zone: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).astimezone(zone)
    if now.tzinfo is None:
        return now.replace(tzinfo=zone)
    return now.astimezone(zone)


def _occurrence(day: date, local_time: time, zone: ZoneInfo) -> datetime | None:
    """Build a valid aware local occurrence, handling DST folds and gaps.

    A 07:00/18:00 schedule is never ambiguous in New York, but configurable
    times can fall in a repeated or skipped hour.  Repeated times choose the
    first fold; skipped times are omitted instead of silently shifting by an
    hour.
    """

    naive = datetime.combine(day, local_time)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.astimezone(timezone.utc))


def _ensure_schedule_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schedule_runs (
            run_key TEXT PRIMARY KEY,
            timezone TEXT NOT NULL,
            scheduled_local TEXT NOT NULL,
            scheduled_utc TEXT NOT NULL,
            handled_at TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_schedule_runs_scheduled_utc
            ON schedule_runs(scheduled_utc);
        """
    )
    connection.commit()


def _parse_iso(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _slot_key(occurrence: datetime, configured_time: time, zone: ZoneInfo) -> str:
    # The UTC value makes a repeated-hour fold unambiguous while local fields
    # keep the row understandable during troubleshooting.
    utc = occurrence.astimezone(timezone.utc)
    return f"{zone.key}|{occurrence.date().isoformat()}|{configured_time.isoformat()}|{utc.isoformat()}"


def due(config: dict, state_db: Path, now: datetime | None = None) -> list[dict]:
    """Return unhandled schedule slots up to ``now``.

    The returned dictionaries are stable occurrence records suitable for
    passing to :func:`mark_handled`.  By default at most the newest pending
    slot is returned, so a computer that was offline for days runs one catch-up
    check instead of sending a historical flood.  Set
    ``schedule.max_pending`` to process more missed slots intentionally.
    """

    settings = _settings(config)
    if settings.get("enabled") is False:
        return []
    zone = _zone(config)
    times = configured_times(config)
    if not times:
        return []
    now_local = _as_local(now, zone)
    catch_up = bool(settings.get("catch_up", True))
    try:
        catch_up_days = max(0, min(31, int(settings.get("catch_up_days", DEFAULT_CATCH_UP_DAYS))))
    except (TypeError, ValueError):
        catch_up_days = DEFAULT_CATCH_UP_DAYS
    try:
        max_pending = max(1, min(32, int(settings.get("max_pending", DEFAULT_MAX_PENDING))))
    except (TypeError, ValueError):
        max_pending = DEFAULT_MAX_PENDING
    state_db = Path(state_db)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_schedule_schema(connection)
        latest_row = connection.execute(
            "SELECT scheduled_utc FROM schedule_runs ORDER BY scheduled_utc DESC LIMIT 1"
        ).fetchone()
        latest_success = _parse_iso(latest_row[0]) if latest_row else None
        if catch_up and latest_success is not None:
            lower_local = latest_success.astimezone(zone) - timedelta(minutes=1)
            earliest_day = max(
                now_local.date() - timedelta(days=catch_up_days),
                lower_local.date(),
            )
        elif catch_up:
            # First use starts today, avoiding a historical replay.
            earliest_day = now_local.date() - timedelta(days=min(catch_up_days, 1))
        else:
            earliest_day = now_local.date()
        if not catch_up:
            max_pending = 1
        candidates: list[dict] = []
        day = earliest_day
        while day <= now_local.date():
            for configured_time in times:
                occurrence = _occurrence(day, configured_time, zone)
                if occurrence is None or occurrence > now_local:
                    continue
                if latest_success is not None and occurrence.astimezone(timezone.utc) <= latest_success:
                    continue
                key = _slot_key(occurrence, configured_time, zone)
                exists = connection.execute(
                    "SELECT 1 FROM schedule_runs WHERE run_key = ?",
                    (key,),
                ).fetchone()
                if exists:
                    continue
                candidates.append(
                    {
                        "run_key": key,
                        "timezone": zone.key,
                        "scheduled_local": occurrence.isoformat(),
                        "scheduled_utc": occurrence.astimezone(timezone.utc).isoformat(),
                        "local_date": occurrence.date().isoformat(),
                        "local_time": configured_time.strftime("%H:%M"),
                        "catch_up": occurrence.date() < now_local.date() or occurrence.time() < now_local.time(),
                    }
                )
            day += timedelta(days=1)
        candidates.sort(key=lambda item: item["scheduled_utc"])
        return candidates[-max_pending:]
    finally:
        connection.close()


def mark_handled(
    state_db: Path,
    occurrence: Mapping[str, Any] | str,
    success: bool = True,
    detail: str = "",
) -> bool:
    """Persist a schedule occurrence only when the run completed successfully.

    A false ``success`` is intentionally a no-op, leaving the slot due for a
    later catch-up attempt.  This is the key distinction between execution
    bookkeeping and a successful scheduled run.
    """

    if not success:
        return False
    if isinstance(occurrence, Mapping):
        run_key = str(occurrence.get("run_key", "") or "")
        zone_name = str(occurrence.get("timezone", DEFAULT_TIMEZONE) or DEFAULT_TIMEZONE)
        scheduled_local = str(occurrence.get("scheduled_local", "") or "")
        scheduled_utc = str(occurrence.get("scheduled_utc", "") or "")
    else:
        run_key = str(occurrence)
        zone_name = DEFAULT_TIMEZONE
        scheduled_local = ""
        scheduled_utc = ""
    if not run_key:
        return False
    now = datetime.now(timezone.utc).isoformat()
    state_db = Path(state_db)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_schedule_schema(connection)
        connection.execute(
            """
            INSERT INTO schedule_runs(run_key, timezone, scheduled_local, scheduled_utc, handled_at, detail)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_key) DO UPDATE SET
                handled_at=excluded.handled_at,
                detail=excluded.detail
            """,
            (run_key, zone_name, scheduled_local, scheduled_utc, now, str(detail)[:2000]),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def record_success(state_db: Path, occurrence: Mapping[str, Any] | str, detail: str = "") -> bool:
    """Named alias for callers that want to make the success requirement explicit."""

    return mark_handled(state_db, occurrence, success=True, detail=detail)


def _schedule_lock_path(state_db: Path) -> Path:
    return Path(str(state_db) + ".schedule.lock")


@contextmanager
def execution_lock(config: Mapping[str, Any], state_db: Path) -> Iterator[bool]:
    """Acquire the cross-process scheduled execution lock without waiting."""

    state_db = Path(state_db)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    lock = None
    if FileLock is not None:
        settings = _settings(config)
        try:
            timeout = max(0.0, float(settings.get("lock_timeout_seconds", 0)))
        except (TypeError, ValueError):
            timeout = 0.0
        lock = FileLock(str(_schedule_lock_path(state_db)))
        try:
            lock.acquire(timeout=timeout)
        except FileLockTimeout:
            yield False
            return
    try:
        yield True
    finally:
        if lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def _callback_result(callback: Callable[..., Any], occurrence: Mapping[str, Any]) -> Any:
    """Call either a no-argument or occurrence-aware callback."""

    try:
        signature = inspect.signature(callback)
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
    except (TypeError, ValueError):
        positional = []
    if positional:
        return callback(occurrence)
    return callback()


def _successful_callback_result(result: Any) -> bool:
    if isinstance(result, bool):
        return result
    if isinstance(result, Mapping):
        if result.get("sources_failed"):
            return False
        status = str(result.get("status", "") or "").lower()
        return status in {"prepared", "no_new_disclosures", "provider_accepted", "already_accepted"}
    return False


def scheduled_run(
    config: dict,
    state_db: Path,
    callback: Callable[..., Any],
    now: datetime | None = None,
) -> dict:
    """Run the callback for due slots and mark only successful results.

    ``callback`` may accept one slot dictionary or no arguments.  Its result is
    returned under ``results``; mapping statuses used by the CLI are preserved.
    """

    if _settings(config).get("enabled") is False:
        return {"status": "disabled", "slots": [], "results": []}
    with execution_lock(config, state_db) as acquired:
        if not acquired:
            return {
                "status": "overlap_skipped",
                "slots": [],
                "results": [],
                "detail": "another scheduled execution currently owns the lock",
            }
        slots = due(config, state_db, now=now)
        if not slots:
            return {"status": "not_due", "slots": [], "results": []}
        results: list[Any] = []
        successful = True
        for slot in slots:
            result = _callback_result(callback, slot)
            results.append(result)
            if _successful_callback_result(result):
                mark_handled(state_db, slot, success=True, detail=str(result.get("status", "")) if isinstance(result, Mapping) else "")
            else:
                successful = False
                # Leave this and later slots unrecorded so a future invocation
                # can catch them up after the failed run is fixed.
                break
        first = results[0] if results else {}
        last = results[-1] if results else {}
        if not successful and isinstance(last, Mapping) and last.get("status"):
            status = str(last["status"])
        elif isinstance(first, Mapping) and first.get("status"):
            status = str(first["status"])
        else:
            status = "completed" if successful else "failed"
        return {
            "status": status,
            "slots": slots,
            "results": results,
            "successful": successful,
        }


def schedule_status(config: dict, state_db: Path) -> dict:
    """Return safe local schedule state for ``status``/``doctor`` commands."""

    zone = _zone(config)
    state_db = Path(state_db)
    state_db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(state_db, timeout=0.5)
    try:
        _ensure_schedule_schema(connection)
        row = connection.execute(
            "SELECT run_key, scheduled_local, scheduled_utc, handled_at, detail FROM schedule_runs ORDER BY scheduled_utc DESC LIMIT 1"
        ).fetchone()
        return {
            "enabled": _settings(config).get("enabled", True) is not False,
            "timezone": zone.key,
            "times": [item.strftime("%H:%M") for item in configured_times(config)],
            "last_success": (
                {
                    "run_key": row[0],
                    "scheduled_local": row[1],
                    "scheduled_utc": row[2],
                    "handled_at": row[3],
                    "detail": row[4],
                }
                if row
                else None
            ),
        }
    finally:
        connection.close()


__all__ = [
    "configured_times",
    "due",
    "execution_lock",
    "mark_handled",
    "record_success",
    "scheduled_run",
    "schedule_status",
]
