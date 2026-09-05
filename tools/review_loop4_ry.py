"""Root's explicit final re-review of the newly retained RBC call findings."""
from pathlib import Path
from hashlib import sha256
from difflib import SequenceMatcher
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servicing_brief.reader_value import inventory_html, require_reader_value
from servicing_brief.reader_value_release import text_review_html, require_five_company_release

NEW = {
    'RBC links mortgage growth to strong retention': ('supporting', 'mortgage_retention', 'Orient the reader to management\'s specific mortgage-retention explanation without claiming a quantified retention rate.'),
    'In Canadian Personal Banking, management attributed 1.8% quarter-over-quarter mortgage growth to higher switch volumes and strong retention. The remarks do not quantify the retention rate. [3]': ('primary', 'mortgage_retention', 'RBC prepared remarks pp. 3-4 directly attribute Canadian mortgage growth to switching and retention. The 1.8 percent measures sequential mortgage growth, not retention. Explicit attribution and the unquantified-retention qualification prevent a stronger causal or numeric inference.'),
    'From the call': ('utility', 'source_orientation', 'Identify the following paragraph as management commentary from the earnings-call materials.'),
    'Chief Risk Officer Graeme Hepworth said RBC was still managing near-term renewal risks in its Home Equity Finance portfolio, while seeing improvements in impairment formations. [3]': ('supporting', 'borrower_risk', 'RBC prepared remarks p. 8 adds continuing renewal risk and improving impairment formations to the growth/retention finding. Preserve the Home Equity Finance population and management attribution; this does not quantify servicing profit or assume all renewal risk has passed.'),
    'Investor question': ('utility', 'reader_question', 'Distinguish a question to investigate from a reported result.'),
    'How will mortgage retention and borrower performance hold up through the remaining renewal cycle?': ('supporting', 'mortgage_retention', 'Test the specific unresolved durability of retention and borrower performance while management still identifies near-term renewal risk.'),
    '[3] Prepared remarks: retention, pp. 3-4; renewal risk, p. 8': ('utility', 'evidence_access', 'Give the reader the original issuer remarks and the exact relevant pages for both retained findings.'),
    'https://www.rbc.com/investor-relations/_assets-custom/pdf/2026q3speech.pdf': ('utility', 'evidence_access', 'Preserve the original issuer document URL in the plain-text alternative.'),
}
REMOVED = {
    'Coverage note',
    'Banking results do not isolate servicing profitability',
    'RBC’s Q3 Personal Banking results combine mortgages with other banking activities. They do not provide a standalone measure of mortgage servicing earnings. [1]',
    '[1] Q3 report: Personal Banking, p. 15',
    'https://www.rbc.com/investor-relations/_assets-custom/pdf/2026q3_report.pdf',
}


def main():
    root = ROOT / 'output/brief-improvement'
    results = {}
    for p in sorted((root / 'reader-value-reviews').glob('*.json')):
        record = json.loads(p.read_text(encoding='utf8'))
        plain = p.stem.endswith('-text')
        stem = p.stem.removesuffix('-text')
        if stem == 'RY-review': record['document_kind'] = 'analysis'
        source = (root / f'{stem}.{"txt" if plain else "html"}').read_text(encoding='utf8')
        html = text_review_html(source, document_kind=record['document_kind']) if plain else source
        blocks = inventory_html(html)
        old = [x['inventory_text'] for x in record['items']]
        new = [x['text'] for x in blocks]
        items = []
        for op, i, j, a, b in SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
            if op == 'equal':
                items.extend(dict(x) for x in record['items'][i:j])
                continue
            assert all(t in REMOVED for t in old[i:j]), (p.name, old[i:j])
            for text in new[a:b]:
                assert text in NEW, (p.name, text)
                priority, scope, reason = NEW[text]
                items.append(dict(verdict='keep', priority=priority, scope=scope, reader_need=reason,
                    incremental_value=reason, inclusion_logic=reason, reason=reason))
        assert len(items) == len(blocks)
        for item, block in zip(items, blocks, strict=True):
            item.update(id=block['id'], inventory_text=block['text'])
        record.update(items=items, html_sha256=sha256(html.encode()).hexdigest(),
                      reviewer='Root, final Loop 4 RBC primary-source adjudication and complete visual re-review')
        result = require_reader_value(html, record)
        results[p.stem] = {k: v for k, v in result.items() if k != 'inventory'}
        p.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding='utf8')
    require_five_company_release(root)
    (root / 'loop-4/reader-value-validation.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('All 15 exact records pass with the explicit RBC retention/renewal findings.')


if __name__ == '__main__': main()
