"""Render the authorized five-company review package.

The review JSON remains canonical.  ``editorial.json`` is an optional display
overlay: it may change labels, prose and grouping, but never numeric inputs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from jinja2 import Environment, FileSystemLoader

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from servicing_brief.reader_content import assert_reader_content
from servicing_brief.branding import brand_view, validate_page_theme
from servicing_brief.company_boundary import canonical_event, normalize_cik


HTTP_SCHEMES = ("http://", "https://")
MONEY_RE = re.compile(
    r"^\s*(?P<leading_sign>[-+\u2212]?)\s*(?:CAD\s*|C\s*)?\$?\s*"
    r"(?P<trailing_sign>[-+\u2212]?)\s*(?P<number>[0-9][0-9,]*(?:\.[0-9]+)?)\s*"
    r"(?P<suffix>bn|billion|b|m|million|mm)?\s*$",
    re.IGNORECASE,
)

PERCENT_RE = re.compile(r"^\s*[-+\u2212]?[0-9]+(?:\.[0-9]+)?%\s*$")
BPS_RE = re.compile(r"^\s*[-+\u2212]?[0-9]+(?:\.[0-9]+)?\s*(?:bps|bp)\s*$", re.IGNORECASE)
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
_FINANCIAL_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<token>"
    r"(?:(?:[-\u2212]\s*)?(?:(?:CAD|C)\s*)?\$?\s*"
    r"(?:[-\u2212]\s*)?[0-9][0-9,]*(?:\.[0-9]+)?"
    r"(?:\s*(?:bps|bp|bn|billion|b|m|million|mm)|\s*%)?)"
    r")"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _has_financial_marker(token: str) -> bool:
    """Require a currency, percentage, or financial scale in prose tokens."""
    return bool(
        "$" in token
        or "%" in token
        or re.search(r"\b(?:bps|bp)\b", token, re.IGNORECASE)
        or re.search(r"\b(?:CAD|C)\b", token, re.IGNORECASE)
        or re.search(r"(?:bn|billion|b|m|million|mm)\s*$", token, re.IGNORECASE)
    )


def _financial_display(value: Any) -> str:
    """Format negative financial scalars embedded in reader-facing prose.

    The canonical source text remains untouched.  This narrow token pass only
    recognizes numeric financial forms, so ordinary prose hyphens and URLs are
    left alone.  Range separators and order stay as supplied while each
    financial endpoint is formatted independently.
    """
    text = _display(value)
    if not text:
        return text
    url_spans = [(match.start(), match.end()) for match in URL_RE.finditer(text)]

    def replace(match: re.Match[str]) -> str:
        token = match.group("token")
        stripped = token.strip()
        if any(start <= match.start() < end for start, end in url_spans):
            return token
        if not _has_financial_marker(stripped):
            if re.fullmatch(r"[-\u2212]0(?:\.0+)?", stripped):
                return _strip_sign(stripped)
            return token
        if text[max(0, match.start() - 1):match.start()] == "(" and text[match.end():match.end() + 1] == ")":
            return token
        if PERCENT_RE.fullmatch(stripped):
            numeric = stripped.lstrip()
            if numeric.startswith(("-", "−")):
                return _strip_sign(stripped) if Decimal(numeric[1:-1]) == 0 else _accounting_text(stripped)
            return token
        if BPS_RE.fullmatch(stripped):
            numeric = stripped.lstrip()
            if numeric.startswith(("-", "−")):
                return _strip_sign(stripped) if Decimal(re.sub(r"\s*(?:bps|bp)\s*$", "", numeric[1:], flags=re.IGNORECASE)) == 0 else _accounting_text(stripped)
            return token
        parts = _money_parts(stripped)
        if parts:
            number, _, sign = parts
            if sign in {"-", "−"}:
                return _strip_sign(stripped) if number == 0 else _accounting_text(stripped)
        return token

    return _FINANCIAL_TOKEN_RE.sub(replace, text)


def _strip_sign(text: str) -> str:
    """Remove the sign from one already-verified numeric display."""
    if "-" in text:
        return text.replace("-", "", 1).strip()
    if "\u2212" in text:
        return text.replace("\u2212", "", 1).strip()
    return text.strip()


def _accounting_text(text: str) -> str:
    """Wrap one already-verified negative display in accounting parentheses."""
    if "-" in text:
        magnitude = text.replace("-", "", 1)
    elif "\u2212" in text:
        magnitude = text.replace("\u2212", "", 1)
    else:
        return text
    return f"({magnitude.strip()})"


def _display(value: Any) -> str:
    """Normalize display prose while retaining financial minus signs."""
    text = "" if value is None else str(value)
    text = re.sub(r"\s*\u2014\s*", ": ", text)
    return text.strip().replace("CAD $", "C$")


_OMIT_SECTIONS = {"deck", "question", "servicing_context", "insights"}
_GENERIC_OMISSION_REASONS = {
    "na", "none", "omit", "omitted", "skip", "skipped", "tbd", "todo", "reason"
}


def _meaningful_reason(value: Any, label: str) -> str:
    """Require an internal omission record to explain the editorial choice."""
    if not isinstance(value, str):
        raise ValueError(f"{label} needs a meaningful reason")
    reason = _display(value)
    compact = re.sub(r"[^a-z0-9]+", "", reason.casefold())
    if len(compact) < 8 or not re.search(r"[a-z]", compact) or compact in _GENERIC_OMISSION_REASONS:
        raise ValueError(f"{label} needs a meaningful reason")
    return reason


def _metric_index(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} needs an integer metric_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{label} needs an integer metric_index")


def _parse_excluded_metrics(value: Any, ticker: str, metric_count: int) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{ticker}: excluded_metrics must be a list")
    exclusions: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item_index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{ticker}: excluded metric {item_index} is not an object")
        metric_index = _metric_index(item.get("metric_index"), f"{ticker}: excluded metric {item_index}")
        if metric_index < 0 or metric_index >= metric_count:
            raise ValueError(f"{ticker}: excluded metric index {metric_index} is unknown")
        if metric_index in seen:
            raise ValueError(f"{ticker}: excluded metric index {metric_index} is duplicated")
        seen.add(metric_index)
        exclusions.append({
            "metric_index": metric_index,
            "reason": _meaningful_reason(item.get("reason"), f"{ticker}: excluded metric {metric_index}"),
        })
    return exclusions


def _parse_omit_sections(value: Any, ticker: str) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{ticker}: omit_sections must be a list")
    omitted: list[dict[str, str]] = []
    seen: set[str] = set()
    for item_index, item in enumerate(value, start=1):
        if not isinstance(item, Mapping):
            raise ValueError(f"{ticker}: omitted section {item_index} is not an object")
        section = str(item.get("section", "")).strip()
        if section not in _OMIT_SECTIONS:
            raise ValueError(f"{ticker}: omitted section {section!r} is not supported")
        if section in seen:
            raise ValueError(f"{ticker}: omitted section {section!r} is duplicated")
        seen.add(section)
        omitted.append({
            "section": section,
            "reason": _meaningful_reason(item.get("reason"), f"{ticker}: omitted section {section}"),
        })
    return omitted


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Missing required JSON file: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read JSON file {path}: {exc}") from exc


def _as_ticker(item: Any) -> str:
    if isinstance(item, Mapping):
        item = item.get("ticker")
    ticker = str(item or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9.]{1,12}", ticker):
        raise ValueError(f"Invalid selected ticker: {item!r}")
    return ticker


def _read_selection(review_root: Path, selection_path: Path | None, tickers_arg: str) -> tuple[list[str], dict[str, Any]]:
    path = selection_path or review_root / "selection.json"
    metadata: dict[str, Any] = {}
    raw_items: Any = None
    if path.exists():
        data = _load_json(path)
        if isinstance(data, Mapping):
            metadata = dict(data)
            for key in ("tickers", "selected_tickers", "selection", "selected", "companies"):
                if key in data:
                    raw_items = data[key]
                    break
        elif isinstance(data, list):
            raw_items = data
        else:
            raise ValueError(f"Selection file must contain an object or list: {path}")
    elif tickers_arg.strip():
        raw_items = [item for item in tickers_arg.split(",") if item.strip()]
    else:
        raise ValueError(f"No selection.json found at {path}; provide --tickers TFC,PFSI,...")
    if not isinstance(raw_items, list):
        raise ValueError("Selection tickers must be a list")
    tickers = [_as_ticker(item) for item in raw_items]
    if len(tickers) != 5:
        raise ValueError(f"Expected exactly five selected tickers, received {len(tickers)}")
    if len(set(tickers)) != len(tickers):
        raise ValueError("Selected tickers must be unique and retain their supplied order")
    return tickers, metadata


def _read_editorial(review_root: Path, editorial_path: Path | None) -> dict[str, Mapping[str, Any]]:
    path = editorial_path or review_root / "editorial.json"
    if not path.exists():
        return {}
    data = _load_json(path)
    if not isinstance(data, Mapping) or data.get("version") != 1:
        raise ValueError(f"Editorial overlay must be an object with version 1: {path}")
    companies = data.get("companies")
    if not isinstance(companies, Mapping):
        raise ValueError(f"Editorial overlay companies must be an object: {path}")
    overlays: dict[str, Mapping[str, Any]] = {}
    for key, value in companies.items():
        ticker = _as_ticker(key)
        if not isinstance(value, Mapping):
            raise ValueError(f"Editorial overlay for {ticker} must be an object")
        overlays[ticker] = value
    return overlays


def _compact_source_label(label: str) -> str:
    text = re.sub(r"\s+", " ", _financial_display(label)).strip()
    return text if len(text) <= 76 else text[:73].rstrip() + "..."


def _source_records(raw_sources: Any, ticker: str, prefix: bool, short_labels: Mapping[str, Any] | None) -> tuple[list[dict[str, str]], dict[str, str]]:
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ValueError(f"{ticker}: review.json must provide at least one source")
    short_labels = short_labels or {}
    records: list[dict[str, str]] = []
    ids: dict[str, str] = {}
    for index, raw in enumerate(raw_sources, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{ticker}: source {index} is not an object")
        source_id = str(raw.get("id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", source_id):
            raise ValueError(f"{ticker}: invalid source id {source_id!r}")
        if source_id in ids:
            raise ValueError(f"{ticker}: duplicate source id {source_id}")
        label = _financial_display(raw.get("label", ""))
        url = str(raw.get("url", "")).strip()
        if not label or not url.lower().startswith(HTTP_SCHEMES):
            raise ValueError(f"{ticker}: source {source_id} needs a label and public HTTP(S) URL")
        lowered = url.lower()
        if any(token in lowered for token in ("localhost", "127.0.0.1", "::1")) or lowered.startswith("file:"):
            raise ValueError(f"{ticker}: source {source_id} uses a local or localhost URL")
        display_id = f"{ticker}-{source_id}" if prefix else source_id
        if source_id in short_labels:
            short_label = _financial_display(short_labels[source_id])
            if not short_label:
                raise ValueError(f"{ticker}: source_labels[{source_id!r}] is empty")
        else:
            short_label = _compact_source_label(label)
        ids[source_id] = display_id
        records.append({
            "id": source_id,
            "display_id": display_id,
            "ref": source_id,
            "anchor_id": f"source-{display_id}",
            "label": label,
            "short_label": short_label,
            "url": url,
            "location": _financial_display(raw.get("location", "")),
        })
    unknown_labels = set(str(key) for key in short_labels) - set(ids)
    if unknown_labels:
        raise ValueError(f"{ticker}: source_labels reference unknown IDs: {sorted(unknown_labels)}")
    return records, ids


def _references(value: Any, label: str, source_ids: Mapping[str, str]) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of source IDs")
    refs: list[str] = []
    for item in value:
        source_id = str(item).strip()
        if source_id not in source_ids:
            raise ValueError(f"{label} references unknown source {source_id!r}")
        refs.append(source_ids[source_id])
    return refs


def _number(value: Any) -> Decimal:
    try:
        text = str(value).strip().replace("\u2212", "-")
        number = Decimal(text)
    except (InvalidOperation, ValueError, AttributeError) as exc:
        raise ValueError(f"not a finite numeric chart value: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite numeric chart value: {value!r}")
    return number


def _abbreviate_period(period: str) -> str:
    text = _display(period)
    match = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text)
    if match:
        month, day_text, year = match.groups()
        return f"{month[:3]} {int(day_text)}, '{year[-2:]}"
    match = re.fullmatch(r"(Q[1-4])\s+(?:FY)?(\d{4})", text, re.IGNORECASE)
    if match:
        return f"{match.group(1).upper()} '{match.group(2)[-2:]}"
    return text


def _money_parts(text: str) -> tuple[Decimal, str, str] | None:
    match = MONEY_RE.fullmatch(_display(text))
    if not match:
        return None
    leading_sign = match.group("leading_sign") or ""
    trailing_sign = match.group("trailing_sign") or ""
    if leading_sign and trailing_sign:
        return None
    sign = leading_sign or trailing_sign
    raw_number = match.group("number")
    number = Decimal(raw_number.replace(",", ""))
    if sign in {"-", "−"}:
        number = -number
    suffix = (match.group("suffix") or "").lower()
    if suffix in {"billion", "b"}:
        suffix = "bn"
    elif suffix in {"million", "mm"}:
        suffix = "m"
    return number, suffix, sign


def _is_signed_display(text: str) -> bool:
    """Return whether a scalar display carries an explicit negative sign."""
    parts = _money_parts(text)
    if parts:
        return parts[2] in {"-", "−"}
    stripped = _display(text)
    return bool(
        (PERCENT_RE.fullmatch(stripped) or BPS_RE.fullmatch(stripped))
        and stripped.startswith(("-", "−"))
    )


def _format_decimal(number: Decimal, preserve_minus: str = "") -> str:
    negative = number < 0
    magnitude = abs(number)
    if magnitude == magnitude.to_integral_value():
        text = format(magnitude, ",.0f")
    else:
        text = format(magnitude, ",f").rstrip("0").rstrip(".")
    return f"({text})" if negative else text


def _unit_code(unit: str) -> str:
    text = _display(unit).lower().replace(" ", "")
    if text in {"c$bn", "cadbn", "cadbillion", "c$billion", "c$billions", "cadbillions"}:
        return "C$bn"
    if text in {"c$m", "cadm", "cadmillion", "c$million", "c$millions", "cadmillions"}:
        return "C$m"
    if text in {"%", "percent", "percentage"}:
        return "%"
    return ""


def _numeric_display(value: Any, target_unit: str = "") -> str:
    text = _display(value)
    code = _unit_code(target_unit)
    parts = _money_parts(text)
    if target_unit and not code:
        raise ValueError(f'Unsupported or unverified display unit: {target_unit}')
    if code == "%":
        if not PERCENT_RE.fullmatch(text):
            raise ValueError('A percentage label requires an explicitly reported percentage')
        if text.lstrip().startswith(("-", "−")):
            number = Decimal(text.lstrip()[1:-1])
            return _strip_sign(text) if number == 0 else _accounting_text(text)
        return text
    if code in {"C$bn", "C$m"}:
        if not parts or not re.match(r'^[-+−]?\s*(?:C\$|CAD\s)', text, re.I) or parts[1] not in {'m','bn'}:
            raise ValueError('Canadian scaled display requires explicit Canadian currency and scale in its source value')
        number, suffix, sign = parts
        if suffix == "bn" and code == "C$m":
            number *= Decimal(1000)
        elif suffix == "m" and code == "C$bn":
            number /= Decimal(1000)
        return _format_decimal(number, sign)
    if PERCENT_RE.fullmatch(text):
        stripped = text.strip()
        if stripped.startswith(("-", "−")):
            number = Decimal(stripped[1:-1])
            return _strip_sign(text) if number == 0 else _accounting_text(text)
        return text
    if parts and not code:
        number, _, sign = parts
        if sign in {"-", "−"}:
            return _strip_sign(text) if number == 0 else _accounting_text(text)
        return text
    if BPS_RE.fullmatch(text):
        stripped = text.strip()
        if stripped.startswith(("-", "−")):
            number = Decimal(re.sub(r"\s*(?:bps|bp)\s*$", "", stripped[1:], flags=re.IGNORECASE))
            return _strip_sign(text) if number == 0 else _accounting_text(text)
        return text
    # Percentages keep their sign and percent mark; other unrecognised text is
    # intentionally preserved rather than guessed or silently rewritten.
    return text


def _infer_unit(value: Any) -> str:
    text = _display(value)
    if "%" in text:
        return "%"
    parts = _money_parts(text)
    if parts and re.match(r'^[-+−]?\s*(?:C\$|CAD\s)', text, re.I):
        return "C$bn" if parts[1] == "bn" else "C$m" if parts[1] == "m" else ""
    return ""


def _chart(raw_chart: Any, overlay_chart: Any, ticker: str, source_ids: Mapping[str, str]) -> tuple[dict[str, Any] | None, str]:
    if overlay_chart is not None and not isinstance(overlay_chart, Mapping):
        raise ValueError(f"{ticker}: editorial chart must be an object")
    overlay_chart = overlay_chart or {}
    if "omit" in overlay_chart and not isinstance(overlay_chart["omit"], bool):
        raise ValueError(f"{ticker}: editorial chart omit must be true or false")
    if overlay_chart.get("omit"):
        if raw_chart is None or not isinstance(raw_chart, Mapping):
            raise ValueError(f"{ticker}: cannot omit a chart without canonical chart values")
        reason = _meaningful_reason(overlay_chart.get("reason"), f"{ticker}: chart omission")
        return None, reason
    if raw_chart is None:
        if overlay_chart:
            raise ValueError(f"{ticker}: editorial chart provided without canonical chart values")
        return None, ""
    if not isinstance(raw_chart, Mapping):
        return None, "Chart omitted because the review did not provide a usable comparable series."
    raw_points = raw_chart.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        return None, "Chart omitted because the review did not provide two comparable reported points."
    title = _financial_display(overlay_chart.get("title") or raw_chart.get("title", ""))
    scope = _financial_display(overlay_chart.get("scope") or raw_chart.get("reader_note", ""))
    unit = _display(overlay_chart.get("unit_label") or raw_chart.get("unit", ""))
    if not title:
        return None, "Chart omitted because the review did not provide a chart title."
    source_refs = _references(raw_chart.get("sources", []), f"{ticker} chart", source_ids)
    labels = overlay_chart.get("period_labels")
    if labels is not None:
        if not isinstance(labels, list) or len(labels) != len(raw_points):
            raise ValueError(f"{ticker}: chart period_labels must match canonical point count")
        periods = [_display(item) for item in labels]
        if any(not item for item in periods):
            raise ValueError(f"{ticker}: chart period_labels cannot be empty")
    else:
        periods = [_display(point.get("period", "")) for point in raw_points]
    parsed: list[tuple[str, str, Decimal]] = []
    for index, (raw_point, period) in enumerate(zip(raw_points, periods), start=1):
        if not isinstance(raw_point, Mapping):
            raise ValueError(f"{ticker}: chart point {index} is not an object")
        canonical_period = _display(raw_point.get("period", ""))
        display = _display(raw_point.get("display", ""))
        if not canonical_period or not period or not display:
            raise ValueError(f"{ticker}: chart point {index} needs period and display")
        parsed.append((period, display, _number(raw_point.get("value"))))
    scale = max(abs(value) for _, _, value in parsed)
    if scale <= 0:
        return None, "Chart omitted because the comparable reported values have no nonzero scale."
    points = []
    signed = any(value < 0 for _, _, value in parsed)
    bar_extent = 52 if signed else 104
    target_unit = _unit_code(unit)
    for index, (period, raw_display, value) in enumerate(parsed):
        display = _numeric_display(raw_display, target_unit)
        aria_display = _numeric_display(raw_display) if value < 0 or _is_signed_display(raw_display) else raw_display
        points.append({
            "period": period,
            "axis_period": _abbreviate_period(period),
            "display": display,
            "aria_display": aria_display,
            "value": str(value),
            "negative": value < 0,
            "height": max(3, int(abs(value) / scale * bar_extent)) if value else 0,
            "current": index == len(parsed) - 1,
        })
    pairs = "; ".join(f"{point['period']}: {point['aria_display']}" for point in points)
    aria = f"{title}"
    if scope:
        aria += f", {scope}"
    aria += f": {pairs}; zero baseline."
    return {
        "title": title,
        "scope": scope,
        "unit": unit,
        "source_ids": source_refs,
        "signed": signed,
        "points": points,
        "aria_label": aria,
    }, ""


def _metric_groups(
    metrics: list[dict[str, Any]],
    overlay: Mapping[str, Any] | None,
    ticker: str,
    current_period: str,
    excluded_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    excluded_metrics = (
        _parse_excluded_metrics(overlay.get("excluded_metrics") if overlay else None, ticker, len(metrics))
        if excluded_metrics is None else excluded_metrics
    )
    excluded_indices = {item["metric_index"] for item in excluded_metrics}
    raw_groups = overlay.get("metric_groups") if overlay else None
    if raw_groups is not None:
        if not isinstance(raw_groups, list):
            raise ValueError(f"{ticker}: metric_groups must be a list")
        groups: list[dict[str, Any]] = []
        seen: list[int] = []
        for group_index, raw_group in enumerate(raw_groups, start=1):
            if not isinstance(raw_group, Mapping):
                raise ValueError(f"{ticker}: metric group {group_index} is not an object")
            rows = raw_group.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"{ticker}: metric group {group_index} needs rows")
            group_rows = []
            for row_index, raw_row in enumerate(rows, start=1):
                if not isinstance(raw_row, Mapping) or isinstance(raw_row.get("metric_index"), bool):
                    raise ValueError(f"{ticker}: metric group {group_index} row {row_index} is invalid")
                metric_index = _metric_index(
                    raw_row.get("metric_index"),
                    f"{ticker}: metric group {group_index} row {row_index}",
                )
                if metric_index < 0 or metric_index >= len(metrics) or metric_index in seen:
                    raise ValueError(f"{ticker}: metric index {metric_index} is out of range or duplicated")
                if metric_index in excluded_indices:
                    raise ValueError(f"{ticker}: metric index {metric_index} is both visible and excluded")
                seen.append(metric_index)
                item = dict(metrics[metric_index])
                item["metric_index"] = metric_index
                if raw_row.get("label") not in (None, ""):
                    item["label"] = _financial_display(raw_row["label"])
                unit = _display(raw_row.get("unit", ""))
                item["unit"] = unit or item.get("unit", "")
                item["previous_display"] = _numeric_display(item["previous_raw"], item["unit"])
                item["current_display"] = _numeric_display(item["current_raw"], item["unit"])
                group_rows.append(item)
            groups.append({
                "key": f"{ticker}-metrics-{group_index}",
                "title": _financial_display(raw_group.get("title") or "Financial figures"),
                "scope": _financial_display(raw_group.get("scope", "")),
                "previous_label": _display(raw_group.get("previous_label") or group_rows[0]["previous_label"]),
                "current_label": _display(raw_group.get("current_label") or current_period),
                "rows": group_rows,
            })
        expected = set(range(len(metrics))) - excluded_indices
        if set(seen) != expected:
            raise ValueError(
                f"{ticker}: metric_groups must cover every canonical metric exactly once or explicitly exclude it"
            )
        return groups

    groups = []
    for metric in metrics:
        if metric["metric_index"] in excluded_indices:
            continue
        previous_label = metric["previous_label"]
        if groups and groups[-1]["previous_label"] == previous_label:
            groups[-1]["rows"].append(metric)
        else:
            groups.append({
                "key": f"{ticker}-metrics-{len(groups) + 1}",
                "title": "Financial figures",
                "scope": "",
                "previous_label": previous_label,
                "current_label": current_period,
                "rows": [metric],
            })
    return groups


def _overlay_text(value: Any, fallback: str, label: str) -> str:
    if value in (None, ""):
        return _financial_display(fallback)
    text = _financial_display(value)
    if not text:
        raise ValueError(f"{label} cannot be empty")
    return text


def _review(raw: Any, ticker: str, prefix: bool, overlay: Mapping[str, Any] | None, index: int) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{ticker}: review.json must contain an object")
    review_ticker = _as_ticker(raw.get("ticker"))
    if review_ticker != ticker:
        raise ValueError(f"{ticker}: review.json ticker is {review_ticker}, not {ticker}")
    overlay = overlay or {}
    brief_kind = overlay.get('brief_kind', 'analysis')
    if brief_kind not in {'analysis', 'coverage_note'}:
        raise ValueError(f'{ticker}: unknown brief_kind')
    omitted_sections = _parse_omit_sections(overlay.get("omit_sections"), ticker)
    omitted_section_names = {item["section"] for item in omitted_sections}
    name = _display(raw.get("name", ""))
    period = _display(raw.get("period", ""))
    call_date = _display(raw.get("call_date", ""))
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", call_date):
        call_date = date.fromisoformat(call_date).strftime("%b %d, %Y").replace(" 0", " ")
    canonical_headline = _display(raw.get("headline", ""))
    canonical_deck = _display(raw.get("summary", ""))
    if not all((name, period, call_date, canonical_headline, canonical_deck)):
        raise ValueError(f"{ticker}: name, period, call_date, headline, and summary are required")

    source_labels = overlay.get("source_labels")
    if source_labels is not None and not isinstance(source_labels, Mapping):
        raise ValueError(f"{ticker}: source_labels must be an object")
    sources, raw_to_display = _source_records(raw.get("sources"), ticker, prefix, source_labels)
    ref_labels = {item["display_id"]: item["ref"] for item in sources}
    ref_urls = {item["display_id"]: item["url"] for item in sources}
    summary_refs = _references(raw.get("summary_sources", []), f"{ticker} summary", raw_to_display)

    headline = _overlay_text(overlay.get("headline"), canonical_headline, f"{ticker} headline")
    deck_obj = overlay.get("deck")
    if deck_obj is not None and not isinstance(deck_obj, Mapping):
        raise ValueError(f"{ticker}: deck must be an object")
    deck_obj = deck_obj or {}
    deck = _overlay_text(deck_obj.get("text"), canonical_deck, f"{ticker} deck")
    deck_refs = _references(deck_obj.get("sources", raw.get("summary_sources", [])), f"{ticker} deck", raw_to_display)
    if "deck" in omitted_section_names:
        deck, deck_refs = "", []

    metrics_raw = raw.get("metrics", [])
    if not isinstance(metrics_raw, list):
        raise ValueError(f"{ticker}: metrics must be a list")
    metrics: list[dict[str, Any]] = []
    highlights = {"green": 0, "red": 0}
    for metric_index, item in enumerate(metrics_raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"{ticker}: metric {metric_index + 1} is not an object")
        label = _financial_display(item.get("label", ""))
        current_raw = _display(item.get("current", ""))
        previous_raw = _display(item.get("previous", ""))
        previous_label = _display(item.get("previous_label", "")) or "Prior period"
        if not label or not current_raw or not previous_raw:
            raise ValueError(f"{ticker}: metric {metric_index + 1} needs label, current, and previous")
        refs = _references(item.get("sources", []), f"{ticker} metric {metric_index + 1}", raw_to_display)
        highlight = str(item.get("highlight", "") or "").strip().lower()
        if highlight not in {"", "green", "red"}:
            raise ValueError(f"{ticker}: metric {metric_index + 1} has invalid highlight {highlight!r}")
        if highlight:
            highlights[highlight] += 1
            if highlights[highlight] > 1:
                raise ValueError(f"{ticker}: at most one {highlight} metric is allowed")
            if not refs:
                raise ValueError(f"{ticker}: highlighted metric {metric_index + 1} needs source support")
        unit = _infer_unit(current_raw)
        metrics.append({
            "metric_index": metric_index,
            "label": label,
            "unit": unit,
            "current_raw": current_raw,
            "previous_raw": previous_raw,
            "current_display": _numeric_display(current_raw, unit),
            "previous_display": _numeric_display(previous_raw, unit),
            "previous_label": previous_label,
            "source_ids": refs,
            "highlight": highlight,
        })
    excluded_metrics = _parse_excluded_metrics(overlay.get("excluded_metrics"), ticker, len(metrics))
    groups = _metric_groups(metrics, overlay, ticker, period, excluded_metrics)

    raw_chart = raw.get("chart")
    chart, chart_note = _chart(raw_chart, overlay.get("chart"), ticker, raw_to_display)

    canonical_analysis = raw.get("analysis", [])
    if not isinstance(canonical_analysis, list):
        raise ValueError(f"{ticker}: analysis must be a list")
    for item in canonical_analysis:
        if not isinstance(item, Mapping):
            raise ValueError(f"{ticker}: analysis item is not an object")
    raw_insights = overlay.get("insights")
    if raw_insights is not None and not isinstance(raw_insights, list):
        raise ValueError(f"{ticker}: insights must be a list")
    if "insights" in omitted_section_names:
        insights = []
    elif raw_insights is not None:
        insights = []
        for insight_index, item in enumerate(raw_insights, start=1):
            if not isinstance(item, Mapping):
                raise ValueError(f"{ticker}: insight {insight_index} is not an object")
            text = _overlay_text(item.get("text"), "", f"{ticker} insight {insight_index}")
            refs = _references(item.get("sources", []), f"{ticker} insight {insight_index}", raw_to_display)
            insights.append({"label": _financial_display(item.get("label", "")), "text": text, "source_ids": refs})
    else:
        insights = [{"label": _financial_display(item.get("label", "")), "text": _financial_display(item.get("text", "")), "source_ids": _references(item.get("sources", []), f"{ticker} analysis", raw_to_display)} for item in canonical_analysis[:2]]

    context_obj = overlay.get("servicing_context")
    if context_obj is not None and not isinstance(context_obj, Mapping):
        raise ValueError(f"{ticker}: servicing_context must be an object")
    context_obj = context_obj or {}
    if context_obj.get("text") not in (None, ""):
        context_text = _overlay_text(context_obj.get("text"), "", f"{ticker} servicing_context")
        context_refs = _references(context_obj.get("sources", []), f"{ticker} servicing_context", raw_to_display)
    elif canonical_analysis:
        context_text = _financial_display(canonical_analysis[0].get("text", ""))
        context_refs = _references(canonical_analysis[0].get("sources", []), f"{ticker} analysis", raw_to_display)
    else:
        context_text, context_refs = "", []
    if "servicing_context" in omitted_section_names:
        context_text, context_refs = "", []

    call_obj = overlay.get("call")
    if call_obj is not None and not isinstance(call_obj, Mapping):
        raise ValueError(f"{ticker}: call must be an object")
    call_obj = call_obj or {}
    if "text" in call_obj and call_obj.get("text") in (None, ""):
        call_text = ""
    else:
        call_text = _overlay_text(call_obj.get("text"), _display(raw.get("call_note", "")), f"{ticker} call")
    call_refs = _references(call_obj.get("sources", raw.get("call_note_sources", [])), f"{ticker} call", raw_to_display)
    def editorial_items(items, section):
        if not isinstance(items, list):
            raise ValueError(f"{ticker}: {section} must be a list")
        result = []
        for item in items:
            if not isinstance(item, Mapping):
                raise ValueError(f"{ticker}: {section} item must be an object")
            result.append({"label": _financial_display(item.get("label", "")),
                           "text": _overlay_text(item.get("text"), "", f"{ticker} {section}"),
                           "source_ids": _references(item.get("sources", []), f"{ticker} {section}", raw_to_display)})
        return result
    call_findings = editorial_items(call_obj["findings"], "call findings") if "findings" in call_obj else ([{"label": "", "text": call_text, "source_ids": call_refs}] if call_text else [])
    call_text = " ".join(item["text"] for item in call_findings)
    call_refs = list(dict.fromkeys(ref for item in call_findings for ref in item["source_ids"]))
    implications = editorial_items(overlay.get("implications", []), "implications")
    call_availability = _financial_display(call_obj.get("availability", ""))
    if call_availability:
        call_availability_refs = _references(call_obj.get("availability_sources", call_obj.get("sources", [])), f"{ticker} call availability", raw_to_display)
    else:
        call_availability_refs = []
    question = _overlay_text(overlay.get("question"), _display(raw.get("investor_question", "")), f"{ticker} question")
    if "question" in omitted_section_names:
        question = ""
    display_name = _overlay_text(overlay.get("display_name"), name, f"{ticker} display_name")

    event_label = "Release & call" if raw.get("release_date") == raw.get("call_date") else "Call"
    # The evidence archive stays complete; the reader's source list may omit
    # unused documents after an explicit editorial decision.
    excluded_sources = overlay.get("excluded_sources", [])
    if not isinstance(excluded_sources, list):
        raise ValueError(f"{ticker}: excluded_sources must be a list")
    used_refs = set(deck_refs if deck else []) | set(context_refs if context_text else [])
    used_refs.update(call_refs if call_text else [])
    used_refs.update(call_availability_refs)
    for group in groups:
        for row in group["rows"]:
            used_refs.update(row["source_ids"])
    for insight in insights:
        used_refs.update(insight["source_ids"])
    for item in implications:
        used_refs.update(item["source_ids"])
    if chart:
        used_refs.update(chart["source_ids"])
    excluded_ids = set()
    for item in excluded_sources:
        if not isinstance(item, Mapping):
            raise ValueError(f"{ticker}: excluded source must be an object")
        source_id = str(item.get("source_id", ""))
        if source_id not in raw_to_display or source_id in excluded_ids:
            raise ValueError(f"{ticker}: unknown or duplicate excluded source {source_id}")
        _meaningful_reason(item.get("reason"), f"{ticker}: excluded source {source_id}")
        if raw_to_display[source_id] in used_refs:
            raise ValueError(f"{ticker}: excluded source {source_id} still supports visible content")
        excluded_ids.add(source_id)
    sources = [source for source in sources if source["id"] not in excluded_ids]
    if brief_kind == 'coverage_note' and (chart or groups or insights or call_text or implications or question):
        raise ValueError(f'{ticker}: a coverage note cannot carry charts, metrics, insights, calls or investor questions')
    return {
        "brief_kind": brief_kind,
        "company_identity": {"ticker": ticker, "cik": normalize_cik(raw.get("cik")), "event": canonical_event(period), "kind": "earnings_brief"},
        "brand": brand_view(ticker, display_name),
        "index": index,
        "ticker": ticker,
        "name": display_name,
        "period": period,
        "call_date": call_date,
        "event_label": event_label,
        "headline": headline,
        "deck": deck,
        "deck_source_ids": deck_refs,
        "metrics": metrics,
        "excluded_metrics": excluded_metrics,
        "metric_groups": groups,
        "chart": chart,
        "chart_note": chart_note,
        "insights": insights,
        "omitted_sections": omitted_sections,
        "servicing_context": {"text": context_text, "source_ids": context_refs},
        "call": {"text": call_text, "findings": call_findings, "source_ids": call_refs, "availability": call_availability, "availability_source_ids": call_availability_refs},
        "implications": implications,
        "question": question,
        "sources": sources,
        "excluded_sources": excluded_sources,
        "ref_labels": ref_labels,
        "ref_urls": ref_urls,
    }


def _universe_note(metadata: Mapping[str, Any]) -> str:
    reader_note = metadata.get("reader_note")
    if isinstance(reader_note, str) and reader_note.strip():
        return _display(reader_note)
    universe = metadata.get("universe")
    count = universe.get("count") if isinstance(universe, Mapping) else None
    count_text = str(count) if isinstance(count, int) else "the validated"
    as_of = _display(metadata.get("as_of") or metadata.get("as_of_date") or "2026-09-05")
    try:
        as_of = date.fromisoformat(as_of[:10]).strftime("%B %d, %Y").replace(" 0", " ")
    except ValueError:
        pass
    return f"Five most recent completed calls among {count_text} validated companies as of {as_of}. Same-day calls use verified times first, then alphabetical ticker order when times are unavailable or not comparable."


def _refs_text(refs: list[str], labels: Mapping[str, str]) -> str:
    if not refs:
        return ""
    return " " + " ".join(f"[{labels[ref]}]" for ref in refs)


def _render_text(companies: list[dict[str, Any]], *, combined: bool, universe_note: str, attachment_note: str) -> str:
    if not combined and len(companies) != 1:
        raise ValueError("A standalone brief must contain exactly one company")
    lines: list[str] = []
    if combined:
        lines.extend(["The Servicing Brief", "September 5, 2026", "", "Index: " + "   ".join(f"{c['index']:02d}/{c['ticker']}" for c in companies), ""])
    else:
        lines.extend(["The Servicing Brief", "September 5, 2026", ""])
    for idx, company in enumerate(companies):
        if idx:
            lines.extend(["", "=" * 56, ""])
        labels = company["ref_labels"]
        if company['brief_kind'] == 'coverage_note':
            lines.append('Coverage note')
        lines.extend([f"Company: {company['name']} ({company['ticker']})", f"Earnings period: {company['period']}", f"{company['event_label']} {company['call_date']}", "", company["headline"], company["deck"] + _refs_text(company["deck_source_ids"], labels)])
        for finding in company["call"]["findings"]:
            lines.extend(["", (finding["label"] + ": " if finding["label"] else "") + finding["text"] + _refs_text(finding["source_ids"], labels)])
        for insight in company["insights"]:
            if insight["text"]:
                prefix = f"{insight['label']}: " if insight["label"] else ""
                lines.extend(["", prefix + insight["text"] + _refs_text(insight["source_ids"], labels)])
        if company["chart"]:
            chart = company["chart"]
            lines.extend(["", chart["title"] + (f" ({chart['unit']})" if chart["unit"] else "") + _refs_text(chart["source_ids"], labels)])
            if chart["scope"]:
                lines.append(chart["scope"])
            lines.extend(f"{point['period']}: {point['display']}" + (" (current)" if point["current"] else "") for point in chart["points"])
            lines.append("Zero baseline; older period left, newer period right.")
        for group in company["metric_groups"]:
            lines.extend(["", group["title"]])
            if group["scope"]:
                lines.append(group["scope"])
            lines.append(f"Metric | {group['previous_label']} | {group['current_label']}")
            for row in group["rows"]:
                unit = f" ({row['unit']})" if row["unit"] else ""
                lines.append(f"{row['label']}{unit} | {row['previous_display']} | {row['current_display']}" + _refs_text(row["source_ids"], labels))
        if company["servicing_context"]["text"]:
            lines.extend(["", "Servicing context", company["servicing_context"]["text"] + _refs_text(company["servicing_context"]["source_ids"], labels)])
        for item in company["implications"]:
            lines.extend(["", "Analyst interpretation" + (": " + item["label"] if item["label"] else ""), item["text"] + _refs_text(item["source_ids"], labels)])
        if company["question"]:
            lines.extend(["", "Investor question", company["question"]])
        lines.extend(["", "Sources"])
        if company["call"]["availability"]:
            lines.append("Call availability: " + company["call"]["availability"] + _refs_text(company["call"]["availability_source_ids"], labels))
        for source in company["sources"]:
            lines.append(f"[{source['ref']}] {source['short_label']}")
            lines.append(source["url"])
    if attachment_note:
        lines.extend(["", "Attachment note", attachment_note])
    if combined:
        lines.extend(["", "Universe note", universe_note])
    rendered = "\n".join(lines).rstrip() + "\n"
    assert_reader_content("", rendered)
    return rendered


def _compact_rendered(html: str) -> str:
    """Compact generated email markup while retaining inline mobile defaults."""
    def compact_css(match: re.Match[str]) -> str:
        css = re.sub(r"/\*.*?\*/", "", match.group(2), flags=re.S)
        css = re.sub(r"\s+", " ", css).strip()
        css = re.sub(r"\s*([{}:;,>])\s*", r"\1", css)
        return match.group(1) + css + match.group(3)

    html = re.sub(r"(<style[^>]*>)(.*?)(</style>)", compact_css, html, flags=re.I | re.S)

    def compact_tag(match: re.Match[str]) -> str:
        tag = match.group(0)
        if not re.search(r"\sstyle=\"", tag, re.I):
            return tag
        # The body/shell/wrap carry the fallback font for clients that strip
        # style blocks.  Descendants inherit it, so their repeated family
        # declarations can be removed safely.
        keep_font = bool(re.match(r"<body\b", tag, re.I) or re.search(r'class=\"(?:email-shell|email-wrap)\"', tag, re.I))
        if keep_font:
            return tag
        def compact_style(style_match: re.Match[str]) -> str:
            style = style_match.group(1)
            style = re.sub(r"font-family:'Segoe UI',Arial,Helvetica,sans-serif;?", "", style, flags=re.I)
            style = re.sub(r"\s+", " ", style).strip()
            return 'style="' + style + '"'
        return re.sub(r'style="([^"]*)"', compact_style, tag, flags=re.I)

    return re.sub(r"<[^>]+>", compact_tag, html)


def _render_html(env: Environment, companies: list[dict[str, Any]], *, combined: bool, universe_note: str, attachment_note: str, full_document: bool) -> str:
    if not combined and len(companies) != 1:
        raise ValueError("A standalone brief must contain exactly one company")
    document_title = "Mortgage servicing: five recent calls" if combined else f"{companies[0]['name']} servicing review"
    rendered = env.get_template("email-template.html.j2").render(
        companies=companies,
        combined=combined,
        universe_note=universe_note,
        attachment_note=attachment_note,
        full_document=full_document,
        document_title=document_title,
    )
    rendered = re.sub(r">\s+<", "><", rendered).strip()
    rendered = _compact_rendered(rendered)
    assert_reader_content(rendered, '')
    if full_document and not combined:
        validate_page_theme(rendered, companies[0]['ticker'])
    return rendered


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the authorized one-off five-company review email")
    parser.add_argument("--tickers", default="", help="Ordered comma-separated tickers when selection.json is absent")
    parser.add_argument("--selection", type=Path, help="Selection manifest; defaults to review-root/selection.json")
    parser.add_argument("--editorial", type=Path, help="Optional version 1 editorial overlay; defaults to review-root/editorial.json")
    parser.add_argument("--review-root", type=Path, help="Folder containing selection.json and <TICKER>/review.json")
    parser.add_argument("--output-dir", type=Path, help="Artifact output folder; defaults to review-root")
    parser.add_argument("--attachment-note", default="", help="Optional explicit attachment omission/supply note")
    parser.add_argument("--legacy-archive", action="store_true", help="Explicitly reproduce the historical combined archive; normal output is one company per brief")
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    review_root = (args.review_root or script_dir).resolve()
    output_dir = (args.output_dir or review_root).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    tickers, metadata = _read_selection(review_root, args.selection.resolve() if args.selection else None, args.tickers)
    overlays = _read_editorial(review_root, args.editorial.resolve() if args.editorial else None)
    universe_note = _universe_note(metadata)
    attachment_note = _display(args.attachment_note or metadata.get("attachment_note", ""))
    raw_reviews = {ticker: _load_json(review_root / ticker / "review.json") for ticker in tickers}

    env = Environment(loader=FileSystemLoader([script_dir, script_dir.parents[1] / 'servicing_brief/templates']), autoescape=True, trim_blocks=True, lstrip_blocks=True)
    combined_companies = [_review(raw_reviews[ticker], ticker, True, overlays.get(ticker), index) for index, ticker in enumerate(tickers, start=1)]
    (output_dir / 'branding-status.json').write_text(json.dumps({c['ticker']: {key: c['brand'][key] for key in ('verified', 'gap_reason', 'primary_color', 'public_logo_url')} for c in combined_companies}, indent=2), encoding='utf8')
    generated = []
    if args.legacy_archive:
        (output_dir / "combined-email.html").write_text(_render_html(env, combined_companies, combined=True, universe_note=universe_note, attachment_note=attachment_note, full_document=True), encoding="utf-8")
        (output_dir / "combined-email.txt").write_text(_render_text(combined_companies, combined=True, universe_note=universe_note, attachment_note=attachment_note), encoding="utf-8")
        (output_dir / "gmail-body.html").write_text(_render_html(env, combined_companies, combined=True, universe_note=universe_note, attachment_note=attachment_note, full_document=False), encoding="utf-8")
        generated.extend(["combined-email.html", "combined-email.txt", "gmail-body.html"])
    for index, ticker in enumerate(tickers, start=1):
        company = _review(raw_reviews[ticker], ticker, False, overlays.get(ticker), index)
        html_name = f"{ticker}-review.html"
        txt_name = f"{ticker}-review.txt"
        (output_dir / html_name).write_text(_render_html(env, [company], combined=False, universe_note=universe_note, attachment_note=attachment_note, full_document=True), encoding="utf-8")
        (output_dir / txt_name).write_text(_render_text([company], combined=False, universe_note=universe_note, attachment_note=attachment_note), encoding="utf-8")
        generated.extend([html_name, txt_name])
    print(json.dumps({"tickers": tickers, "files": generated, "output_dir": str(output_dir)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as exc:
        raise SystemExit(f"render_email: {exc}")
