"""Reviewed source records expose findings, never their internal bookkeeping."""
from pathlib import Path
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from servicing_brief.branding import brand_view


def test_earnings_context_list_preserves_finding_and_hides_internal_record():
    env = Environment(loader=FileSystemLoader(Path(__file__).parents[1] / 'servicing_brief/templates'))
    view = dict(company_identity={'ticker':'PFSI','cik':'0001745916','event':'2026-Q2'},
                name='PennyMac Financial Services', ticker='PFSI', period='Q2 2026',
                brand=brand_view('PFSI','PennyMac Financial Services'), headline_main='Servicing expenses',
                points=[], explanations=[], rows=[], sources=[], issues=[],
                earnings_context=[{'id':'INTERNAL-ID-NOT-READER-COPY', 'title':'Advance expense',
                                   'text':'The total expense increase was $14.2m, not a separately quantified provision.',
                                   'supporting_review':{'private_review_reason':'INTERNAL-REVIEW-REASON'},
                                   'sources':[{'number':3,'source_url':'https://issuer.example/10q',
                                               'location':'MD&A servicing expenses','content_hash':'INTERNAL-HASH'}]}])
    for template in ('brief.html.j2','brief.txt.j2'):
        rendered = env.get_template(template).render(**view)
        visible = BeautifulSoup(rendered,'html.parser').get_text(' ',strip=True) if '.html.' in template else rendered
        assert '$14.2m' in visible and 'not a separately quantified provision' in visible
        assert '[3]' in visible
        assert not any(value in visible for value in ('INTERNAL-ID','INTERNAL-REVIEW','INTERNAL-HASH', "'sources':"))
