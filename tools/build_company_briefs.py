"""Build separate, held review packages. This command never sends mail."""
from datetime import datetime, timezone
from hashlib import sha256
import base64
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from jinja2 import Environment, FileSystemLoader
from servicing_brief.config import load_config
from servicing_brief.branding import brand_view, validate_page_theme
from servicing_brief.company_boundary import require_company_boundary, validate_mime_message
from servicing_brief.delivery import prepare_messages
from servicing_brief.models import Document
from servicing_brief.pipeline import write_report
from servicing_brief.reader_value_release import refresh_report_review
from servicing_brief.reader_content import assert_reader_content
from servicing_brief.reporting import build_report
from build_company_editorial import build as curate
from email import policy
from email.parser import BytesParser

OUT = ROOT/'output/company-briefs'


def renderer_module():
    path=ROOT/'output/five-company-review/render_email.py'
    spec=importlib.util.spec_from_file_location('company_renderer',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document_for_source(source, raw, identity):
    path=Path(source['local_path'])
    content=path.read_bytes()
    label=source['label'].lower()
    kind='prepared_remarks' if 'management comments' in label else 'transcript' if 'transcript' in label else 'presentation' if 'presentation' in label else 'quarterly_report'
    return Document(issuer=raw['name'],cik=identity['cik'],title=source['label'],kind=kind,source='ir',url=source['url'],
                    published=raw.get('release_date') or raw['call_date'],period=identity['event'],path=str(path),
                    content_hash=sha256(content).hexdigest(),classification='issuer-published',mime_type='application/pdf',
                    metadata={'ticker':identity['ticker'],'reviewed_location':source.get('location','')})


def save(config, ticker, report, documents):
    folder=OUT/ticker
    report['id']=sha256((ticker+report['company_identity']['event']+report['html']).encode()).hexdigest()[:24]
    require_company_boundary(report,documents)
    validate_page_theme(report['html'],ticker)
    refresh_report_review(report,folder)
    write_report(report,folder)
    (folder/'documents.json').write_text(json.dumps([d.to_dict() for d in documents],indent=2),encoding='utf8')
    # Convenient standalone preview paths, never a financial compilation.
    (OUT/f'{ticker}-review.html').write_text(report['html'],encoding='utf8')
    (OUT/f'{ticker}-review.txt').write_text(report['text'],encoding='utf8')
    paths=prepare_messages(config,report,documents,folder)
    mime=[]
    for index,path in enumerate(paths,1):
        parsed=BytesParser(policy=policy.default).parsebytes(path.read_bytes())
        validate_mime_message(parsed)
        html=parsed.get_body(preferencelist=('html',)).get_content()
        (folder/f'email-{index}.html').write_text(html,encoding='utf8')
        decoded=html
        for part in parsed.walk():
            cid=str(part.get('Content-ID','')).strip('<>')
            if cid and part.get_content_maintype()=='image':
                data_url='data:'+part.get_content_type()+';base64,'+base64.b64encode(part.get_payload(decode=True)).decode()
                decoded=decoded.replace('cid:'+cid,data_url)
        assert_reader_content(decoded, parsed.get_body(preferencelist=('plain',)).get_content(), subject=str(parsed.get('Subject', '')))
        (folder/f'email-{index}-preview.html').write_text(decoded,encoding='utf8')
        (folder/f'email-{index}.txt').write_text(parsed.get_body(preferencelist=('plain',)).get_content(),encoding='utf8')
        mime.append({'path':str(path.relative_to(ROOT)),'bytes':path.stat().st_size,'sha256':sha256(path.read_bytes()).hexdigest()})
    return {'company_identity':report['company_identity'],'theme':brand_view(ticker,ticker)['theme'],
            'html_sha256':sha256((OUT/f'{ticker}-review.html').read_bytes()).hexdigest(),'text_sha256':sha256((OUT/f'{ticker}-review.txt').read_bytes()).hexdigest(),
            'normalized_html_sha256':sha256(report['html'].encode()).hexdigest(),'normalized_text_sha256':sha256(report['text'].encode()).hexdigest(),
            'html_bytes':len(report['html'].encode()),'messages':mime,'send_attempted':False}


def main():
    curate()
    renderer=renderer_module()
    overlays=json.loads((OUT/'editorial.json').read_text(encoding='utf8'))['companies']
    env=Environment(loader=FileSystemLoader([ROOT/'output/five-company-review',ROOT/'servicing_brief/templates']),autoescape=True,trim_blocks=True,lstrip_blocks=True)
    config=load_config(ROOT/'config.example.toml')
    config.update(_send=False,_storage=str(OUT/'scratch'),ai={'enabled':False})
    config['email'].update(recipient='bkaranf5@gmail.com',from_address='bkaranf5@gmail.com')
    results={}
    for ticker,overlay in overlays.items():
        raw=json.loads((OUT/'inputs'/ticker/'review.json').read_text(encoding='utf8'))
        company=renderer._review(raw,ticker,False,overlay,1)
        html=renderer._render_html(env,[company],combined=False,universe_note='',attachment_note='',full_document=True)
        text=renderer._render_text([company],combined=False,universe_note='',attachment_note='')
        identity=company['company_identity']
        kept={s['id'] for s in company['sources']}
        documents=[document_for_source(source,raw,identity) for source in raw['sources'] if source['id'] in kept]
        report={'subject':f"{ticker} | {company['period']} | The Servicing Brief",'company_identity':identity,
                'generated_at':datetime.now(timezone.utc).isoformat(),'html':html,'text':text,
                'company_reports':{ticker:{'html':html,'text':text}},'evidence':raw,'context_documents':[],
                'design_version':'company-executive-v1','coverage':{'as_of':'2026-09-05','checked':[ticker]}}
        results[ticker]=save(config,ticker,report,documents)
    base=ROOT/'output/qa/draft'
    old=json.loads((base/'reader-first-report.json').read_text(encoding='utf8'))
    documents=[Document(**d) for d in json.loads((base/'documents.json').read_text(encoding='utf8')) if d['kind'] != '8-K']
    report=build_report(config,documents,baseline=True,coverage=old['coverage'])
    results['PFSI']=save(config,'PFSI',report,documents)
    (OUT/'build-status.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps(results,indent=2))


if __name__=='__main__':
    main()
