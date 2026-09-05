"""Verify the delivered artifact and negative release boundaries without sending."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

from bs4 import BeautifulSoup
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'output/five-company-review'
OUT = ROOT / 'output/reader-value-review'
PDF = ROOT / 'output/pdf/Servicing-Briefs-2026-09-05.pdf'

def digest(path):
    return sha256(path.read_bytes()).hexdigest()

def main():
    sent = json.loads((ROOT/'output/pdf/pdf-email-state.json').read_text())
    assert digest(PDF) == sent['sha256']
    baseline = json.loads((BASE/'redesign-baseline.json').read_text())['canonical_files']
    assert all(digest(BASE/name) == value for name, value in baseline.items())
    proof = json.loads((OUT/'sent-pdf-verification.json').read_text())
    pdf = pymupdf.open(PDF)
    matches = []
    tokens = lambda value: Counter(re.findall(r'\w+|[^\w\s]', value))
    for page, previous in zip(pdf, proof['pages'], strict=True):
        ticker = previous['ticker']
        path = BASE/f'{ticker}-review.html'
        assert digest(path) == previous['html_sha256']
        soup = BeautifulSoup(path.read_text(encoding='utf8'), 'html.parser')
        for tag in soup.select('style,script,title,.document-footer'):
            tag.decompose()
        assert tokens(page.get_text()) == tokens(soup.body.get_text(' ', strip=True)), ticker
        matches.append(ticker)
    protected = [PDF, *BASE.glob('*.html'), *BASE.glob('*.eml'), *BASE.glob('*.zip'),
                 *[BASE/name for name in baseline], *list((BASE/'sent-2026-09-05').rglob('*'))]
    protected = [p for p in protected if p.is_file()]
    before = {str(p.relative_to(ROOT)): digest(p) for p in protected}
    runtime = Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node'
    commands = [
        [sys.executable, '-m', 'servicing_brief.reader_value_release', str(BASE)],
        [sys.executable, str(BASE/'package_email.py')],
        [str(runtime/'bin/node.exe'), str(ROOT/'tools/create_phone_pdf.cjs'), str(runtime/'node_modules/playwright')],
    ]
    boundaries = []
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding='utf8')
        assert result.returncode != 0, command
        assert 'P0 reader-value release HOLD' in result.stderr, result.stderr
        boundaries.append({'command': command, 'exit_code': result.returncode, 'expected_hold': True})
    assert all(digest(ROOT/name) == value for name, value in before.items())
    records = json.loads((OUT/'item-review.json').read_text(encoding='utf8'))
    decisions = Counter(r['verdict'] for review in records.values() for r in review['items'] if r['in_sent_pdf'])
    result = {'sent_pdf_sha256': digest(PDF), 'exact_pdf_html_matches': matches,
              'sent_blocks_reviewed': sum(decisions.values()), 'dispositions': dict(decisions),
              'canonical_files_preserved': len(baseline), 'protected_files_unchanged': len(before),
              'boundaries': boundaries, 'replacement_sent': False}
    (OUT/'release-verification.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result, indent=2))

if __name__ == '__main__':
    main()
