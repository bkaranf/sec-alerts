"""Small, deterministic earnings calendar for verified servicing candidates.

The calendar stores dates supplied from official issuer evidence.  It never
turns an announced date into a completed call, and its refresh command only
checks explicitly supplied official IR pages for access and content changes.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from functools import cmp_to_key
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EVENT_NAMES = ("latest_completed_call", "next_announced_release", "next_announced_call")
NEXT_STATUSES = {"announced", "unknown", "cancelled", "superseded"}
LATEST_STATUSES = {"completed", "unknown", "cancelled", "superseded"}
INCLUDED_ELIGIBILITY = {"confirmed", "included", "eligible", "confirmed_included", "included_requested_universe"}
_TICKER = re.compile(r"[A-Z0-9.]{1,12}")

CSV_FIELDS = (
    "cik", "company", "ticker", "servicing_category", "eligibility", "reporting_period",
    "latest_completed_call_datetime", "latest_completed_call_timezone", "latest_completed_call_official_source_url", "latest_completed_call_status",
    "next_announced_release_datetime", "next_announced_release_timezone", "next_announced_release_official_source_url", "next_announced_release_status",
    "next_announced_call_datetime", "next_announced_call_timezone", "next_announced_call_official_source_url", "next_announced_call_status",
    "checked_at", "unresolved_note", "official_ir_url", "refresh_checked_at", "refresh_status",
    "refresh_source_url", "refresh_http_status", "refresh_content_sha256", "refresh_changed", "refresh_error",
)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _url(value: Any, *, field: str, required: bool = False) -> str:
    result = _text(value)
    if not result:
        if required:
            raise ValueError(f"{field} requires an official http(s) source URL")
        return ""
    parsed = urlparse(result)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field} must be an official http(s) URL")
    return result


def _zone(value: Any):
    name = _text(value)
    if not name:
        return None
    if name.upper() in {"UTC", "Z"}:
        return timezone.utc
    if re.fullmatch(r"[+-]\d{2}:?\d{2}", name):
        sign = 1 if name[0] == "+" else -1
        compact = name[1:].replace(":", "")
        return timezone(sign * timedelta(hours=int(compact[:2]), minutes=int(compact[2:])))
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _parse_datetime(value: Any, timezone_name: Any = "") -> tuple[datetime | None, date | None, bool]:
    """Return UTC instant, local date, and whether an exact time is known."""
    if isinstance(value, datetime):
        parsed = value
        date_only = False
    elif isinstance(value, date):
        return None, value, False
    else:
        raw = _text(value)
        if not raw:
            return None, None, False
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            try:
                return None, date.fromisoformat(raw), False
            except ValueError:
                return None, None, False
        date_only = False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None, None, False
    if parsed.tzinfo is None:
        tz = _zone(timezone_name)
        if tz is None:
            return None, parsed.date(), False
        parsed = parsed.replace(tzinfo=tz)
    local_tz = _zone(timezone_name) or parsed.tzinfo
    local_date = parsed.astimezone(local_tz).date() if local_tz else parsed.date()
    return parsed.astimezone(timezone.utc), local_date, not date_only


def _as_of_info(value: Any, timezone_name: str = "UTC") -> tuple[datetime, bool]:
    raw = _text(value)
    date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw))
    instant, day, _ = _parse_datetime(value, timezone_name)
    if instant is not None:
        return instant, date_only
    if day is not None:
        tz = _zone(timezone_name) or timezone.utc
        return datetime.combine(day, time.max, tzinfo=tz).astimezone(timezone.utc), True
    raise ValueError(f"as_of must be an ISO date or timezone-aware datetime: {value!r}")


def _as_of(value: Any) -> datetime:
    return _as_of_info(value)[0]


def _event(raw: Any, source: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = dict(raw) if isinstance(raw, Mapping) else ({"datetime": raw} if raw else {})
    prefix = f"{name}_"
    if not value:
        value = {
            "datetime": source.get(prefix + "datetime"),
            "timezone": source.get(prefix + "timezone"),
            "official_source_url": source.get(prefix + "official_source_url"),
            "status": source.get(prefix + "status"),
        }
    status = _text(value.get("status")).lower() or "unknown"
    result = {
        "datetime": _text(value.get("datetime") or value.get("date")) or None,
        "timezone": _text(value.get("timezone")),
        "official_source_url": _url(value.get("official_source_url") or value.get("source_url"), field=f"{name}.official_source_url"),
        "status": status,
    }
    return result


def normalize_row(raw: Mapping[str, Any], *, default_checked_at: Any = None) -> dict[str, Any]:
    """Normalize one JSON/CSV row and enforce the calendar contract."""
    if not isinstance(raw, Mapping):
        raise ValueError("calendar rows must be objects")
    row = dict(raw)
    ticker = _text(row.get("ticker")).upper()
    if ticker and not _TICKER.fullmatch(ticker):
        raise ValueError(f"invalid ticker: {row.get('ticker')!r}")
    if not row.get("company") and row.get("name"):
        row["company"] = row["name"]
    if not row.get("servicing_category") and row.get("category"):
        row["servicing_category"] = row["category"]
    for field in ("cik", "company", "servicing_category", "eligibility"):
        if not _text(row.get(field)):
            raise ValueError(f"calendar row {ticker} requires {field}")
    row.update({
        "cik": _text(row["cik"]),
        "company": _text(row["company"]),
        "ticker": ticker,
        "servicing_category": _text(row["servicing_category"]),
        "eligibility": _text(row["eligibility"]).lower(),
        "reporting_period": _text(row.get("reporting_period") or row.get("reportingperiod")) or "unknown",
    })
    checked_at = row.get("checked_at") or default_checked_at
    if not _text(checked_at):
        raise ValueError(f"calendar row {ticker} requires checked_at")
    if _parse_datetime(checked_at)[0] is None and _parse_datetime(checked_at)[1] is None:
        raise ValueError(f"calendar row {ticker} has invalid checked_at")
    row["checked_at"] = _text(checked_at)
    for name in EVENT_NAMES:
        row[name] = _event(row.get(name), row, name)
    row["unresolved_note"] = _text(row.get("unresolved_note"))
    if row.get("official_ir_url"):
        row["official_ir_url"] = _url(row["official_ir_url"], field=f"{ticker}.official_ir_url")
    _validate_events(row)
    if "refresh" in row and row["refresh"] is not None:
        if not isinstance(row["refresh"], Mapping):
            raise ValueError(f"calendar row {ticker} refresh must be an object")
        refresh = dict(row["refresh"])
        if refresh.get("source_url"):
            refresh["source_url"] = _url(refresh["source_url"], field=f"{ticker}.refresh.source_url")
        row["refresh"] = refresh
    return row


def _validate_events(row: Mapping[str, Any]) -> None:
    ticker = row["ticker"]
    for name in EVENT_NAMES:
        event = row[name]
        status = _text(event.get("status")).lower()
        allowed = LATEST_STATUSES if name == "latest_completed_call" else NEXT_STATUSES
        if status not in allowed:
            raise ValueError(f"{ticker}.{name}.status must be one of {sorted(allowed)}")
        value = event.get("datetime")
        if value:
            instant, day, _ = _parse_datetime(value, event.get("timezone"))
            if instant is None and day is None:
                raise ValueError(f"{ticker}.{name}.datetime is not ISO parseable")
        if status == "completed":
            if name != "latest_completed_call":
                raise ValueError(f"{ticker}.{name} cannot be completed")
            if not value:
                raise ValueError(f"{ticker}.latest_completed_call requires datetime when completed")
            _url(event.get("official_source_url"), field=f"{ticker}.{name}.official_source_url", required=True)
        if name != "latest_completed_call" and status == "announced":
            if not value:
                raise ValueError(f"{ticker}.{name} requires datetime when announced")
            _url(event.get("official_source_url"), field=f"{ticker}.{name}.official_source_url", required=True)


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        if not all(isinstance(item, Mapping) for item in value):
            raise ValueError("calendar rows must all be objects")
        return list(value)
    if isinstance(value, Mapping):
        for key in ("calendar", "companies", "candidates", "rows"):
            if isinstance(value.get(key), list):
                if not all(isinstance(item, Mapping) for item in value[key]):
                    raise ValueError(f"calendar field {key} contains a malformed row")
                return list(value[key])
    raise ValueError("calendar input must be a list or an object containing calendar rows")


def _dedupe(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rows:
        row = raw if isinstance(raw, dict) else normalize_row(raw)
        key = _text(row.get("cik"))
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(row if isinstance(row, dict) else dict(row))
    return result


def load_calendar(path: Path | str, *, default_checked_at: Any = None) -> list[dict[str, Any]]:
    """Load and normalize a JSON or CSV calendar."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return _dedupe(normalize_row(_csv_row(row), default_checked_at=default_checked_at) for row in csv.DictReader(stream))
    data = json.loads(path.read_text(encoding="utf-8"))
    return _dedupe(normalize_row(row, default_checked_at=default_checked_at) for row in _records(data))


