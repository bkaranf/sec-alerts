"""Compatibility and package boundary checks for the review renderer."""

from __future__ import annotations

import importlib.util
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from servicing_brief import review_rendering


ROOT = Path(__file__).parents[1]
LEGACY_PATH = ROOT / "output" / "five-company-review" / "render_email.py"
LEGACY_ROOT = LEGACY_PATH.parent
_spec = importlib.util.spec_from_file_location("legacy_review_rendering_compat", LEGACY_PATH)
assert _spec and _spec.loader
legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(legacy)


def _load_legacy(name: str):
    spec = importlib.util.spec_from_file_location(name, LEGACY_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _raw_review() -> dict:
    return {
        "ticker": "TST",
        "cik": "0000123456",
        "name": "Test issuer",
        "period": "Q3 2026",
        "call_date": "2026-08-01",
        "release_date": "2026-07-31",
        "headline": "Mortgage balances increased",
        "summary": "Reported mortgage balances rose during the quarter.",
        "summary_sources": ["1"],
        "metrics": [
            {
                "label": "Servicing balance",
                "current": "C$120m",
                "previous": "C$100m",
                "previous_label": "Q2 2026",
                "sources": ["1"],
                "highlight": "",
            }
        ],
        "analysis": [],
        "call_note": "",
        "call_note_sources": [],
        "investor_question": "How will the issuer manage mortgage risk?",
        "sources": [{"id": "1", "label": "Quarterly report", "url": "https://issuer.example/report"}],
        "chart": None,
    }


def _overlay() -> dict:
    return {
        "version": 1,
        "metric_groups": [
            {
                "title": "Mortgage figures",
                "previous_label": "Q2 2026",
                "current_label": "Q3 2026",
                "rows": [{"metric_index": 0, "unit": "C$m"}],
            }
        ],
        "excluded_metrics": [],
        "insights": [],
        "chart": None,
    }


def test_package_and_legacy_renderers_keep_the_same_synthetic_output() -> None:
    raw = _raw_review()
    overlay = _overlay()
    package_company = review_rendering._review(raw, "TST", False, overlay, 1)
    legacy_company = legacy._review(raw, "TST", False, overlay, 1)
    assert package_company == legacy_company

    package_env = review_rendering.create_review_environment()
    legacy_env = legacy.create_review_environment()
    assert package_env._review_template_name == "review_email.html.j2"
    assert legacy_env._review_template_name == "email-template.html.j2"
    assert package_env.get_template("review_email.html.j2")
    assert legacy_env.get_template("email-template.html.j2")

    for full_document in (False, True):
        package_html = review_rendering._render_html(
            package_env,
            [package_company],
            combined=False,
            universe_note="",
            attachment_note="",
            full_document=full_document,
        )
        legacy_html = legacy._render_html(
            legacy_env,
            [legacy_company],
            combined=False,
            universe_note="",
            attachment_note="",
            full_document=full_document,
        )
        assert package_html == legacy_html

    assert review_rendering._render_text(
        [package_company], combined=False, universe_note="", attachment_note=""
    ) == legacy._render_text([legacy_company], combined=False, universe_note="", attachment_note="")


def test_legacy_monkeypatch_is_isolated_from_other_renderer_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    original = review_rendering._metric_groups
    second_legacy = _load_legacy("legacy_review_rendering_second")
    calls: list[bool] = []

    def patched_metric_groups(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(legacy, "_metric_groups", patched_metric_groups)
    legacy._review(_raw_review(), "TST", False, _overlay(), 1)

    assert calls == [True]
    assert review_rendering._metric_groups is original
    assert second_legacy._impl._metric_groups is not legacy._impl._metric_groups
    second_legacy._review(_raw_review(), "TST", False, _overlay(), 1)
    assert review_rendering._metric_groups is original


def test_canonical_patch_survives_legacy_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    original = review_rendering._metric_groups

    def patched_metric_groups(*args, **kwargs):
        return original(*args, **kwargs)

    monkeypatch.setattr(review_rendering, "_metric_groups", patched_metric_groups)
    legacy._review(_raw_review(), "TST", False, _overlay(), 1)
    assert review_rendering._metric_groups is patched_metric_groups


def test_legacy_nonfunction_patch_is_restored_after_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    original = legacy._impl.MONEY_RE
    replacement = re.compile(original.pattern + r"(?:)", original.flags)
    assert replacement is not original
    monkeypatch.setattr(legacy, "MONEY_RE", replacement)

    assert legacy._financial_display("C$1m") == "C$1m"
    assert legacy._impl.MONEY_RE is original


def test_overlapping_legacy_dispatches_are_serialized(monkeypatch: pytest.MonkeyPatch) -> None:
    original = legacy._impl._metric_groups
    entered = threading.Event()
    first_started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    errors: list[BaseException] = []

    def patched_metric_groups(*args, **kwargs):
        entered.set()
        release.wait(timeout=5)
        return original(*args, **kwargs)

    def render_once(started: threading.Event) -> None:
        try:
            started.set()
            legacy._review(_raw_review(), "TST", False, _overlay(), 1)
        except BaseException as exc:  # pragma: no cover - assertion reports the worker failure
            errors.append(exc)

    monkeypatch.setattr(legacy, "_metric_groups", patched_metric_groups)
    first = threading.Thread(target=render_once, args=(first_started,))
    first.start()
    assert entered.wait(timeout=2)

    second = threading.Thread(target=render_once, args=(second_started,))
    second.start()
    assert second_started.wait(timeout=2)
    assert not legacy._PATCH_LOCK.acquire(blocking=False)
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert legacy._impl._metric_groups is original


def test_legacy_main_passes_historical_defaults_without_running_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_main(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return 17

    monkeypatch.setattr(legacy, "_impl", SimpleNamespace(main=fake_main))
    assert legacy.main(["--tickers", "TST"]) == 17
    assert seen["argv"] == ["--tickers", "TST"]
    assert seen["default_review_root"] == LEGACY_ROOT
    assert seen["template_root"] == LEGACY_ROOT


def test_packaged_main_requires_an_explicit_review_root() -> None:
    with pytest.raises(ValueError, match="--review-root is required"):
        review_rendering.main([])
