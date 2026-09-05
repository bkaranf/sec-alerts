"""Rebuild the representative normal brief using its archived source package."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from servicing_brief.models import Document
from servicing_brief.reporting import build_report
from servicing_brief.config import load_config

def main():
    base=ROOT/'output/qa/draft'
    out=ROOT/'output/brief-improvement'
    old=json.loads((base/'reader-first-report.json').read_text(encoding='utf8'))
    documents=[Document(**d) for d in json.loads((base/'documents.json').read_text(encoding='utf8'))]
    config=load_config(ROOT/'config.example.toml')
    config['ai']={'enabled':False}
    config['_storage']=str(out/'scratch')
    report=build_report(config,documents,baseline=True,coverage=old['coverage'])
    out.mkdir(parents=True,exist_ok=True)
    (out/'PFSI-review.html').write_text(report['html'],encoding='utf8')
    (out/'PFSI-review.txt').write_text(report['text'],encoding='utf8')
    (out/'PFSI-report.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    print('Rebuilt normal PFSI preview from archived documents; no send.')

if __name__=='__main__':main()
