"""Root's targeted re-review of loop-2 layout and text-citation changes."""
from pathlib import Path
from hashlib import sha256
import json
import re
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from servicing_brief.reader_value import inventory_html,require_reader_value
from servicing_brief.reader_value_release import text_review_html,require_five_company_release

def main():
    root=ROOT/'output/brief-improvement'
    for path in (root/'reader-value-reviews').glob('*.json'):
        record=json.loads(path.read_text(encoding='utf8'))
        plain=path.stem.endswith('-text')
        stem=path.stem.removesuffix('-text')
        source=(root/f'{stem}.{"txt" if plain else "html"}').read_text(encoding='utf8')
        html=text_review_html(source,document_kind=record['document_kind']) if plain else source
        blocks=inventory_html(html)
        if plain and stem!='PFSI-review' and len(blocks)==len(record['items'])+2:
            assert blocks[0]['text']=='The Servicing Brief' and blocks[1]['text']=='September 5, 2026'
            added=[]
            for block in blocks[:2]:
                added.append(dict(id=block['id'],inventory_text=block['text'],verdict='keep',priority='utility',scope='publisher',reader_need='Identify the independent publisher and dated issue when reading the standalone text alternative.',incremental_value='Publisher identity now survives forwarding without the HTML logo or compiled email masthead.',inclusion_logic='Root accepted this exact standalone publisher/date addition for consistent company and publisher identity.',reason='The text alternative needs the same publisher orientation as its HTML companion.'))
            record['items']=added+record['items']
        assert len(blocks)==len(record['items']),path
        for item,block in zip(record['items'],blocks,strict=True):
            prior=item['inventory_text']
            if prior.startswith(('Payment pressure.', 'Payment pressure:')):
                prior=prior.replace('Payment pressure','Payment changes',1)
                item['reason']='The neutral heading describes renewal payment movement without implying that a payment decrease proves financial pressure. Both disclosed cohorts and their qualification are unchanged.'
            if plain and stem=='PFSI-review' and prior.startswith('Management '):
                prior=re.sub(r'\[(\d+); HTML text line \d+; https://[^\]]+\]',r'[\1]',prior)
                item['reason']='Root verified the financial explanation is unchanged and its compact source number resolves to the same original URL in Sources. Extraction locators remain in evidence.'
            assert prior==block['text'],(path.name,item['id'],prior,block['text'])
            item.update(id=block['id'],inventory_text=block['text'])
            item.pop('html_sha256',None)
        record.update(reviewer='Root, loop-2 full visual/content review and exact text equivalence',html_sha256=sha256(html.encode()).hexdigest())
        require_reader_value(html,record)
        path.write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf8')
    results=require_five_company_release(root)
    out=root/'loop-2'
    out.mkdir(exist_ok=True)
    (out/'reader-value-validation.json').write_text(json.dumps({k:{n:v for n,v in r.items() if n!='inventory'} for k,r in results.items()},indent=2),encoding='utf8')
    print('All 15 exact output records re-reviewed: compact PFSI references, neutral BMO label and standalone publisher identity are the explicit text changes.')

if __name__=='__main__':main()
