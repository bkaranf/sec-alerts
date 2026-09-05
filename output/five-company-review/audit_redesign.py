"""Independently check editorial scope, canonical preservation and rendered numbers."""
from pathlib import Path
from decimal import Decimal
import hashlib
import json
import re
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent

_OMIT_SECTIONS = {"deck", "question", "servicing_context", "insights"}
_GENERIC_OMISSION_REASONS = {
    "na", "none", "omit", "omitted", "skip", "skipped", "tbd", "todo", "reason"
}


def _meaningful_reason(value, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} needs a meaningful reason")
    reason = re.sub(r"\s*\u2014\s*", ": ", value).strip()
    compact = re.sub(r"[^a-z0-9]+", "", reason.casefold())
    if len(compact) < 8 or not re.search(r"[a-z]", compact) or compact in _GENERIC_OMISSION_REASONS:
        raise ValueError(f"{label} needs a meaningful reason")
    return reason


def _metric_index(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} needs an integer metric_index")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value.strip())
    raise ValueError(f"{label} needs an integer metric_index")


def _metric_partition(edit: dict, ticker: str, metric_count: int) -> tuple[set[int], list[dict[str, object]]]:
    raw_exclusions = edit.get("excluded_metrics")
    if raw_exclusions is None:
        raw_exclusions = []
    if not isinstance(raw_exclusions, list):
        raise ValueError(f"{ticker}: excluded_metrics must be a list")
    exclusions: list[dict[str, object]] = []
    excluded: set[int] = set()
    for item_index, item in enumerate(raw_exclusions, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{ticker}: excluded metric {item_index} is not an object")
        metric_index = _metric_index(item.get("metric_index"), f"{ticker}: excluded metric {item_index}")
        if metric_index < 0 or metric_index >= metric_count:
            raise ValueError(f"{ticker}: excluded metric index {metric_index} is unknown")
        if metric_index in excluded:
            raise ValueError(f"{ticker}: excluded metric index {metric_index} is duplicated")
        excluded.add(metric_index)
        exclusions.append({
            "metric_index": metric_index,
            "reason": _meaningful_reason(item.get("reason"), f"{ticker}: excluded metric {metric_index}"),
        })

    raw_groups = edit.get("metric_groups")
    if raw_groups is None:
        visible = set(range(metric_count)) - excluded
    else:
        if not isinstance(raw_groups, list):
            raise ValueError(f"{ticker}: metric_groups must be a list")
        visible: set[int] = set()
        for group_index, group in enumerate(raw_groups, start=1):
            if not isinstance(group, dict):
                raise ValueError(f"{ticker}: metric group {group_index} is not an object")
            rows = group.get("rows")
            if not isinstance(rows, list) or not rows:
                raise ValueError(f"{ticker}: metric group {group_index} needs rows")
            for row_index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    raise ValueError(f"{ticker}: metric group {group_index} row {row_index} is invalid")
                metric_index = _metric_index(
                    row.get("metric_index"),
                    f"{ticker}: metric group {group_index} row {row_index}",
                )
                if metric_index < 0 or metric_index >= metric_count or metric_index in visible:
                    raise ValueError(f"{ticker}: metric index {metric_index} is out of range or duplicated")
                if metric_index in excluded:
                    raise ValueError(f"{ticker}: metric index {metric_index} is both visible and excluded")
                visible.add(metric_index)
    expected = set(range(metric_count))
    if visible & excluded or visible | excluded != expected:
        raise ValueError(
            f"{ticker}: metric_groups must cover every canonical metric exactly once or explicitly exclude it"
        )
    return visible, exclusions


def _omitted_sections(edit: dict, ticker: str) -> list[dict[str, str]]:
    raw_sections = edit.get("omit_sections")
    if raw_sections is None:
        return []
    if not isinstance(raw_sections, list):
        raise ValueError(f"{ticker}: omit_sections must be a list")
    sections: list[dict[str, str]] = []
    seen: set[str] = set()
    for item_index, item in enumerate(raw_sections, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{ticker}: omitted section {item_index} is not an object")
        section = str(item.get("section", "")).strip()
        if section not in _OMIT_SECTIONS:
            raise ValueError(f"{ticker}: omitted section {section!r} is not supported")
        if section in seen:
            raise ValueError(f"{ticker}: omitted section {section!r} is duplicated")
        seen.add(section)
        sections.append({
            "section": section,
            "reason": _meaningful_reason(item.get("reason"), f"{ticker}: omitted section {section}"),
        })
    return sections


tickers = json.loads((ROOT / 'selection.json').read_text(encoding='utf8'))['selected_tickers']
baseline = json.loads((ROOT / 'redesign-baseline.json').read_text(encoding='utf8'))
for rel, expected in baseline['canonical_files'].items():
    assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == expected, f'Canonical file changed: {rel}'
overlay = json.loads((ROOT / 'editorial.json').read_text(encoding='utf8'))
assert overlay['version'] == 1 and set(overlay['companies']) == set(tickers)
assert '\u2014' not in json.dumps(overlay, ensure_ascii=False)
html = BeautifulSoup((ROOT / 'combined-email.html').read_text(encoding='utf8'), 'html.parser')
word_counts = {}
checked_cells = 0
excluded_by_ticker = {}
omitted_sections_by_ticker = {}
chart_omissions_by_ticker = {}

def word_count(value):
    return len(value.split())

def canonical_value(value):
    text = value.replace('CAD', '').replace('C$', '').replace('$', '').replace(',', '').strip()
    match = re.fullmatch(r'([+\-\u2212]?[\d.]+)\s*(bn|m|%)?', text)
    assert match, f'Unrecognized canonical numeric text: {value}'
    number = Decimal(match[1].replace('\u2212', '-'))
    return number * {'bn': Decimal('1000000000'), 'm': Decimal('1000000'), '%': Decimal('1'), None: Decimal('1')}[match[2]]

def numeric_text(value):
    return Decimal(value.replace(',', '').replace('%', '').replace('\u2212', '-').strip())

for ticker in tickers:
    raw = json.loads((ROOT / ticker / 'review.json').read_text(encoding='utf8'))
    edit = overlay['companies'][ticker]
    ids = {s['id'] for s in raw['sources']}
    visible_indexes, exclusions = _metric_partition(edit, ticker, len(raw['metrics']))
    excluded_by_ticker[ticker] = exclusions
    omitted_sections = _omitted_sections(edit, ticker)
    omitted_names = {item['section'] for item in omitted_sections}
    omitted_sections_by_ticker[ticker] = omitted_sections
    chart_config = edit.get('chart')
    if chart_config is not None and not isinstance(chart_config, dict):
        raise ValueError(f"{ticker}: editorial chart must be an object")
    if chart_config and 'omit' in chart_config and not isinstance(chart_config['omit'], bool):
        raise ValueError(f"{ticker}: editorial chart omit must be true or false")
    if chart_config and chart_config.get('omit') is True:
        if raw.get('chart') is None or not isinstance(raw.get('chart'), dict):
            raise ValueError(f"{ticker}: cannot omit a chart without canonical chart values")
        chart_omissions_by_ticker[ticker] = _meaningful_reason(
            chart_config.get('reason'), f"{ticker}: chart omission"
        )
    checks = [('headline', edit['headline'], 12)]
    if 'deck' not in omitted_names:
        checks.append(('deck', edit.get('deck', {}).get('text', ''), 35))
    if 'servicing_context' not in omitted_names:
        checks.append(('servicing_context', edit.get('servicing_context', {}).get('text', ''), 60))
    checks.append(('call', edit.get('call', {}).get('text', ''), 35))
    if 'question' not in omitted_names:
        checks.append(('question', edit.get('question', ''), 25))
    insights = edit.get('insights', [])
    if not isinstance(insights, list):
        raise ValueError(f"{ticker}: insights must be a list")
    if 'insights' not in omitted_names:
        checks.extend((f'insight_{i}', x.get('text', ''), 35) for i,x in enumerate(insights))
    word_counts[ticker] = {name: word_count(value) for name,value,limit in checks}
    for name,value,limit in checks:
        assert word_count(value) <= limit, f'{ticker} {name} exceeds {limit} words'
    blocks = []
    if 'deck' not in omitted_names:
        blocks.append(edit['deck'])
    if 'servicing_context' not in omitted_names:
        blocks.append(edit['servicing_context'])
    blocks.append(edit['call'])
    if 'insights' not in omitted_names:
        blocks.extend(insights)
    for block in blocks:
        assert block['sources'] and set(block['sources']).issubset(ids)
    assert set(edit['source_labels']) == ids
    section = html.select_one(f'.company-section[data-ticker="{ticker}"]')
    assert section is not None
    tables = section.select('.financial-table')
    assert len(tables) == len(edit['metric_groups'])
    for group, table in zip(edit['metric_groups'], tables):
        headers = table.select('thead th')
        assert group['previous_label'] in headers[1].get_text(' ', strip=True)
        assert group['current_label'] in headers[2].get_text(' ', strip=True)
        for row in group['rows']:
            rendered = table.select_one(f'tr[data-metric-index="{row["metric_index"]}"]')
            assert rendered is not None
            metric = raw['metrics'][row['metric_index']]
            divisor = {'C$bn':Decimal('1000000000'), 'C$m':Decimal('1000000'), '%':Decimal('1')}[row['unit']]
            for which in ['previous','current']:
                cell = rendered.select_one(f'.{which}-value')
                assert cell is not None
                actual = numeric_text(cell.get_text(' ', strip=True))
                assert actual == canonical_value(metric[which]) / divisor, f'{ticker} metric {row["metric_index"]} {which}: numeric mismatch'
                checked_cells += 1
    for metric_index in visible_indexes:
        assert section.select_one(f'tr[data-metric-index="{metric_index}"]') is not None
    for exclusion in exclusions:
        assert section.select_one(f'tr[data-metric-index="{exclusion["metric_index"]}"]') is None
    if ticker in chart_omissions_by_ticker:
        assert section.select_one('.earnings-chart') is None
    for link in section.select('a[href]'):
        assert link['href'].startswith(('https://','#')), f'Nonpublic source link: {link["href"]}'
assert '\u2014' not in html.get_text()
result = {'passed': True, 'canonical_files_unchanged': len(baseline['canonical_files']),
          'companies': tickers, 'rendered_numeric_cells_verified': checked_cells,
          'editorial_word_counts': word_counts,
          'excluded_metrics': excluded_by_ticker,
          'omitted_sections': omitted_sections_by_ticker,
          'chart_omissions': chart_omissions_by_ticker,
          'limits': 'Mechanical evidence checks supplement direct financial and visual review.'}
(ROOT / 'redesign-validation.json').write_text(json.dumps(result, indent=2), encoding='utf8')
print(json.dumps(result))
