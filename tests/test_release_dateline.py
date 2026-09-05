from servicing_brief.reporting import _event_date
from hashlib import sha256


def test_release_dateline_is_independent_of_filing_and_dividend_dates(tmp_path):
    path=tmp_path/'release.htm'
    doc={'kind':'release','source':'sec','published':'2026-08-01','path':str(path),'url':'https://issuer.example/release','id':'release'}
    path.write_text('<p>WESTLAKE VILLAGE, Calif. – July 29, 2026 – Company reports earnings.</p><p>Dividend payable August 30, 2026.</p>',encoding='utf8')
    doc['content_hash'] = sha256(path.read_bytes()).hexdigest()
    evidence=_event_date([doc])
    assert evidence['date']=='2026-07-29' and evidence['label']=='Released'
    assert 'July 29, 2026' in evidence['excerpt']
    path.write_text('<p>Dividend payable August 30, 2026.</p>',encoding='utf8')
    doc['content_hash'] = sha256(path.read_bytes()).hexdigest()
    evidence=_event_date([doc])
    assert evidence['date']=='2026-08-01' and evidence['label']=='Filed'
