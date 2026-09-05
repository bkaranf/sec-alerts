"""Package the reviewed one-off email and original public documents; never send."""
from pathlib import Path
from email.message import EmailMessage
from email.policy import SMTP
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from servicing_brief.editorial_control import validate_source_insights
from servicing_brief.reader_content import assert_reader_content
from servicing_brief.reader_value_release import require_five_company_release
from servicing_brief.delivery import embed_brand_logos

ROOT = Path(__file__).resolve().parent
UNIVERSE = ROOT.parent / 'servicer-universe'
TICKERS = ['TD', 'RY', 'CM', 'BNS', 'BMO']
SUBJECT = 'Mortgage servicing | Five recent earnings calls | September 5, 2026'
LIMIT = 15 * 1024 * 1024
NOTE = ('Attached: five company briefs, the earnings calendar, company lists and 15 original documents. '
        'Additional source materials are linked in each brief; attachment details are in the archive manifest.')

def build():
    require_five_company_release(ROOT)
    subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'audit_redesign.py')], check=True)
    html = (ROOT / 'combined-email.html').read_text(encoding='utf8')
    soup = BeautifulSoup(html, 'html.parser')
    calls = {}
    for section in soup.select('.company-section[data-ticker]'):
        paragraphs = []
        for node in section.select('.call-text'):
            for ref in node.select('sup'):
                ref.decompose()
            paragraphs.append(node.get_text(' ', strip=True))
        calls[section['data-ticker']] = ' '.join(paragraphs)
    control = json.loads((ROOT / 'source-insight-control.json').read_text(encoding='utf8'))
    audit = validate_source_insights(control, calls)
    for stem in ['combined-email', *[f'{ticker}-review' for ticker in TICKERS]]:
        assert_reader_content((ROOT / f'{stem}.html').read_text(encoding='utf8'),
                              (ROOT / f'{stem}.txt').read_text(encoding='utf8'))
    body = (ROOT / 'gmail-body.html').read_text(encoding='utf8')
    assert_reader_content(body, '')
    assert len(body.encode('utf8')) < 90_000, 'Email body exceeds 90KB clipping safety budget'
    (ROOT / 'source-insight-validation.json').write_text(json.dumps(audit, indent=2), encoding='utf8')
    sources = []
    originals = {}
    for ticker in TICKERS:
        review = json.loads((ROOT / ticker / 'review.json').read_text(encoding='utf8'))
        for source in review['sources']:
            path = Path(source['local_path'])
            data = path.read_bytes()
            include = (path.suffix.lower() == '.pdf'
                       and not any(term in path.name for term in ['2025', 'covered-bond'])
                       and path.name != 'CM-Q3-2026-investor-presentation.pdf')
            archive_path = f'documents/{ticker}/{path.name}' if include else None
            if archive_path:
                originals[archive_path] = data
            sources.append({'company': ticker, 'source_id': source['id'],
                            'label': source['label'], 'url': source['url'],
                            'location': source.get('location'),
                            'sha256': hashlib.sha256(data).hexdigest(),
                            'original_bytes': len(data), 'archive_path': archive_path,
                            'omission_reason': None if include else
                            'Linked online; omitted from attachment to keep this email below 15 MiB.'})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in originals.items():
            archive.writestr(name, data)
        for ticker in TICKERS:
            for suffix in ['html', 'txt']:
                path = ROOT / f'{ticker}-review.{suffix}'
                archive.writestr(f'briefs/{path.name}', path.read_bytes())
        for name in ['calendar.csv', 'confirmed-servicers.csv', 'unresolved-candidates.csv',
                     'EARNINGS_CALENDAR.md']:
            archive.writestr(f'calendar/{name}', (UNIVERSE / name).read_bytes())
        archive.writestr('source-manifest.json', json.dumps(sources, indent=2, ensure_ascii=False))
        # Carry the new call finding's precise provenance without publishing
        # internal selection judgments or local workstation paths.
        call_evidence = [{key: record[key] for key in
                         ('ticker', 'source_id', 'source_url', 'sha256', 'location',
                          'evidence_excerpt', 'insight_type', 'published_text')}
                         for record in control['companies']
                         if record['disposition'] == 'insight_added']
        archive.writestr('call-evidence.json', json.dumps(call_evidence, indent=2, ensure_ascii=False))
        archive.writestr('README.txt', 'The Servicing Brief | September 5, 2026\n\n'
            + NOTE + '\n\nStart with the five HTML or plain-text briefs in briefs/. '
            'The CSV calendar can be opened in Excel. Dates marked unknown have not been verified. '
            'The company list is a documented SEC screen, with unresolved candidates retained; '
            'it is not a claim of exhaustive coverage. Each source manifest entry includes its '
            'official URL, page reference and SHA-256. Attached documents preserve original bytes. '
            'RBC management comments are prepared remarks, not a full Q&A transcript.\n')
    zip_bytes = buf.getvalue()
    email = EmailMessage(policy=SMTP)
    email['From'] = email['To'] = 'bkaranf5@gmail.com'
    email['Subject'] = SUBJECT
    email.set_content((ROOT / 'combined-email.txt').read_text(encoding='utf8'))
    email.add_alternative((ROOT / 'gmail-body.html').read_text(encoding='utf8'), subtype='html')
    embed_brand_logos(email, (ROOT / 'gmail-body.html').read_text(encoding='utf8'))
    email.add_attachment(zip_bytes, maintype='application', subtype='zip',
                         filename='Servicing-Briefs-2026-09-05.zip')
    mime = email.as_bytes()
    assert len(mime) <= LIMIT, f'MIME exceeds 15 MiB: {len(mime)}'
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        assert archive.testzip() is None
        for s in sources:
            if s['archive_path']:
                assert hashlib.sha256(archive.read(s['archive_path'])).hexdigest() == s['sha256']
    (ROOT / 'Servicing-Briefs-2026-09-05.zip').write_bytes(zip_bytes)
    (ROOT / 'review-email.eml').write_bytes(mime)
    manifest = {'zip_bytes': len(zip_bytes), 'mime_bytes': len(mime), 'mime_limit': LIMIT,
                'zip_sha256': hashlib.sha256(zip_bytes).hexdigest(),
                'original_document_count': len(originals), 'sources': sources,
                'attachment_note': NOTE, 'sent': False}
    (ROOT / 'attachment-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf8')
    print(json.dumps({k:v for k,v in manifest.items() if k != 'sources'}))

if __name__ == '__main__':
    # Fail before rewriting any preview or previously prepared email.
    require_five_company_release(ROOT)
    subprocess.run([sys.executable, '-X', 'utf8', str(ROOT / 'render_email.py'),
                    '--attachment-note', NOTE], check=True)
    build()