def _csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for name in EVENT_NAMES:
        prefix = f"{name}_"
        result[name] = {
            "datetime": result.pop(prefix + "datetime", ""),
            "timezone": result.pop(prefix + "timezone", ""),
            "official_source_url": result.pop(prefix + "official_source_url", ""),
            "status": result.pop(prefix + "status", "unknown"),
        }
    refresh_fields = {"checked_at", "status", "source_url", "http_status", "content_sha256", "changed", "error"}
    refresh = {key: result.pop("refresh_" + key, "") for key in refresh_fields}
    if any(_text(value) for value in refresh.values()):
        result["refresh"] = refresh
    return result


def save_calendar(rows: Iterable[Mapping[str, Any]], path: Path | str) -> Path:
    """Validate and save rows as the format selected by ``path``."""
    normalized = [normalize_row(row) for row in rows]
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".csv":
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in normalized:
                writer.writerow(_flat_csv_row(row))
    else:
        path.write_text(json.dumps({"version": 1, "calendar": normalized}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _flat_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = {field: row.get(field, "") for field in CSV_FIELDS}
    for name in EVENT_NAMES:
        event = row[name]
        result[f"{name}_datetime"] = event.get("datetime") or ""
        result[f"{name}_timezone"] = event.get("timezone", "")
        result[f"{name}_official_source_url"] = event.get("official_source_url", "")
        result[f"{name}_status"] = event.get("status", "unknown")
    refresh = row.get("refresh") if isinstance(row.get("refresh"), Mapping) else {}
    for key in ("checked_at", "status", "source_url", "http_status", "content_sha256", "changed", "error"):
        result[f"refresh_{key}"] = refresh.get(key, "")
    return result


def _included(row: Mapping[str, Any]) -> bool:
    return _text(row.get("eligibility")).lower() in INCLUDED_ELIGIBILITY


def _row_key(row: Mapping[str, Any]) -> str:
    return _text(row.get("ticker")) or _text(row.get("cik"))


def select_latest5(rows: Sequence[Mapping[str, Any]], as_of: Any, *, limit: int = 5, as_of_timezone: str = "UTC") -> dict[str, Any]:
    """Select the latest explicitly completed calls and expose tie decisions."""
    if limit < 1:
        raise ValueError("limit must be positive")
    if _zone(as_of_timezone) is None:
        raise ValueError(f"as_of_timezone is not recognized: {as_of_timezone!r}")
    cutoff, date_only_cutoff = _as_of_info(as_of, as_of_timezone)
    cutoff_day = cutoff.astimezone(_zone(as_of_timezone)).date()
    candidates: list[tuple[dict[str, Any], date, datetime | None, bool]] = []
    excluded: list[dict[str, str]] = []
    for row in _dedupe(normalize_row(raw) for raw in rows):
        key = _row_key(row)
        cik = _text(row.get("cik"))
        if not _included(row):
            resolved_exclusion = row["eligibility"] in {"excluded", "consolidated", "inactive", "outside_reporting_scope"}
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": "confirmed_excluded" if resolved_exclusion else "unresolved_eligibility", "reason": row.get("unresolved_note") or ("documented universe exclusion" if resolved_exclusion else "eligibility is not confirmed or included")})
            continue
        event = row["latest_completed_call"]
        if event.get("status") != "completed":
            if event.get("status") in {"cancelled", "superseded"}:
                category = "included_call_cancelled_or_superseded"
                reason = f"included candidate's latest call is {event['status']}, not completed"
            elif event.get("datetime"):
                category = "included_call_unknown"
                reason = "included candidate has a call date but no explicit completed status"
            else:
                category = "included_call_unknown"
                reason = "included candidate's latest completed call has not been verified"
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": category, "reason": reason})
            continue
        instant, day, known_time = _parse_datetime(event.get("datetime"), event.get("timezone"))
        if day is None:
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": "confirmed_call_date_unverified", "reason": "completed call date is missing or invalid"})
            continue
        if instant is not None and instant > cutoff:
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": "newer_potential_candidate", "reason": "completed call is after as_of"})
            continue
        if instant is None and day > cutoff_day:
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": "newer_potential_candidate", "reason": "completed call date is after as_of"})
            continue
        if instant is None and not date_only_cutoff and day == cutoff_day:
            excluded.append({"key": cik, "ticker": row["ticker"], "display_key": key, "category": "call_time_unverified_before_intraday_cutoff", "reason": "same-day call time is unverified, so occurrence before the intraday as_of cutoff cannot be confirmed"})
            continue
        candidates.append((row, day, instant, known_time))

    unknown_days = {item[1] for item in candidates if not item[3]}

    def compare(left: tuple[dict[str, Any], date, datetime | None, bool], right: tuple[dict[str, Any], date, datetime | None, bool]) -> int:
        if left[1] == right[1] and left[1] in unknown_days:
            return -1 if _row_key(left[0]) < _row_key(right[0]) else (1 if _row_key(left[0]) > _row_key(right[0]) else 0)
        if left[2] is not None and right[2] is not None and left[2] != right[2]:
            return -1 if left[2] > right[2] else 1
        if left[1] != right[1]:
            return -1 if left[1] > right[1] else 1
        return -1 if _row_key(left[0]) < _row_key(right[0]) else (1 if _row_key(left[0]) > _row_key(right[0]) else 0)

    ordered = sorted(candidates, key=cmp_to_key(compare))
    notes: list[str] = []
    for day in sorted(unknown_days, reverse=True):
        group = sorted(_row_key(item[0]) for item in candidates if item[1] == day)
        notes.append(f"{day.isoformat()}: one or more call times are unverified; the whole same-day group is ordered alphabetically ({', '.join(group)}).")
    definitive_categories = {"unresolved_eligibility", "newer_potential_candidate", "confirmed_call_date_unverified", "call_time_unverified_before_intraday_cutoff", "included_call_unknown", "included_call_cancelled_or_superseded"}
    definitive_reasons = [f"{item['key']}: {item['reason']}" for item in excluded if item["category"] in definitive_categories]
    if definitive_reasons:
        notes.append("The selection is not definitive because unresolved or newer potential candidates remain outside it.")
    selected = []
    for rank, (row, _day, _instant, known_time) in enumerate(ordered[:limit], start=1):
        item = copy.deepcopy(row)
        item["selection"] = {"rank": rank, "call_time_verified": known_time, "sort_basis": "UTC call time" if known_time else "alphabetical same-day fallback"}
        selected.append(item)
    return {"as_of": cutoff.isoformat(), "as_of_timezone": as_of_timezone, "as_of_policy": "Date-only as_of uses the end of the declared timezone day; an unverified same-day call cannot be confirmed before an intraday cutoff.", "limit": limit, "definitive": bool(selected) and not definitive_reasons, "definitive_reasons": definitive_reasons, "selected": selected, "excluded": excluded, "notes": notes}


def due_items(rows: Sequence[Mapping[str, Any]], as_of: Any, *, horizon_days: int = 1, stale_after_days: int = 3) -> list[dict[str, Any]]:
    """Return announced near-term events and rows needing another check."""
    if horizon_days < 0 or stale_after_days < 0:
        raise ValueError("horizon_days and stale_after_days must be non-negative")
    cutoff = _as_of(as_of)
    horizon = datetime.combine((cutoff + timedelta(days=horizon_days)).date(), time.max, tzinfo=timezone.utc)
    result: list[dict[str, Any]] = []
    for row in _dedupe(normalize_row(raw) for raw in rows):
        if row["eligibility"] in {"excluded", "consolidated", "inactive", "outside_reporting_scope"}:
            continue
        ticker = _row_key(row)
        checked, _, _ = _parse_datetime(row.get("checked_at"))
        if checked is None or checked < cutoff - timedelta(days=stale_after_days):
            result.append({"ticker": ticker, "company": row["company"], "kind": "check", "priority": 1, "reason": "calendar check is stale or unverified", "checked_at": row.get("checked_at")})
        if not _included(row):
            result.append({"ticker": ticker, "company": row["company"], "kind": "eligibility", "priority": 1, "reason": "eligibility remains unresolved; verify before selection"})
        for name in ("next_announced_release", "next_announced_call"):
            event = row[name]
            status = event.get("status")
            if status in {"cancelled", "superseded"}:
                result.append({"ticker": ticker, "company": row["company"], "kind": name, "priority": 1, "status": status, "reason": f"{status} announcement needs reconciliation", "event": event})
                continue
            if status == "unknown":
                result.append({"ticker": ticker, "company": row["company"], "kind": name, "priority": 2, "status": status, "reason": "next date is unknown; check the official IR page", "event": event})
                continue
            instant, day, _ = _parse_datetime(event.get("datetime"), event.get("timezone"))
            near = (instant is not None and instant <= horizon) or (instant is None and day is not None and day <= horizon.date())
            if near:
                result.append({"ticker": ticker, "company": row["company"], "kind": name, "priority": 0, "status": status, "reason": "announced release or call is due or overdue", "event": event})
    result.sort(key=lambda item: (item["priority"], item["ticker"], item["kind"]))
    return result


def _safe_error(exc: BaseException) -> str:
    return " ".join(str(exc).split())[:300] or type(exc).__name__


def _ir_url(value: Any, *, field: str) -> str:
    result = _url(value, field=field, required=True)
    host = (urlparse(result).hostname or "").lower().rstrip(".")
    if host == "sec.gov" or host.endswith(".sec.gov"):
        raise ValueError(f"{field} must be an issuer IR URL; SEC URLs are not accepted by calendar refresh")
    return result


def _fetch_official(url: str, *, cache_dir: Path | None, timeout: float) -> dict[str, Any]:
    """Check one supplied official URL through the existing robots-aware IR fetcher."""
    from servicing_brief.sources.ir import _fetch
    import httpx

    url = _ir_url(url, field="official IR URL")

    def run(storage: Path) -> dict[str, Any]:
        config = {"_storage": str(storage), "sources": {"ir_timeout": timeout, "ir_max_retries": 0, "ir_user_agent": "mortgage-servicing-calendar/0.1"}}
        with httpx.Client(follow_redirects=True, timeout=timeout) as session:
            fetched = _fetch(config, session, {}, {}, url)
        return {"status": "ok", "http_status": fetched.status_code, "source_url": fetched.final_url, "content": fetched.content}

    if cache_dir is not None:
        return run(Path(cache_dir))
    with tempfile.TemporaryDirectory(prefix="servicing-calendar-") as temp:
        return run(Path(temp))


def refresh_calendar(
    rows: Sequence[Mapping[str, Any]],
    *,
    official_urls: Mapping[str, str] | None = None,
    checked_at: Any = None,
    cache_dir: Path | str | None = None,
    timeout: float = 20.0,
    fetcher: Callable[[str], Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check supplied IR pages and update only refresh provenance/status.

    Date fields are deliberately untouched.  A caller that has manually
    verified a new date must write that event into the returned row separately.
    """
    checked = checked_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    urls = {str(key).upper(): value for key, value in (official_urls or {}).items()}
    output: list[dict[str, Any]] = []
    for raw in rows:
        row = normalize_row(raw, default_checked_at=checked)
        ticker = _row_key(row)
        url = urls.get(ticker) or urls.get(_text(row.get("cik")).upper()) or _text(row.get("official_ir_url"))
        previous = row.get("refresh") if isinstance(row.get("refresh"), Mapping) else {}
        if not url:
            row["refresh"] = {"checked_at": checked, "status": "missing_official_url", "source_url": "", "http_status": "", "content_sha256": "", "changed": None, "error": "no supplied official IR URL"}
            output.append(row)
            continue
        url = _ir_url(url, field=f"{ticker}.refresh.source_url")
        try:
            page = fetcher(url) if fetcher else _fetch_official(url, cache_dir=Path(cache_dir) if cache_dir else None, timeout=timeout)
            content = bytes(page.get("content", b""))
            digest = hashlib.sha256(content).hexdigest()
            old_digest = _text(previous.get("content_sha256"))
            row["refresh"] = {"checked_at": checked, "status": _text(page.get("status")) or "ok", "source_url": _ir_url(page.get("source_url") or url, field=f"{ticker}.refresh.source_url"), "http_status": page.get("http_status", ""), "content_sha256": digest, "changed": (digest != old_digest) if old_digest else None, "error": ""}
        except Exception as exc:
            row["refresh"] = {"checked_at": checked, "status": "blocked" if getattr(exc, "blocked", False) else "error", "source_url": url, "http_status": getattr(exc, "status_code", ""), "content_sha256": "", "changed": None, "error": _safe_error(exc)}
        output.append(row)
    return output


def _write_json(value: Any, output: Path | None) -> None:
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def _url_args(values: Sequence[str]) -> dict[str, str]:
    result = {}
    for value in values:
        ticker, separator, url = value.partition("=")
        if not separator or not ticker.strip() or not url.strip():
            raise ValueError("--url values must be TICKER=HTTPS_URL")
        result[ticker.strip().upper()] = _url(url.strip(), field=f"{ticker}.official_url", required=True)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read and select a verified servicing earnings calendar")
    commands = parser.add_subparsers(dest="command", required=True)
    select = commands.add_parser("select", help="select the latest completed calls")
    select.add_argument("--calendar", type=Path, required=True)
    select.add_argument("--as-of", required=True)
    select.add_argument("--limit", type=int, default=5)
    select.add_argument("--output", type=Path)
    due = commands.add_parser("due", help="show due releases/calls and checks")
    due.add_argument("--calendar", type=Path, required=True)
    due.add_argument("--as-of", required=True)
    due.add_argument("--horizon-days", type=int, default=1)
    due.add_argument("--stale-after-days", type=int, default=3)
    due.add_argument("--output", type=Path)
    refresh = commands.add_parser("refresh", help="check supplied official IR pages")
    refresh.add_argument("--calendar", type=Path, help="existing JSON or CSV calendar")
    refresh.add_argument("--companies", type=Path, help="candidate rows to initialize a calendar")
    refresh.add_argument("--output", type=Path, required=True)
    refresh.add_argument("--checked-at")
    refresh.add_argument("--cache-dir", type=Path)
    refresh.add_argument("--url", action="append", default=[], metavar="TICKER=URL")
    args = parser.parse_args(argv)
    if args.command == "select":
        _write_json(select_latest5(load_calendar(args.calendar), args.as_of, limit=args.limit), args.output)
    elif args.command == "due":
        _write_json(due_items(load_calendar(args.calendar), args.as_of, horizon_days=args.horizon_days, stale_after_days=args.stale_after_days), args.output)
    else:
        if bool(args.calendar) == bool(args.companies):
            parser.error("refresh requires exactly one of --calendar or --companies")
        initial_checked_at = args.checked_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        rows = load_calendar(args.calendar or args.companies, default_checked_at=initial_checked_at)
        save_calendar(refresh_calendar(rows, official_urls=_url_args(args.url), checked_at=initial_checked_at, cache_dir=args.cache_dir), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
