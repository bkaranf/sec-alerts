"""One-off artifact checks; sources and financial interpretation need review too."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
universe = json.loads((ROOT.parent/'servicer-universe/ticker-reference-live-exchange.json').read_text())['records']
expected = {r['ticker']: str(r['cik']).zfill(10) for r in universe}
tickers = ['TD', 'RY', 'CM', 'BNS', 'BMO']
checks = []
for ticker in tickers:
    file = ROOT/ticker/'review.json'
    if not file.exists():
        checks.append({'ticker': ticker, 'errors':['review missing']})
        continue
    review = json.loads(file.read_text(encoding='utf-8'))
    errors = []
    if review['cik'] != expected[ticker]: errors.append('CIK does not match live SEC directory')
    if '\u2014' in file.read_text(encoding='utf-8'): errors.append('em dash present')
    prose = ' '.join([review.get('summary',''),review.get('call_note',''),review.get('investor_question','')] + [p['text'] for p in review.get('analysis',[])])
    if not 150 <= len(prose.split()) <= 300: errors.append('prose outside review length range')
    for color in ['green','red']:
        if sum(m.get('highlight') == color for m in review['metrics']) > 1: errors.append('too many '+color+' highlights')
    if any(m.get('highlight','') not in {'','green','red'} for m in review['metrics']): errors.append('unknown highlight enum')
    sources=[]
    for source in review['sources']:
        path=Path(source.get('local_path',''))
        if not path.is_absolute() or not path.is_file():
            errors.append('source path missing or not absolute: '+source['id'])
            continue
        sources.append({'id':source['id'],'path':str(path),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    for attachment in review.get('attachment_candidates',[]):
        if not Path(attachment).is_absolute() or not Path(attachment).is_file(): errors.append('attachment missing or not absolute')
    checks.append({'ticker':ticker,'cik':review['cik'],'prose_words':len(prose.split()),'chart':bool(review.get('chart')),'errors':errors,'sources':sources})
(ROOT/'artifact-checks.json').write_text(json.dumps({'checks':checks},indent=2),encoding='utf-8')
print(json.dumps([{k:r[k] for k in r if k!='sources'} for r in checks],indent=2))
raise SystemExit(1 if any(r['errors'] for r in checks) else 0)
