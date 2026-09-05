"""Curate reviewed source findings into separate company brief inputs.

Original review/evidence files are immutable. New selections and sourced table
additions live in output/company-briefs, beside their internal review record.
"""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / 'output/five-company-review'
OUT = ROOT / 'output/company-briefs'


def paragraph(label, text, *sources):
    return {'label': label, 'text': text, 'sources': list(sources)}


def metric(label, current, previous, previous_label, source, highlight=''):
    return {'label': label, 'current': current, 'previous': previous,
            'previous_label': previous_label, 'sources': [source], 'highlight': highlight}


def selection(name, headline, deck, deck_sources, *, call=(), insights=(), implications=(), question='', source_labels=None, availability='', availability_sources=()):
    return {'display_name': name, 'headline': headline, 'deck': {'text': deck, 'sources': deck_sources},
            'call': {'text': '', 'sources': [], 'findings': list(call), 'availability': availability,
                     'availability_sources': list(availability_sources)},
            'insights': list(insights), 'implications': list(implications), 'question': question,
            'source_labels': source_labels or {},
            'omit_sections': [{'section': 'servicing_context', 'reason': 'Static eligibility and unrelated annual balances do not explain the current retained financial argument.'}],
            'excluded_sources': []}


def build():
    reviews = {t: json.loads((BASE/t/'review.json').read_text(encoding='utf8')) for t in ['TD','RY','CM','BNS','BMO']}
    overlays = {}
    overlays['TD'] = selection('Toronto-Dominion Bank', 'Mortgage arrears warrant a closer look beneath stronger bank credit',
        'Residential mortgages 31 to 89 days past due and not impaired rose to C$631m from C$536m over nine months. TD\'s risk chief separately flagged slightly higher real estate secured lending delinquency despite stable bankwide delinquency.', ['1','3'],
        call=[
            paragraph('What the risk chief said', 'In Q&A, chief risk officer Ajai Bambawale described lower impaired formations and stronger credit performance across broader portfolios, while real estate secured lending delinquency was slightly higher. The broader credit improvement therefore leaves a mortgage-specific concern to monitor.', '3'),
            paragraph('The pricing decision', 'Asked whether competition explained flat sequential mortgage growth, Canadian Personal Banking head Sona Mehta said TD maintained pricing discipline while expanding margins and real estate secured lending volumes. Her explanation concerns lending and the banking segment; no mortgage servicing margin or renewal-retention rate was disclosed.', '3')],
        insights=[paragraph('Where the additional arrears sit', 'Of the C$95m increase, C$78m was in the 31 to 60 day bucket and C$17m in the 61 to 89 day bucket. These differences are calculated from the report. The table covers groupwide residential mortgages, including loans measured at fair value through other comprehensive income; it excludes loans less than 31 days past due.', '1')],
        implications=[paragraph('Separate exposure from performance', 'The earlier arrears bucket accounts for most of the increase, making cures and movement into later delinquency the next useful indicators. Absolute overdue balances cannot establish a delinquency rate, collection effectiveness or cost to serve without a comparable mortgage denominator and account-level outcomes.', '1')],
        question='Are the additional 31 to 60 day mortgage arrears curing before they move into the later bucket, and how much borrower contact does that require?',
        source_labels={'1':'Q3 report, p. 70: mortgage arrears and definition','3':'Earnings-call transcript, pp. 11–12: risk and mortgage-pricing Q&A'})
    reviews['TD']['metrics'] = [metric('31 to 60 days past due', 'C$485m','C$407m','Oct 31, 2025','1'), metric('61 to 89 days past due','C$146m','C$129m','Oct 31, 2025','1'), metric('Total, 31 to 89 days','C$631m','C$536m','Oct 31, 2025','1','red')]
    overlays['TD']['metric_groups'] = [{'title':'Mortgage arrears by age','scope':'Groupwide residential mortgages past due but not impaired. C$ millions.','previous_label':'Oct 31, 2025','current_label':'Jul 31, 2026','rows':[{'metric_index':i,'unit':'C$m'} for i in range(3)]}]
    overlays['TD']['chart'] = {'title':'Early mortgage arrears','scope':'31 to 89 days past due and not impaired. Balances, not delinquency rates.','unit_label':'C$ millions','period_labels':['Oct 31, 2025','Jul 31, 2026']}

    overlays['RY'] = selection('Royal Bank of Canada','Retention supports mortgage growth; renewal risk remains',
        'Management linked 1.8% sequential mortgage growth in Canadian Personal Banking to switches and retention. The prepared remarks also flag near-term Home Equity Finance renewal risk and competitive mortgage pricing.', ['3'],
        call=[
            paragraph('Growth and retention', 'CEO Dave McKay attributed the improved mortgage-growth pace to higher switch volumes and strong retention. He did not quantify a renewal-retention rate or separate renewals from new switch-in business.', '3'),
            paragraph('Pricing outlook', 'CFO Katherine Gibson expected relatively stable Canadian Banking margins next quarter, with structural tailwinds offset by stronger competition for mortgages and term deposits. The outlook covers the banking segment.', '3'),
            paragraph('Borrower risk at renewal', 'Chief risk officer Graeme Hepworth said near-term renewal risks remained in Home Equity Finance while impairment formations were improving. Improved credit trends and remaining renewal pressure coexist in this portfolio.', '3')],
        implications=[paragraph('Growth does not settle the earnings question', 'The unresolved issue is the economics of retention: balances can be retained at a lower spread, while renewal-related borrower support still consumes resources. The remarks support monitoring retention, achieved renewal pricing and subsequent borrower performance together. They do not establish that RBC has sacrificed spread or lowered servicing costs.', '3')],
        question='What renewal retention and achieved pricing supported the reported growth, and how are recently renewed Home Equity Finance accounts performing?',
        source_labels={'3':'Prepared remarks: CEO pp. 3–4, CFO p. 7, CRO p. 8'},
        availability='Analyst Q&A is not included in the issuer-prepared remarks.', availability_sources=['3'])
    overlays['RY']['chart']={'omit':True,'reason':'No compatible retention rate or renewal-price series is disclosed; a lending-balance chart would imply an unsupported link to the 1.8% management measure.'}
    overlays['RY']['metric_groups']=[]
    overlays['RY']['excluded_metrics']=[{'metric_index':i,'reason':'The broader balance or bank metric does not quantify the retained retention, pricing and renewal-risk argument.'} for i in range(len(reviews['RY']['metrics']))]

    overlays['CM'] = selection('CIBC','Late mortgage arrears rise while losses remain low',
        'Canadian residential mortgage 90+ day delinquency rose to 0.51% from 0.47% in Q2. Management links delinquency to slower housing sales and reports losses in line with historical levels. The renewal cohort averages do not establish outcomes for mortgages already delinquent.', ['5'],
        insights=[
            paragraph('The housing-sales explanation', 'The presentation attributes higher mortgage delinquency to slower housing sales. Mortgage losses remain low and in line with historical levels, which management links to the portfolio\'s loan-to-value profile.', '5'),
            paragraph('What the renewal scenario assumes', 'For renewals from Q4 FY2026 through Q4 FY2027, CIBC models 4.0% and 4.5% renewal rates with no income growth since origination. The largest displayed average payment increase is 1.8% of income at origination: an 11% increase in the mortgage payment itself for Q4 FY2026 at 4.5%. These are modeled cohort averages, excluding third-party mortgages not originated by CIBC, not observed customer outcomes.', '5'),
            paragraph('How to read the delinquency figure', 'The 90+ day rate is based on gross carrying amounts of Canadian residential mortgages and includes multi-family mortgages. It measures mortgage balances, not the share of borrowers.', '5')],
        implications=[paragraph('Keep average affordability and distressed cases separate', 'The renewal illustration helps frame average affordability, while late-stage arrears point to cases already requiring resolution. A favorable average payment profile does not show how quickly those cases will cure or whether a smaller stressed group faces larger payment changes.', '5')],
        question='How do actual renewal payment changes and time to cure compare with the presentation scenarios, especially for mortgages already delinquent?',
        source_labels={'5':'Presentation: PDF pp. 24, 37, 45, 49 and 52: arrears, renewals and definitions'},
        availability='An issuer-published call transcript was not located in the available materials.')
    reviews['CM']['metrics']=[metric('90+ day mortgage delinquency','0.51%','0.47%','Q2 2026','5','red')]
    overlays['CM']['metric_groups']=[]
    overlays['CM']['excluded_metrics']=[{'metric_index':0,'reason':'The adjacent chart already displays both exact rates and periods; a one-row table would repeat it.'}]
    overlays['CM']['chart']={'title':'Late mortgage arrears','scope':'Canadian residential mortgages including multi-family. 90+ day delinquency, based on gross carrying amounts.','unit_label':'percent','period_labels':['Q2 2026','Q3 2026']}

    overlays['BNS'] = selection('Scotiabank','Mortgage arrears rise; management flags Ontario and GTA stress',
        'Canadian mortgage 90+ day delinquency reached 34 basis points, up from 32 in Q2 and 24 a year earlier. Management identifies COVID-era mortgages and some stress in Ontario and the Greater Toronto Area.', ['5'],
        insights=[
            paragraph('Management identifies the pressure', 'Scotiabank says mortgage delinquency is being affected by COVID-era mortgages, with some stress concentrated in Ontario and the Greater Toronto Area. This identifies a vintage and geographic concern without quantifying its contribution to the overall increase.', '5'),
            paragraph('The uninsured GTA differs from the wider book', 'Uninsured GTA mortgage delinquency reached 46 basis points, compared with 35 basis points for uninsured Canadian mortgages overall. Its five-basis-point quarterly increase also exceeded the three-basis-point increase for uninsured Canada.', '5'),
            paragraph('What the rate measures', 'The Canadian mortgage figures include Wealth Management. The rate divides balances 90+ days past due by total mortgage balances at the reporting date; it does not reflect payment-deferral programs. It measures balances, not account counts, and does not quantify servicing expense or time to resolution.', '5')],
        implications=[paragraph('Focus on the concentrated exposure', 'The geographic and vintage detail makes a broad average less informative for case management. A useful next assessment would separate cures, time in delinquency and realized losses for the affected Ontario/GTA vintages from the rest of the book. The rate increase alone cannot establish a rise in servicing cost.', '5')],
        question='Are cures and time to resolution improving for the COVID-era Ontario/GTA mortgage cohorts, and what share of late arrears do those cohorts represent?',
        source_labels={'5':'Investor presentation, pp. 39–40: mortgage stress, rates and definitions'},
        availability='An issuer-published call transcript was not located. The current analysis uses the investor presentation.')
    reviews['BNS']['metrics']=[metric('All Canadian mortgages','0.34%','0.32%','Q2 2026','5'),metric('Uninsured Canadian mortgages','0.35%','0.32%','Q2 2026','5'),metric('Uninsured GTA mortgages','0.46%','0.41%','Q2 2026','5','red')]
    reviews['BNS']['chart']={'title':'Canadian mortgage delinquency','unit':'percent','sources':['5'],'points':[{'period':'Q3 2025','value':'0.24','display':'0.24%'},{'period':'Q2 2026','value':'0.32','display':'0.32%'},{'period':'Q3 2026','value':'0.34','display':'0.34%'}]}
    overlays['BNS']['chart']={'title':'Late mortgage arrears','scope':'Canadian mortgages, including Wealth Management. 90+ days; balance-weighted. Selected quarters.','unit_label':'percent'}
    overlays['BNS']['metric_groups']=[{'title':'Where late arrears are higher','scope':'90+ day rates by mortgage balance, including Wealth Management. Uninsured GTA is a subset of uninsured Canada. 34 basis points equals 0.34%.','previous_label':'Q2 2026','current_label':'Q3 2026','rows':[{'metric_index':i,'unit':'%'} for i in range(3)]}]

    overlays['BMO'] = selection('BMO Financial Group','Payment relief for some renewing borrowers; late arrears still rise',
        'Nearly half of mortgages renewed in Q3 had a payment decrease. Meanwhile, Canadian residential mortgage 90+ day delinquency rose to 0.56% from 0.51% in Q2. BMO is extending Lumi into renewal conversations, with results still unquantified.', ['3','5'],
        call=[paragraph('Lumi enters renewal conversations', 'CEO Darryl White said Lumi was being extended and scaled to support client conversations, starting with mortgage renewals. No renewal retention, cost per conversation or borrower outcomes from the rollout were reported.', '3')],
        insights=[
            paragraph('Completed renewals and the next cohort', 'The presentation says 22% of mortgage balances renew in the next 12 months. Its statement that nearly half of Q3 renewals had payment decreases concerns completed renewals, a different population. The forward payment illustrations assume a 4.25% renewal rate and include regular payments and additional prepayments to date.', '5'),
            paragraph('Credit still needs attention', 'Canadian residential mortgage 90+ day delinquency increased from 0.37% a year earlier to 0.51% in Q2 and 0.56% in Q3. BMO describes proactive account management and pre-delinquency engagement across its Canadian consumer portfolio. The mortgage rate remains a credit measure, not evidence of higher or lower servicing profit.', '5')],
        implications=[paragraph('Evaluate the rollout through renewal outcomes', 'The completed cohort provides a useful affordability observation, but it does not predict the experience of the upcoming 22% of balances. The practical test of Lumi is whether supported renewal conversations improve retention and resolution without worsening errors or complaints. No such result is established by the current disclosure.', '3','5')],
        question='What retention, contact cost and borrower outcomes emerge from Lumi-supported renewals, measured separately from the changing mix of customers coming due?',
        source_labels={'3':'Earnings-call transcript, p. 3: Darryl White on Lumi','5':'Presentation, pp. 29, 31 and 40–41: delinquency, renewals and assumptions'})
    reviews['BMO']['metrics']=[metric('Canadian mortgage 90+ days','0.56%','0.51%','Q2 2026','5','red')]
    reviews['BMO']['chart']={'title':'Canadian mortgage delinquency','unit':'percent','sources':['5'],'points':[{'period':'Q3 2025','value':'0.37','display':'0.37%'},{'period':'Q2 2026','value':'0.51','display':'0.51%'},{'period':'Q3 2026','value':'0.56','display':'0.56%'}]}
    overlays['BMO']['chart']={'title':'Late mortgage arrears','scope':'Canadian consumer residential mortgages, 90+ day delinquency. Selected quarters.','unit_label':'percent'}
    overlays['BMO']['metric_groups']=[]
    overlays['BMO']['excluded_metrics']=[{'metric_index':0,'reason':'The adjacent chart already displays the exact current and prior rates; a one-row table would repeat it.'}]

    for ticker, overlay in overlays.items():
        used=set(overlay['source_labels'])
        overlay['excluded_sources']=[{'source_id':s['id'],'reason':'This source supports no retained current finding, qualifier or event date; its original remains in the evidence archive.'} for s in reviews[ticker]['sources'] if s['id'] not in used]
        out=OUT/'inputs'/ticker
        out.mkdir(parents=True,exist_ok=True)
        (out/'review.json').write_text(json.dumps(reviews[ticker],indent=2,ensure_ascii=False),encoding='utf8')
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'editorial.json').write_text(json.dumps({'version':1,'companies':overlays},indent=2,ensure_ascii=False),encoding='utf8')
    (OUT/'inputs/selection.json').write_text(json.dumps({'tickers':list(reviews),'as_of':'2026-09-05'}),encoding='utf8')
    print('Prepared five separate company inputs; original reviews unchanged. No send.')


if __name__ == '__main__':
    build()
