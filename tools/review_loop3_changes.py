"""Record root's explicit review of five bounded Loop 3 PFSI copy changes."""
from pathlib import Path
from hashlib import sha256
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servicing_brief.reader_value import inventory_html, require_reader_value
from servicing_brief.reader_value_release import text_review_html, require_five_company_release

CHANGES = {
    'Servicing earnings improved.': (
        'Servicing pretax income rose from Q1 2026.',
        'The headline now specifies the reported pretax measure and sequential comparator, so it cannot imply improvement over the year-ago quarter.'),
    'Funding costs climbed.': (
        'Servicing interest expense increased.',
        'The secondary headline identifies the disclosed total servicing interest expense without narrowing it to the MSR financing subset.'),
    "¹ Non-GAAP presentation; see the issuer's reconciliation. Portfolio UPB includes owned servicing, subservicing and loans held for sale.": (
        "¹ Non-GAAP presentation; see the issuer's reconciliation. Portfolio UPB is a period-end balance and includes owned servicing, subservicing and loans held for sale.",
        'The qualification distinguishes the UPB stock from the quarterly income flows while preserving portfolio scope and the non-GAAP reconciliation.'),
    'Prepayments and arrears. Owned-portfolio prepayments slowed to 11.6% CPR from 13.7% in Q1. Separately, 60+ day delinquency declined to 4.1% from 4.2% of loans in that portfolio. [2]': (
        'Prepayments and arrears. Owned-portfolio prepayments slowed to 11.6% CPR from 13.7% in Q1. Separately, 60+ day delinquency declined to 4.1% from 4.2% by loan count in that portfolio. [2]',
        'The presentation defines this owned-portfolio delinquency rate by loan count. Making the denominator explicit prevents confusion with the separate UPB-based filing metric.'),
}
NOTE = 'Versus Q1 2026: stronger pretax income and higher interest expense.'


def main():
    root = ROOT / 'output/brief-improvement'
    results = {}
    for path in sorted((root / 'reader-value-reviews').glob('*.json')):
        record = json.loads(path.read_text(encoding='utf8'))
        plain = path.stem.endswith('-text')
        stem = path.stem.removesuffix('-text')
        source = (root / f'{stem}.{"txt" if plain else "html"}').read_text(encoding='utf8')
        html = text_review_html(source, document_kind=record['document_kind']) if plain else source
        blocks = inventory_html(html)
        pfsi = stem == 'PFSI-review'
        assert len(blocks) == len(record['items']) + int(pfsi), path
        items, changes_seen, previous = [], set(), iter(record['items'])
        for block in blocks:
            if pfsi and block['text'] == NOTE:
                item = dict(verdict='keep', priority='utility', scope='comparison_basis',
                    reader_need='Interpret the selected income and expense changes against their actual prior quarter.',
                    incremental_value='The note makes the table emphasis understandable without relying on color or assuming a year-over-year comparison.',
                    inclusion_logic='Both selected source-exact figures increased versus Q1 2026; this statement is valid in HTML and plain text.',
                    reason='A short basis note prevents the green and red emphasis from implying a different comparator or a whole-company judgment.')
            else:
                item = dict(next(previous))
                expected = item['inventory_text']
                if pfsi and expected in CHANGES:
                    changes_seen.add(expected)
                    expected, reason = CHANGES[expected]
                    item.update(reader_need=reason, incremental_value=reason, inclusion_logic=reason, reason=reason)
                assert block['text'] == expected, (path.name, expected, block['text'])
            item.update(id=block['id'], inventory_text=block['text'])
            items.append(item)
        assert next(previous, None) is None
        assert changes_seen == (set(CHANGES) if pfsi else set())
        record.update(items=items, reviewer='Root, explicit Loop 3 source-definition and visual review',
                      html_sha256=sha256(html.encode()).hexdigest())
        result = require_reader_value(html, record)
        results[path.stem] = {k: v for k, v in result.items() if k != 'inventory'}
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf8')
    require_five_company_release(root)
    (root / 'loop-3/reader-value-validation.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('15 exact-output reviews pass; 4 precise PFSI revisions and 1 comparison note explicitly reviewed in each alternative.')


if __name__ == '__main__':
    main()
