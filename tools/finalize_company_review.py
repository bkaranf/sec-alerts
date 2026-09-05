"""Bind approved reader reviews to the current company drafts. Never sends mail."""
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bs4 import BeautifulSoup
from servicing_brief.branding import validate_page_theme
from servicing_brief.company_boundary import require_company_boundary, validate_mime_message
from servicing_brief.config import load_config
from servicing_brief.delivery import prepare_messages
from servicing_brief.editorial_control import validate_source_insights
from servicing_brief.models import Document
from servicing_brief.reader_value import require_reader_value
from servicing_brief.reader_content import assert_reader_mime
from servicing_brief.reader_value_release import refresh_report_review, text_review_html

OUT = ROOT / 'output/company-briefs'
REVIEW = OUT / 'review'


def read(path):
    return json.loads(path.read_text(encoding='utf8'))


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def mime_reviews(ticker, message):
    assert_reader_mime(message)
    html = message.get_body(preferencelist=('html',)).get_content()
    text = message.get_body(preferencelist=('plain',)).get_content()
    records = [REVIEW / f'{ticker}-mime-reader-value.json', REVIEW / f'{ticker}-mime-reader-value-text.json']
    results = [require_reader_value(html, read(records[0])),
               require_reader_value(text_review_html(text), read(records[1]))]
    return (html, text), [{'path': str(p.relative_to(ROOT)), 'sha256': digest(p),
                          'status': r['status'], 'items': r['reviewed_count']}
                         for p, r in zip(records, results)]


def main():
    status = read(OUT / 'build-status.json')
    audit = read(REVIEW / 'qa/audit.json')
    assert len(status) == 6 and len(audit['results']) == 60
    calls, candidates = {}, {}
    # Validate every company before updating any package.
    for ticker, built in status.items():
        folder = OUT / ticker
        report = read(folder / 'report.json')
        documents = [Document(**d) for d in read(folder / 'documents.json')]
        for extension in ('html', 'txt'):
            file = OUT / f'{ticker}-review.{extension}'
            field = 'html_sha256' if extension == 'html' else 'text_sha256'
            assert digest(file) == built[field] == audit['hashes'][file.name], file
        require_company_boundary(report, documents)
        validate_page_theme(report['html'], ticker)
        result = refresh_report_review(report, folder)
        assert result['status'] == 'approved', (ticker, result['blockers'])
        assert len(built['messages']) == 1
        old = ROOT / built['messages'][0]['path']
        assert digest(old) == built['messages'][0]['sha256']
        parsed = BytesParser(policy=policy.default).parsebytes(old.read_bytes())
        validate_mime_message(parsed)
        bodies, mime_records = mime_reviews(ticker, parsed)
        soup = BeautifulSoup(report['html'], 'html.parser')
        for ref in soup.select('.call-text sup'):
            ref.decompose()
        calls[ticker] = re.sub(r'\s+', ' ', ' '.join(p.get_text(' ', strip=True) for p in soup.select('.call-text'))).strip()
        candidates[ticker] = (report, documents, bodies, mime_records, old,
                              parsed.get('X-Servicing-Reader-Value') == 'approved')
    source_validation = validate_source_insights(read(REVIEW / 'source-insight-control.json'), calls)
    config = load_config(ROOT / 'config.example.toml')
    config.update(_send=False, _storage=str(OUT / 'scratch'), ai={'enabled': False})
    config['email'].update(recipient='bkaranf5@gmail.com', from_address='bkaranf5@gmail.com')
    manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'send_attempted': False,
                'source_validation': source_validation, 'companies': {}, 'checks': {}}
    for ticker, (report, documents, original_bodies, mime_records, old, already_approved) in candidates.items():
        folder = OUT / ticker
        paths = [old] if already_approved else prepare_messages(config, report, documents, folder)
        assert len(paths) == 1
        parsed = BytesParser(policy=policy.default).parsebytes(paths[0].read_bytes())
        validate_mime_message(parsed)
        bodies, _ = mime_reviews(ticker, parsed)
        assert bodies == original_bodies, f'{ticker}: reviewed MIME bodies changed'
        assert parsed['X-Servicing-Reader-Value'] == 'approved'
        (folder / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf8')
        status[ticker]['messages'] = [{'path': str(p.relative_to(ROOT)), 'bytes': p.stat().st_size, 'sha256': digest(p)} for p in paths]
        records = [folder / 'reader-value-review.json', folder / 'reader-value-text-review.json']
        manifest['companies'][ticker] = {**status[ticker], 'reader_value': report['reader_value'],
            'reader_review_records': [{'path': str(p.relative_to(ROOT)), 'sha256': digest(p)} for p in records] + mime_records,
            'mime_html_sha256': sha256(bodies[0].encode()).hexdigest(),
            'mime_text_sha256': sha256(bodies[1].encode()).hexdigest(),
            'sources': [{'title': d.title, 'sha256': d.content_hash} for d in documents]}
    (OUT / 'build-status.json').write_text(json.dumps(status, indent=2), encoding='utf8')
    for path in [OUT / 'build-status.json', REVIEW / 'qa/audit.json', REVIEW / 'source-insight-control.json']:
        manifest['checks'][str(path.relative_to(ROOT))] = digest(path)
    (REVIEW / 'final-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print(json.dumps({'companies': len(candidates), 'reader_reviews': len(candidates) * 4,
                      'source_validation': source_validation, 'mime_bodies_unchanged': True,
                      'send_attempted': False}, indent=2))


if __name__ == '__main__':
    main()
