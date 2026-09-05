"""Release-boundary adapters for the P0 human reader-value review.

Drafts remain available for review. A missing, stale or unresolved review cannot
be promoted to a deliverable. Audit reasons are never inserted into UI copy.
"""
import json
import re
from html import escape
from pathlib import Path
from .reader_value import evaluate_reader_value, require_reader_value
from .company_boundary import CompanyBoundaryError, has_company_identity, require_company_boundary


def text_review_html(text: str, *, document_kind: str = 'analysis') -> str:
    """Inventory the plain-text alternative without interpreting its markup."""
    if document_kind not in {'analysis', 'coverage_note'}:
        raise ValueError('Unknown document kind for plain-text review')
    references = dict(re.findall(r'\[(\d+)\][^\n]*\n(https://[^\s]+)', text)) if document_kind == 'coverage_note' else {}
    lines = []
    for line in text.splitlines():
        if not line.strip():
            continue
        safe = escape(line)
        if references:
            safe = re.sub(r'\[(\d+)\]', lambda m: f'<a href="{escape(references[m[1]], quote=True)}">{m[0]}</a>' if m[1] in references else m[0], safe)
        lines.append(f'<p>{safe}</p>')
    body = ''.join(lines)
    if document_kind == 'coverage_note':
        body = '<section class="company-section" data-brief-kind="coverage_note">' + body + '</section>'
    return '<body>' + body + '</body>'


def review_file(html_path: Path, review_path: Path):
    html = html_path.read_text(encoding='utf8')
    review = json.loads(review_path.read_text(encoding='utf8')) if review_path.exists() else None
    return require_reader_value(html, review)


def require_five_company_release(root: Path):
    tickers = json.loads((root/'selection.json').read_text(encoding='utf8'))['selected_tickers']
    if (not isinstance(tickers, list) or len(tickers) != 5
            or not all(isinstance(ticker, str) and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', ticker) for ticker in tickers)
            or len(set(tickers)) != 5):
        raise ValueError('P0 reader-value release HOLD: selection must contain exactly five unique valid tickers.')
    results = {}
    failures = []
    # Every output is bound separately: an approved single-company file cannot
    # approve a changed email or additional unreviewed material in compilation.
    for stem in [*[f'{t}-review' for t in tickers], 'combined-email', 'gmail-body']:
        try:
            results[stem] = review_file(root/f'{stem}.html', root/'reader-value-reviews'/f'{stem}.json')
            text_path = root/f'{stem}.txt'
            if text_path.exists():
                record_path = root/'reader-value-reviews'/f'{stem}-text.json'
                record = json.loads(record_path.read_text(encoding='utf8')) if record_path.exists() else None
                results[f'{stem}-text'] = require_reader_value(text_review_html(text_path.read_text(encoding='utf8'), document_kind=(record or {}).get('document_kind','analysis')), record)
        except ValueError as exc:
            failures.append(f'{stem}: {exc}')
    if failures:
        raise ValueError('P0 reader-value release HOLD. ' + '\n'.join(failures))
    return results


def refresh_report_review(report: dict, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    path = output/'reader-value-review.json'
    review = json.loads(path.read_text(encoding='utf8')) if path.exists() else None
    result = evaluate_reader_value(report['html'], review)
    text_path = output/'reader-value-text-review.json'
    text_review = json.loads(text_path.read_text(encoding='utf8')) if text_path.exists() else None
    text_result = evaluate_reader_value(text_review_html(str(report.get('text', '')), document_kind=(text_review or {}).get('document_kind','analysis')), text_review)
    result['text_review'] = text_result
    if has_company_identity(report):
        try:
            # Source-document matching is performed before report creation by
            # the pipeline/delivery boundary.  The release gate still checks
            # the persisted artifact itself so an approved review cannot be
            # reused for a combined or identity-less replacement.
            boundary = require_company_boundary(report, (), require_surfaces=True)
            result['company_boundary'] = boundary
        except CompanyBoundaryError as exc:
            result['status'] = 'blocked'
            result.setdefault('blockers', []).insert(0, f'P0 company-boundary release HOLD: {exc}')
            text_result['status'] = 'blocked'
            text_result.setdefault('blockers', []).insert(0, f'P0 company-boundary release HOLD: {exc}')
            result['company_boundary'] = {'status': 'blocked', 'detail': str(exc)}
    if text_result['status'] != 'approved':
        result['status'] = 'blocked'
        result['blockers'].extend(f'Plain-text alternative: {reason}' for reason in text_result['blockers'])
    (output/'reader-value-status.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    report['reader_value'] = {key: result[key] for key in ('status', 'html_sha256')}
    report['reader_value']['text_sha256'] = text_result['html_sha256']
    return result


if __name__ == '__main__':
    import sys
    try:
        require_five_company_release(Path(sys.argv[1]).resolve())
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    print('P0 reader-value release approved.')
