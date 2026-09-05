"""Bind root's compilation review to exact, individually reviewed content."""
from pathlib import Path
from hashlib import sha256
import copy
import json
import re
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from servicing_brief.reader_value import inventory_html, require_reader_value
from servicing_brief.reader_value_release import text_review_html, require_five_company_release

def normalized(text):
    return re.sub(r'\[(?:TD|RY|CM|BNS|BMO)-(\d+)\]',r'[\1]',text)

def wrapper(block):
    text=block['text']
    if text=='The Servicing Brief':
        need='Recognize the independent publication above the company material.'
        value='Publisher identity distinguishes this compilation from issuer-authored communications.'
    elif text=='September 5, 2026':
        need='Know the compilation date separately from each earnings release.'
        value='A shared preparation date orients all five dated company entries.'
    elif re.fullmatch(r'0[1-5]/(?:TD|RY|CM|BNS|BMO)',text) or text.startswith('Index: '):
        need='Navigate directly to the desired company without scanning unrelated entries.'
        value='The chronological ticker index maps each company to its own distinct brief.'
    elif text=='Universe note' or text.startswith('September 5, 2026. Five recent calls'):
        need='Understand the bounded recency claim and unresolved issuer screening coverage.'
        value='The provisional selection may change when candidates such as VersaBank are resolved; it is not an exhaustive latest-five claim.'
    elif re.fullmatch('=+',text):
        need='Recognize a company boundary in the plain-text email alternative.'
        value='The separator prevents adjacent company paragraphs being interpreted as one issuer.'
    else:
        raise ValueError('Unreviewed compilation-only item: '+text)
    return dict(id=block['id'],inventory_text=text,verdict='keep',priority='supporting',scope='orientation',
                reader_need=need,incremental_value=value,inclusion_logic=need+' '+value,reason=value)

def main():
    out=ROOT/'output/brief-improvement'
    records=out/'reader-value-reviews'
    for stem, plain in [('combined-email',False),('gmail-body',False),('combined-email',True)]:
        lookup={}
        for ticker in ['TD','RY','CM','BNS','BMO']:
            suffix='-text' if plain else ''
            record=json.loads((records/f'{ticker}-review{suffix}.json').read_text(encoding='utf8'))
            source=(out/f'{ticker}-review.{"txt" if plain else "html"}').read_text(encoding='utf8')
            html=text_review_html(source,document_kind=record['document_kind']) if plain else source
            require_reader_value(html,record)
            for item in record['items']:
                lookup[normalized(item['inventory_text'])]=item
        source=(out/f'{stem}.{"txt" if plain else "html"}').read_text(encoding='utf8')
        html=text_review_html(source) if plain else source
        items=[]
        for block in inventory_html(html):
            existing=lookup.get(normalized(block['text']))
            if existing:
                item=copy.deepcopy(existing)
                item.update(id=block['id'],inventory_text=block['text'])
                item.pop('html_sha256',None)
                item['inclusion_logic']='Root compared this compiled item to its individually reviewed equivalent. '+item['inclusion_logic']
            else: item=wrapper(block)
            items.append(item)
        record=dict(version=1,priority='P0',objective='mortgage_servicing',reviewer='Root, exact compilation equivalence and wrapper review',document_kind='analysis',html_sha256=sha256(html.encode()).hexdigest(),items=items)
        require_reader_value(html,record)
        path=records/f'{stem}{"-text" if plain else ""}.json'
        path.write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf8')
        print(path.name,len(items))
    results=require_five_company_release(out)
    (out/'loop-1/reader-value-validation.json').write_text(json.dumps({key:{k:v for k,v in value.items() if k!='inventory'} for key,value in results.items()},indent=2),encoding='utf8')

if __name__=='__main__':main()
