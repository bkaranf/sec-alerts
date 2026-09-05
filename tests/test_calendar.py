"""Focused offline checks for the verified earnings calendar."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from servicing_brief.calendar import due_items, load_calendar, refresh_calendar, save_calendar, select_latest5


def _row(
    cik: str,
    ticker: str,
    call: str | None,
    *,
    status: str = "completed",
    eligibility: str = "confirmed",
    call_timezone: str = "UTC",
    checked_at: str = "2026-09-04T12:00:00Z",
    next_call: dict | None = None,
    next_release: dict | None = None,
) -> dict:
    return {
        "cik": cik,
        "company": ticker or f"Company {cik}",
        "ticker": ticker,
        "servicing_category": "mortgage servicing",
        "eligibility": eligibility,
        "reporting_period": "Q2 2026",
        "latest_completed_call": {
            "datetime": call,
            "timezone": call_timezone,
            "official_source_url": f"https://ir.example/{cik}/call" if call else "",
            "status": status,
        },
        "next_announced_release": next_release or {"status": "unknown"},
        "next_announced_call": next_call or {"status": "unknown"},
        "checked_at": checked_at,
    }


def test_json_and_csv_round_trip_keep_separate_event_statuses(tmp_path: Path):
    row = _row(
        "1",
        "AAA",
        "2026-08-06T15:00:00Z",
        next_call={
            "datetime": "2026-09-10T17:00:00-04:00",
            "timezone": "America/New_York",
            "official_source_url": "https://ir.example/1/next-call",
            "status": "announced",
        },
        next_release={
            "datetime": "2026-09-10",
            "timezone": "America/New_York",
            "official_source_url": "https://ir.example/1/release",
            "status": "announced",
        },
    )
    json_path = save_calendar([row], tmp_path / "calendar.json")
    csv_path = save_calendar([row], tmp_path / "calendar.csv")
    for path in (json_path, csv_path):
        loaded = load_calendar(path)
        assert loaded[0]["cik"] == "1"
        assert loaded[0]["latest_completed_call"]["status"] == "completed"
        assert loaded[0]["next_announced_call"]["status"] == "announced"
        assert loaded[0]["next_announced_release"]["datetime"] == "2026-09-10"


def test_selection_requires_explicit_completion_and_dedupes_cik():
    rows = [
        _row("1", "AAA", "2026-08-06T15:00:00Z"),
        _row("1", "AAX", "2026-08-07T15:00:00Z"),
        _row("2", "BBB", "2026-08-06", status="unknown"),
        _row("3", "CCC", "2026-08-06", status="cancelled"),
        _row("4", "DDD", "2026-09-06T15:00:00Z"),
        _row("5", "EEE", "2026-08-06T15:00:00Z", eligibility="unresolved"),
        _row("6", "", "2026-08-05T15:00:00Z"),
    ]
    report = select_latest5(rows, "2026-09-05T12:00:00Z")
    assert [item["cik"] for item in report["selected"]] == ["1", "6"]
    assert report["definitive"] is False
    categories = {item["key"]: item["category"] for item in report["excluded"]}
    assert categories["2"] == "included_call_unknown"
    assert categories["3"] == "included_call_cancelled_or_superseded"
    assert categories["4"] == "newer_potential_candidate"
    assert categories["5"] == "unresolved_eligibility"


def test_same_day_unknown_time_orders_whole_group_alphabetically_and_utc_is_global():
    rows = [
        _row("1", "ZZZ", "2026-08-05T23:30:00Z"),
        _row("2", "BBB", "2026-08-06T14:00:00Z"),
        _row("3", "AAA", "2026-08-06T15:00:00Z"),
        _row("4", "CCC", "2026-08-06"),
    ]
    report = select_latest5(rows, "2026-09-05", as_of_timezone="America/New_York")
    assert [item["ticker"] for item in report["selected"]] == ["AAA", "BBB", "CCC", "ZZZ"]
    assert any("whole same-day group" in note and "AAA, BBB, CCC" in note for note in report["notes"])


def test_intraday_cutoff_does_not_promote_unknown_same_day_call():
    row = _row("1", "AAA", "2026-09-05")
    report = select_latest5([row], "2026-09-05T12:00:00-04:00", as_of_timezone="America/New_York")
    assert report["selected"] == []
    assert report["excluded"][0]["category"] == "call_time_unverified_before_intraday_cutoff"
    assert report["definitive"] is False


def test_confirmed_exclusions_do_not_hide_unknown_calls_or_fill_check_queue():
    eligible = _row("1", "AAA", "2026-08-27T12:00:00Z")
    excluded = _row("2", "BBB", None, status="unknown", eligibility="excluded")
    excluded["unresolved_note"] = "Reviewed disclosure concerns consumer installment loans only."
    result = select_latest5([eligible, excluded], "2026-09-05")
    assert result["definitive"] is True
    assert result["excluded"][0]["category"] == "confirmed_excluded"
    assert not any(item["ticker"] == "BBB" for item in due_items([eligible, excluded], "2026-09-05"))
    unknown = _row("3", "CCC", None, status="unknown")
    result = select_latest5([eligible, unknown], "2026-09-05")
    assert result["definitive"] is False
    assert result["excluded"][0]["category"] == "included_call_unknown"


def test_due_queue_distinguishes_announced_unknown_stale_and_cancelled():
    rows = [
        _row("1", "AAA", "2026-08-01T12:00:00Z", next_release={"datetime": "2026-09-06", "timezone": "UTC", "official_source_url": "https://ir.example/1/release", "status": "announced"}),
        _row("2", "BBB", "2026-08-01T12:00:00Z", checked_at="2026-08-01T12:00:00Z", next_call={"datetime": "2026-09-06", "timezone": "UTC", "official_source_url": "https://ir.example/2/call", "status": "cancelled"}),
        _row("3", "CCC", "2026-08-01T12:00:00Z", checked_at="2026-09-05T11:00:00Z"),
    ]
    items = due_items(rows, "2026-09-05T12:00:00Z", stale_after_days=3)
    assert any(item["ticker"] == "AAA" and item["kind"] == "next_announced_release" and item["priority"] == 0 for item in items)
    assert any(item["ticker"] == "BBB" and item.get("status") == "cancelled" for item in items)
    assert any(item["ticker"] == "BBB" and item["kind"] == "check" for item in items)
    assert any(item["ticker"] == "CCC" and item["status"] == "unknown" for item in items)


def test_refresh_records_page_hash_without_changing_verified_date():
    row = _row("1", "AAA", "2026-08-06T15:00:00Z", checked_at="2026-08-20T12:00:00Z")
    refreshed = refresh_calendar(
        [row],
        official_urls={"AAA": "https://ir.example/1"},
        checked_at="2026-09-05T12:00:00Z",
        fetcher=lambda url: {"status": "ok", "http_status": 200, "source_url": url, "content": b"official page"},
    )
    assert refreshed[0]["checked_at"] == "2026-08-20T12:00:00Z"
    assert refreshed[0]["refresh"]["checked_at"] == "2026-09-05T12:00:00Z"
    assert refreshed[0]["refresh"]["content_sha256"]


def test_refresh_rejects_sec_url_and_missing_url_is_a_refresh_status():
    row = _row("1", "AAA", "2026-08-06T15:00:00Z")
    with pytest.raises(ValueError, match="SEC URLs"):
        refresh_calendar([row], official_urls={"AAA": "https://www.sec.gov/Archives/test"}, checked_at="2026-09-05T12:00:00Z")
    missing = _row("2", "BBB", "2026-08-06T15:00:00Z")
    missing.pop("latest_completed_call")
    missing["latest_completed_call"] = {"status": "unknown"}
    missing.pop("official_ir_url", None)
    result = refresh_calendar([missing], checked_at="2026-09-05T12:00:00Z")
    assert result[0]["refresh"]["status"] == "missing_official_url"
    assert result[0]["checked_at"] == "2026-09-04T12:00:00Z"


def test_malformed_json_row_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"calendar": [_row("1", "AAA", "2026-08-06T15:00:00Z"), "not a row"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="malformed row"):
        load_calendar(path)
