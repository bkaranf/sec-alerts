"""Explicit editorial revision for loop 1; never mutates canonical evidence."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'output/five-company-review'
DEST = ROOT/'output/brief-improvement'

def main():
    data = copy.deepcopy(json.loads((SOURCE/'editorial.json').read_text(encoding='utf8')))
    for ticker, edit in data['companies'].items():
        raw = json.loads((SOURCE/ticker/'review.json').read_text(encoding='utf8'))
        edit['metric_groups'] = []
        edit['excluded_metrics'] = [{'metric_index': i, 'reason': 'Removed from the compact reader selection: no distinct servicing conclusion beyond the retained finding or its visual.'} for i in range(len(raw['metrics']))]
        edit['insights'] = []
        edit['omit_sections'] = [{'section': 'servicing_context', 'reason': 'The retained finding carries its own scope; annual figures and eligibility do not explain this quarter.'}]
        edit['call'] = {'text': '', 'sources': []}
        if ticker not in ('TD','CM'):
            edit['chart'] = {'omit': True, 'reason': 'Available lending or credit bars would compete with the selected renewal finding or bounded coverage note.'}
    td = data['companies']['TD']
    td.update(headline='Early mortgage arrears rise over nine months', question='How much of the increase reflects new arrears versus fewer loans returning to current status?')
    td['deck'] = {'text': 'More mortgage exposure is past due. These are overdue balances, not delinquency rates or a measure of servicing effectiveness.', 'sources': ['1']}
    td['excluded_sources'] = [{'source_id': s, 'reason': 'No retained claim relies on the broader lending, call or annual disclosure.'} for s in ['2','3','4']]
    td['source_labels']['1'] = 'Q3 report: mortgage arrears, p. 70'

    cm = data['companies']['CM']
    cm.update(headline='Mortgage delinquencies edge higher', question='How are slower property sales affecting time to resolution and the cost of managing delinquent mortgages?')
    cm['deck'] = {'text': 'CIBC says slower housing sales affect mortgage delinquencies. It describes mortgage losses as low and in line with historical levels.', 'sources': ['5']}
    cm['excluded_sources'] = [{'source_id': s, 'reason': 'The retained mortgage-credit finding is supported by the presentation; the separate source is unused.'} for s in ['1','2','3','4']]
    cm['source_labels']['5'] = 'Q3 presentation: Canadian consumer lending, p. 24'
    cm['chart']['scope'] = 'Canadian residential mortgages, including multi-family; 90+ days past due as a share of gross loan carrying amount'
    cm['source_labels']['5'] = 'Q3 presentation: p. 24; definitions p. 45, 49'

    bmo = data['companies']['BMO']
    bmo.update(headline='BMO extends its chatbot to mortgage renewals', question='What will show whether Lumi improves renewal completion and retention without worsening borrower outcomes?')
    bmo['deck'] = {'text': 'BMO reports that 22% of Canadian mortgage balances renew in the next 12 months.', 'sources': ['5']}
    bmo['insights'] = [{'label': 'Payment changes', 'text': 'Nearly half of mortgages renewed in Q3 had a payment decrease. That describes the completed renewal cohort; it does not forecast payments for the upcoming cohort.', 'sources': ['5']}]
    bmo['call'] = {'text': 'CEO Darryl White said Lumi was being extended into client conversations, starting with mortgage renewals. The call describes a rollout, without reporting its effect on renewal outcomes.', 'sources': ['3']}
    bmo['excluded_sources'] = [{'source_id': s, 'reason': 'The renewal finding does not require overlapping loan balances, mixed annual rights or a separate credit series.'} for s in ['1','2','4']]
    bmo['source_labels']['5'] = 'Q3 presentation: mortgage renewals, p. 31'
    bmo['source_labels']['3'] = 'Call transcript: CEO remarks, p. 3'

    ry = data['companies']['RY']
    ry['brief_kind'] = 'analysis'
    ry['headline'] = 'RBC links mortgage growth to strong retention'
    ry['deck'] = {'text': 'In Canadian Personal Banking, management attributed 1.8% quarter-over-quarter mortgage growth to higher switch volumes and strong retention. The remarks do not quantify the retention rate.', 'sources': ['3']}
    ry['call'] = {'text': 'Chief Risk Officer Graeme Hepworth said RBC was still managing near-term renewal risks in its Home Equity Finance portfolio, while seeing improvements in impairment formations.', 'sources': ['3']}
    ry['question'] = 'How will mortgage retention and borrower performance hold up through the remaining renewal cycle?'
    ry['excluded_sources'] = [{'source_id': s, 'reason': 'No retained claim relies on this source after selecting the management remarks on retention and renewal risk.'} for s in ['1','2','4','5','6']]
    ry['source_labels'] = {'3': 'Prepared remarks: retention, pp. 3-4; renewal risk, p. 8'}

    bns = data['companies']['BNS']
    bns['brief_kind'] = 'coverage_note'
    bns['headline'] = 'Current earnings and servicing disclosures have different scopes'
    bns['deck'] = {'text': 'Q3 Canadian Banking earnings cover the broader banking business. The covered-bond prospectus describes servicing activity as of October 2025; it cannot establish a Q3 servicing earnings trend.', 'sources': ['1','3']}
    bns['omit_sections'].append({'section': 'question', 'reason': 'No quarter-specific servicing uncertainty is established that would justify a manufactured question.'})
    bns['excluded_sources'] = [{'source_id': s, 'reason': 'The reader needs only the current segment report and the dated servicing disclosure to interpret this coverage boundary.'} for s in ['2','4','5']]
    bns['source_labels']['1'] = 'Q3 report: Canadian Banking, p. 23'
    bns['source_labels']['3'] = 'Covered-bond prospectus: servicing scope, p. 311'
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST/'editorial.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf8')
    selection = json.loads((SOURCE/'selection.json').read_text(encoding='utf8'))
    (DEST/'selection.json').write_text(json.dumps(selection, ensure_ascii=False, indent=2), encoding='utf8')
    control = json.loads((SOURCE/'source-insight-control.json').read_text(encoding='utf8'))
    for record in control['companies']:
        ticker = record['ticker']
        record['published_text'] = data['companies'][ticker]['call']['text']
        if ticker in ('TD','RY'):
            record.update(disposition='no_material_incremental_insight', insight_type='', why_it_matters='',
                          documented_reason_for_no_increment='The available call finding describes broader mortgage origination or bank margin performance, without a supported servicing operating, fee or retention implication. It does not justify a call section in this compact brief.')
        if ticker == 'BMO':
            record['why_it_matters'] = 'The rollout concerns mortgage renewal conversations in a portfolio with upcoming renewals. Investors can test adoption, completion, retention and borrower outcomes without assuming cost savings already occurred.'
    (DEST/'source-insight-control.json').write_text(json.dumps(control, ensure_ascii=False, indent=2), encoding='utf8')

if __name__ == '__main__':
    main()
