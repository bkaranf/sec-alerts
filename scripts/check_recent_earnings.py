"""Small SEC earnings-disclosure queue, using EdgarTools and the shared guard.

Search results prioritize document review; they never prove a call completed.
This command does not create drafts or send messages.
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.discover_servicers import collect_query_form, write_json, now_utc
from servicing_brief.sources.ratelimit import sec_acquisition_guard


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--since', required=True)
    parser.add_argument('--through', default=date.today().isoformat())
    parser.add_argument('--universe', type=Path, default=ROOT/'output/servicer-universe/candidates.json')
    parser.add_argument('--output', type=Path, default=ROOT/'output/servicer-universe/recent-earnings')
    args = parser.parse_args()
    start, end = date.fromisoformat(args.since), date.fromisoformat(args.through)
    if start > end:
        parser.error('--since must be on or before --through')
    rows = json.loads(args.universe.read_text(encoding='utf-8'))
    universe = rows.get('candidates', rows.get('companies', []))
    ciks = {str(r['cik']).zfill(10): r for r in universe}
    audit = {'started_at': now_utc(), 'since': args.since, 'through': args.through,
             'status': 'pending', 'purpose': 'Actual disclosure review queue; not verified call dates',
             'queries': ['"conference call"', '"earnings call"'], 'forms': ['8-K', '6-K'],
             'query_audits': [], 'hits': [], 'matched_companies': []}
    args.output.mkdir(parents=True, exist_ok=True)
    with sec_acquisition_guard(args.output, timeout=20) as guard:
        if not guard.get('acquired') or not guard.get('manager_verified'):
            raise SystemExit('SEC guard unavailable; retry after the active collector finishes.')
        from edgar import search_filings, set_identity
        if not os.environ.get('EDGAR_IDENTITY'):
            raise SystemExit('EDGAR_IDENTITY configured=no')
        set_identity(os.environ['EDGAR_IDENTITY'])
        try:
            for query in audit['queries']:
                for form in audit['forms']:
                    records, checks, _prior, _split = collect_query_form(search_filings, query, form, start, end, args.output/'pages', args.output/'uncertain-pages')
                    audit['hits'].extend(records)
                    audit['query_audits'].extend(checks)
            matched = {}
            for hit in audit['hits']:
                cik = str(hit.get('cik', '')).zfill(10)
                if cik in ciks:
                    original = ciks[cik]
                    row = matched.setdefault(cik, {'cik': cik, 'company': original.get('company_name', original.get('company')), 'tickers': original.get('ticker_reference', {}).get('tickers', [original.get('ticker','')]), 'hits': []})
                    row['hits'].append(hit)
            audit['matched_companies'] = list(matched.values())
            audit['status'] = 'completed' if all(c.get('pagination_complete') for c in audit['query_audits']) else 'partial'
        except Exception as exc:
            audit.update(status='error', error_type=type(exc).__name__)
        finally:
            audit['finished_at'] = now_utc()
            write_json(args.output/'queue.json', audit)
    print(json.dumps({'status': audit['status'], 'hits':len(audit['hits']), 'matched_companies': len(audit['matched_companies']), 'output':str(args.output/'queue.json')}))


if __name__ == '__main__':
    main()
