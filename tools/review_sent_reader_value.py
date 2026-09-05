"""Record a human editorial audit of the exact sent five-company PDF.

The decisions below are explicit reviewer judgments, not a scoring model.
The script fails when the reviewed block IDs/text change. It never edits a brief.
"""
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servicing_brief.reader_value import inventory_html, evaluate_reader_value

BASE = ROOT / 'output/five-company-review'
OUT = ROOT / 'output/reader-value-review'

# id: expected prefix, disposition, scope, reader need, judgment/action.
D = {
'TD': {
5: ('Mortgage and HELOC', 'revise', 'mortgage_credit', 'Identify the current mortgage-credit development.', 'Lead with the arrears change and its nine-month interval. Canadian lending and groupwide arrears are different populations; the headline supplies no servicing-performance finding.'),
6: ('TD’s Canadian', 'remove', 'mortgage_credit', 'Orient the reader without repeating the headline.', 'Repeats the headline and chart direction without a new implication; omit the deck rather than fill the slot.'),
7: ('Early mortgage arrears', 'keep', 'mortgage_credit', 'Assess the amount of mortgage exposure in early arrears.', '536 to 631 C$m is a useful absolute exposure comparison. Retain dates, 31-to-89-day definition, groupwide scope and zero baseline; do not call it an arrears rate or servicing failure.'),
11: ('Mortgages + HELOCs', 'revise', 'mortgage_lending', 'Understand the relevant portfolio scale.', '415.396 to 422.214 C$bn describes Canadian lending, not serviced UPB and not a denominator for groupwide arrears. Keep only if a specific scale/composition explanation needs it; otherwise omit.'),
12: ('Amortizing mortgages', 'revise', 'mortgage_lending', 'Understand a material change in mortgage composition.', '267.469 to 245.012 C$bn moves differently from the combined portfolio. The brief gives no composition or classification explanation. Resolve the basis and explain the divergence before interpreting it; otherwise omit.'),
13: ('Early mortgage arrears', 'keep', 'mortgage_credit', 'Look up exact values underlying the credit-exposure visual.', 'The 536/631 C$m row is an intentional chart-to-table lookup, not extra prose. The red signal can denote higher absolute exposure only, not a demonstrated decline in servicer effectiveness.'),
17: ('Allowance for loan losses', 'revise', 'mortgage_credit', 'Distinguish credit reserves from realized losses and servicing expense.', '345 to 430 C$m is a reserve stock, compared year over year. Add that interpretation and a relevant mortgage-credit connection; do not imply this is servicing cost or current-period loss expense.'),
18: ('Portfolio mix.', 'revise', 'mortgage_credit', 'Understand payment-reset and collateral-risk exposure.', '46% variable-rate and 13% insured can frame payment sensitivity and loss protection. Explain that connection and the mortgage-plus-HELOC population. 90% amortizing is a static mix fact with no independent change explained; omit it unless needed for that interpretation.'),
19: ('Credit profile.', 'remove', 'mortgage_credit', 'Assess current borrower stress.', 'A 792 average bureau score and five-year average loss rate near one basis point do not explain the current arrears increase. Historical averages can obscure deterioration; no comparable current trend or threshold is supplied.'),
21: ('At Oct 31, 2025', 'revise', 'annual_mixed', 'Know what can and cannot be concluded about servicing economics.', 'The C$139m fair value and C$75m carrying value are old U.S. rights values without a current bridge or materiality context. Omit those values here; retain a concise current-quarter disclosure limitation so lending figures cannot be mistaken for servicing earnings.'),
23: ('Sona Mehta', 'remove', 'broader_bank', 'Understand a change in servicing revenue, cost, retention or borrower operations.', 'Originations, disciplined pricing and margin expansion are new mortgage-banking text but establish no servicing outcome. A call section is optional; do not keep this merely because it came from a transcript.'),
25: ('Can TD disclose', 'revise', 'direct_servicing', 'Ask a question tied to the observed operating risk.', 'The general request for fee income, UPB and delinquency disclosure is not specific to the quarter. Focus on what drove early arrears, cure behavior and related servicing workload; ask about effects without presuming them.'),
},
'RY': {
5: ('Mortgage lending grows', 'revise', 'broader_bank', 'Identify a servicing-relevant development rather than a broad bank summary.', 'Loan growth and Personal Banking margin narrowing are not servicing earnings. A primary headline needs a mortgage-credit, retention, cost or servicing-performance finding; otherwise hold this as a mortgage-banking context note.'),
6: ('Canadian mortgage lending', 'revise', 'broader_bank', 'Separate the relevant credit signal from segment-profit narration.', 'The deck repeats lending, income and margin movements. Retain only a supported useful credit interpretation, without treating the 43-to-42 bp movement as material by itself.'),
7: ('Canadian mortgage lending', 'remove', 'mortgage_lending', 'See an important operating change that a chart clarifies.', '461.384 to 473.303 C$bn is loan exposure, not serviced UPB. A two-bar chart adds no demonstrated servicing decision beyond the same table row; do not choose it just to supply a visual.'),
11: ('Residential mortgage lending', 'revise', 'mortgage_lending', 'Understand scale where it explains mortgage-credit exposure.', 'Keep the loan balance only as supporting lending context with an explicit decision bridge. It is not evidence of servicing contract growth, fee growth or serviced volume.'),
12: ('Personal Banking net income', 'remove', 'broader_bank', 'Assess mortgage-servicing profitability.', '1,774 to 1,826 C$m spans the entire Personal Banking business. No servicing contribution or cost bridge is given; a green highlight cannot turn it into servicing performance.'),
13: ('Personal Banking net interest margin', 'remove', 'broader_bank', 'Assess servicing economics without confusing loan spread with servicing fee yield.', '2.65% to 2.62% is a broad banking margin. Remove the red earnings signal from this servicing brief unless a supported causal bridge is provided.'),
14: ('Uninsured mortgage loan-to-value', 'remove', 'mortgage_credit', 'Understand an actionable change in collateral protection.', '62% in both periods is a static average, excludes Homeline and gives no tail distribution, threshold or change. Its existence in the source is insufficient for inclusion.'),
15: ('Lending scope.', 'revise', 'mortgage_lending', 'Prevent lending balances from being misread as serviced UPB.', 'Retain the lending-versus-servicing distinction if the balance is retained. The C$13bn commercial-client and C$18bn securitization details are unnecessary unless they explain the retained finding; otherwise keep them in evidence.'),
16: ('Credit signal.', 'revise', 'mortgage_credit', 'Assess mortgage-credit direction and materiality.', '42 versus 43 basis points is mortgage-specific and potentially useful, but one basis point alone does not establish a material improvement. Supply a longer comparable trend or a concrete monitoring implication; do not promote a tiny move to a major finding.'),
18: ('The covered-bond agreement', 'revise', 'eligibility', 'Know the limits of the available servicing-profit information.', 'Naming RBC as Servicer belongs in universe eligibility. Remove that clause from the analysis; retain the limitation that broader banking results do not isolate servicing earnings. Qualification does not make the unrelated numbers useful.'),
20: ('In prepared remarks', 'remove', 'broader_bank', 'Understand a forward-looking development in mortgage servicing.', 'Astra review: the Canadian Banking margin outlook is sourced but has no demonstrated servicing-fee, retained-contract, cost or borrower-operations connection. Remove from this brief. Novel financial text is not sufficient reader value; reconsider only with new evidence establishing a material connection.'),
22: ('How did Canadian', 'revise', 'direct_servicing', 'Ask a falsifiable question grounded in the actual finding.', 'Loan growth does not establish growth in retained servicing contracts. Tie the question to mortgage competition and renewal retention or disclosure boundaries without assuming that link.'),
25: ('[2] Earnings release', 'remove', 'reference', 'Verify a retained claim.', 'No retained body claim cites source 2. Keep the full source collection internally; omit an unused source link from the short reader list.'),
28: ('[5] Call details', 'revise', 'reference', 'Verify the release/call date and source availability.', 'Useful for event identity, but its inline citation follows an outlook that comes from the remarks PDF. Attach this reference to the date or availability note, not to the outlook assertion.'),
},
'CM': {
5: ('Banking profit rises', 'revise', 'broader_bank', 'Identify the mortgage-credit development.', 'Lead with Canadian mortgage delinquency rather than whole-bank profit. A broad profit rise does not explain servicing operations or offset a delinquency signal.'),
6: ('Canadian banking profit', 'remove', 'broader_bank', 'Get additional information after the headline.', 'Repeats the headline and imports broad margin performance. No incremental servicing explanation is added.'),
7: ('Mortgage delinquency', 'keep', 'mortgage_credit', 'Monitor the proportion of Canadian mortgages materially past due.', '0.47% to 0.51% is a comparable 90-plus-day delinquency rate. It is useful borrower-stress evidence; it does not prove a change in servicing quality or the amount of servicing expense.'),
11: ('Banking net income', 'remove', 'broader_bank', 'Evaluate servicing profit.', '846 to 948 C$m is whole Canadian Personal and Business Banking income. No servicing attribution is established; omit its green-highlighted row.'),
12: ('Banking net interest margin', 'remove', 'broader_bank', 'Evaluate servicing fee economics.', '3.12% to 3.16% is broad segment interest margin, not servicing fee yield. It has no demonstrated contribution to the delinquency or servicing-cost finding.'),
13: ('90-plus-day delinquency', 'keep', 'mortgage_credit', 'Look up the exact borrower-stress rate behind the chart.', 'The 0.47%/0.51% row intentionally repeats the chart as a precise reference. Keep the Canadian mortgage population and quarterly dates; the red signal denotes higher delinquency only.'),
17: ('Uninsured residential mortgages', 'remove', 'mortgage_lending', 'Understand the scale of the population behind the finding.', '239.4 to 243.1 C$bn is all-geography uninsured lending, whereas the delinquency rate is Canadian. It cannot supply the relevant denominator and no other servicing decision is explained.'),
18: ('Margin drivers.', 'remove', 'broader_bank', 'Understand the driver of a servicing result.', 'The 25-bp year-over-year margin explanation concerns mix, pricing and interest rates for the broader segment. It was available presentation commentary, not an explanation of servicing economics.'),
19: ('Credit signal.', 'keep', 'mortgage_credit', 'Interpret delinquency increases without equating arrears with realized loss.', 'The issuer says losses remain low while slower home sales affect delinquency. This adds a useful explanation and distinguishes the delinquency signal from realized loss; retain issuer attribution.'),
21: ('CMHC lists', 'revise', 'eligibility', 'Understand measurement limits without reading screening mechanics.', 'Eligibility as a covered-bond servicer is a universe-screening fact. Move it internally. Keep only the concise limitation needed to avoid mistaking lending-credit measures for servicing earnings or servicing effectiveness.'),
23: ('How is CIBC', 'revise', 'direct_servicing', 'Ask about the risk actually shown in the brief.', 'Remove the unsupported link to expanding banking margins. Ask how slower home sales and rising 90-plus-day mortgage delinquency affect cure timelines, borrower assistance and servicing workload.'),
28: ('[3] CMHC registry', 'remove', 'reference', 'Verify a reader-facing financial finding.', 'The registry supports screening eligibility, not a retained financial claim. Preserve it in the universe record rather than the brief sources.'),
29: ('[4] Earnings release', 'remove', 'reference', 'Verify retained analysis.', 'No body claim cites source 4. It is a collection-completeness link, not necessary reader verification.'),
},
'BNS': {
5: ('Mortgage balances ease', 'revise', 'broader_bank', 'Identify a material servicing-related development.', 'The headline pairs loan balances with whole-bank earnings. No new servicing-specific finding is established; do not fabricate a stronger headline to fill the company slot.'),
6: ('Canadian mortgage balances', 'remove', 'broader_bank', 'Learn something beyond the headline.', 'Repeats lending, earnings and margins with no added implication.'),
7: ('Canadian mortgage lending', 'remove', 'mortgage_lending', 'See a change that matters to servicing operations.', '315.063 to 311.209 C$bn is loan exposure and a small change. There is no serviced-volume, retention or revenue bridge; this was a chart chosen from available comparable numbers.'),
11: ('Residential mortgage lending', 'revise', 'mortgage_lending', 'Understand exposure scale only when tied to a useful finding.', 'The lending balance cannot be compared directly with the old serviced-UPB disclosure. Retain only if an actual lending-versus-servicing reconciliation explains a reader decision; otherwise omit.'),
12: ('Banking net income', 'remove', 'broader_bank', 'Assess servicing profitability.', '935 to 1,071 C$m is broad Canadian Banking income. The green highlight imports an unrelated success signal into a servicing brief.'),
13: ('Banking net interest margin', 'remove', 'broader_bank', 'Assess servicing revenue/cost economics.', '2.36% to 2.38% is interest margin for the banking segment. No servicing fee or expense contribution is shown.'),
14: ('Uninsured mortgage loan-to-value', 'remove', 'mortgage_credit', 'Understand a material collateral-risk change.', '56% in both periods is an unchanged average with no threshold or risk-distribution explanation. Omit static context that changes no current assessment.'),
15: ('Margin momentum.', 'remove', 'broader_bank', 'Understand a current servicing operating change.', 'Fifth margin increase, 3% average loan growth and 4% average mortgage growth are broader banking facts. The average/year-over-year versus period-end/quarter distinction is correct but only needed because unrelated comparisons were included.'),
16: ('Earnings drivers.', 'remove', 'broader_bank', 'Explain a servicing result.', 'Higher revenue and lower credit provisions are generic whole-segment earnings drivers. They do not identify a servicing lever, contract change or operational outcome.'),
18: ('The prospectus describes', 'revise', 'annual_mixed', 'Establish a usable servicing-volume baseline.', 'The 1,004,267 loans and C$292.37bn are directly servicing-related, unlike lending balances. They are dated Oct 31, 2025 and no update/reconciliation is shown. Keep only as an explicitly needed historical benchmark, not a new Q3 finding. Generic collection/escrow/foreclosure duties add no company-specific change.'),
20: ('How is Scotiabank', 'revise', 'direct_servicing', 'Ask a concrete question tied to available servicing evidence.', 'Replace the unexplained NIM-to-retention-cost link with a question about changes in the historical serviced-loan population and comparable servicing economics, if that benchmark is retained.'),
26: ('[4] Earnings release', 'revise', 'reference', 'Verify retained statements.', 'Retain only if a useful claim remains after the generic earnings-drivers paragraph is removed.'),
27: ('[5] Presentation', 'revise', 'reference', 'Verify retained operating information.', 'Retain only if a meaningful presentation finding replaces the broad banking-momentum filler; document availability alone is insufficient.'),
},
'BMO': {
5: ('Mortgage balances grow', 'revise', 'mortgage_credit', 'Identify the most useful operating issue.', 'Renewal exposure and borrower payment changes are stronger servicing-relevant findings than generic balance growth. Also avoid pairing quarterly lending growth with nine-month impaired-balance growth as if both measured the same interval.'),
6: ('Residential mortgage balances', 'remove', 'mortgage_credit', 'Learn an incremental implication after the headline.', 'This sentence repeats the headline and adds no explanation.'),
7: ('Impaired mortgage balances', 'keep', 'mortgage_credit', 'Monitor the stock of mortgages with established credit impairment.', '903 to 1,135 C$m is a useful absolute workout-exposure signal. Keep groupwide scope and Oct-to-Jul dates. It does not establish impairment rate, new quarterly defaults or servicing quality.'),
11: ('Total residential mortgages', 'revise', 'mortgage_lending', 'Understand lending exposure where it explains the retained credit signal.', '193.816 to 196.924 C$bn can be scale context, but the April comparison is not the October baseline of impaired loans. Do not compute or imply a matching impairment-rate trend from these pairs.'),
12: ('Canadian residential mortgages', 'revise', 'mortgage_lending', 'Understand the Canadian renewal population.', '162.090 to 164.183 C$bn is the most relevant of the three overlapping lending rows for Canadian mortgage renewals. Keep at most the necessary scale reference and explain the population connection; do not imply a count of renewals.'),
13: ('Canadian mortgages + HELOCs', 'remove', 'mortgage_lending', 'Understand Canadian mortgage renewal exposure.', '215.629 to 218.797 C$bn includes HELOCs and is broader than the mortgage-renewal population. It duplicates scale without helping the renewal finding.'),
17: ('Gross impaired balances', 'keep', 'mortgage_credit', 'Look up the chart values and compare credit-stage exposure.', '903/1,135 C$m is a useful exact reference for the impaired stock. Red denotes higher absolute impaired exposure only; preserve nine-month interval and groupwide scope.'),
18: ('30 to 89 days past due', 'revise', 'mortgage_credit', 'Distinguish early arrears from impaired loans.', '854 to 846 C$m provides stage context, but a small decline cannot be read as cures or operational improvement while impaired balances grow. Remove an unqualified green success signal; explain possible migration/denominator limits without asserting their cause.'),
19: ('Renewal exposure.', 'keep', 'direct_servicing', 'Plan borrower outreach and renewal-processing capacity.', '22% of Canadian mortgage balances renewing in the next 12 months identifies a specific exposure and time horizon. The outreach/capacity implication is a bounded inference, not a measured cost increase or loan-count forecast.'),
20: ('Payment relief.', 'keep', 'mortgage_credit', 'Assess borrower payment pressure at renewal.', 'Nearly half of Q3 renewals had lower payments. This directly informs the payment-pressure narrative while correctly separating it from servicing fees; it does not imply all borrowers benefit or quantify future arrears.'),
22: ('FY2025 retained U.S.', 'remove', 'annual_mixed', 'Assess current mortgage-only servicing economics.', 'C$170m fair value/C$146m carrying value mixes U.S. mortgage and RV rights and is annual. No current bridge, allocation or materiality is supplied. A disclaimer cannot manufacture relevance.'),
24: ('Darryl White', 'revise', 'direct_servicing', 'Understand a concrete change in renewal operations.', 'The Lumi rollout is potentially useful channel/operating-strategy information, not mere technology news. Tie it to the renewal-capacity finding and ask for adoption, completion or retention outcomes; do not imply savings or improved service already occurred.'),
26: ('Will BMO disclose', 'revise', 'direct_servicing', 'Ask a question the retained quarter-specific facts motivate.', 'The question is dominated by the mixed annual rights population. Focus instead on renewal completion, retention, payment outcomes and workload alongside the rollout and 22% exposure.'),
31: ('[4] FY2025 annual report', 'remove', 'reference', 'Verify a retained company finding.', 'Only the excluded mixed annual rights paragraph needs this source here. Preserve the original in evidence and omit the unused reader link.'),
}}

LOGIC = {
 'metric': 'Observed: render_email._metric_groups and audit_redesign required every canonical metric exactly once. Source coverage and numeric fidelity took precedence over an editorial exclusion decision.',
 'chart': 'Observed: add_charts.CHARTS supplied a comparable pair for every company; render_email._chart checked finite values/labels and proportions, not whether the visual changed a servicing assessment.',
 'headline': 'Observed: editorial.json supplied a short headline; audit_redesign enforced a 12-word ceiling, not a servicing-specific reader question.',
 'default': 'Observed: editorial.json supplied this text; audit_redesign enforced source IDs and word limits and exactly two insights. Inference: availability, novelty or template completion was treated as sufficient reader value.',
 'call': 'Observed: editorial_control accepted broad substantive markers and a written rationale, plus provenance. New sourced financial text could pass without a servicing decision bridge.',
 'question': 'Observed: render_email used editorial question or canonical fallback. No check required a question to follow from a retained quarter-specific finding.',
 'reference': 'Observed: renderer emitted every canonical source label, including eligibility and uncited materials, to preserve collection coverage.'}

def main():
    OUT.mkdir(exist_ok=True)
    proof=json.loads((OUT/'sent-pdf-verification.json').read_text(encoding='utf8'))
    expected={row['ticker']:row['html_sha256'] for row in proof['pages']}
    assert all(not row['pdf_only'] and not row['html_only'] for row in proof['pages'])
    assert hashlib.sha256((ROOT/'output/pdf/Servicing-Briefs-2026-09-05.pdf').read_bytes()).hexdigest()==proof['pdf_sha256']
    controls=BASE/'reader-value-reviews';controls.mkdir(exist_ok=True)
    summary={}; all_records={}
    for ticker, decisions in D.items():
        html=(BASE/f'{ticker}-review.html').read_text(encoding='utf8')
        assert hashlib.sha256(html.encode()).hexdigest()==expected[ticker], 'Sent-artifact audit must not silently review replacement content'
        units=inventory_html(html);records=[]
        # Utility IDs are explicitly limited to the actual identity/date,
        # section captions, table headings, source links and availability note.
        for u in units:
            number=int(u['id']);text=u['text'];entry=decisions.get(number)
            if entry:
                prefix, verdict, scope, need, reason=entry
                assert text.startswith(prefix),(ticker,number,text)
                priority='primary' if u['kind'] in {'chart','headline'} or (ticker=='BMO' and number==19) else 'supporting'
                logic=LOGIC.get(u['kind'],LOGIC['default'])
                if number in {'TD':{23},'RY':{20},'CM':set(),'BNS':set(),'BMO':{24}}[ticker]:logic=LOGIC['call']
                if text.endswith('?'):logic=LOGIC['question']
                increment=reason
            else:
                is_utility=(number<=4 or u['kind']=='heading' or text.startswith(('Metric ', '[', 'Webcast available;', 'Attached:')) or not re.search(r'[.!?]',text.rstrip('.')) and len(text.split())<21)
                assert is_utility,('UNREVIEWED CONTENT',ticker,number,text)
                verdict='keep';scope='reference' if text.startswith('[') else 'qualification';priority='utility'
                need='Identify the company, event, units, comparison basis or supporting source accurately.'
                reason='Retain this exact identity/date, navigation label, table basis or source/availability utility. It is not claimed as an investment insight; update/remove it if its associated content is removed.'
                increment='Allows the reader to orient, interpret the retained measure or verify its source without replacing an operating finding.'
                logic='Observed: template supplies identity/date, table scope and source navigation. This is necessary reader utility, not justification for keeping unrelated analysis.'
            records.append({**u,'verdict':verdict,'scope':scope,'priority':priority,'reader_need':need,
                            'incremental_value':increment,'inclusion_logic':logic,'relevance_bridge':reason,
                            'reason':reason,'in_sent_pdf':not text.startswith('Attached:')})
        review={'version':1,'priority':'P0','html_sha256':hashlib.sha256(html.encode()).hexdigest(),
                'reviewer':'Root substantive review; independent Astra review for RY/BMO and cross-company premise',
                'objective':'mortgage_servicing','items':records}
        (controls/f'{ticker}-review.json').write_text(json.dumps(review,indent=2,ensure_ascii=False),encoding='utf8')
        evaluation=evaluate_reader_value(html,review)
        all_records[ticker]=review
        summary[ticker]={'sent_pdf_blocks':sum(r['in_sent_pdf'] for r in records),
                         'decisions':dict(Counter(r['verdict'] for r in records if r['in_sent_pdf'])),
                         'gate_status':evaluation['status'],'blockers':evaluation['blockers']}
    (OUT/'item-review.json').write_text(json.dumps(all_records,indent=2,ensure_ascii=False),encoding='utf8')
    (OUT/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf8')
    lines=['# Review of the delivered servicing PDF','',
      'Every displayed block is reviewed below, including dates, chart labels, financial values, table bases, sources and availability qualifications. The sent PDF is preserved. Its text matches the five reviewed HTML bodies after the email-only attachment footer is excluded.','',
      'Keep means useful in its stated role, not proof that the whole company brief is ready. Revise/remove findings are P0 release blockers. Source accuracy and visual approval do not establish reader value. No replacement email is authorized or sent.','']
    for ticker,review in all_records.items():
        lines.extend([f'## {ticker}','', '| ID | Sent content | Decision and reader value | Inclusion logic |','|---|---|---|---|'])
        for r in review['items']:
            if not r['in_sent_pdf']:continue
            clean=lambda s:s.replace('|','/').replace('\n',' ')
            lines.append(f"| {r['id']} | {clean(r['text'])} | **{r['verdict']}**: {clean(r['reason'])} | {clean(r['inclusion_logic'])} |")
        lines.append('')
    (OUT/'REVIEW.md').write_text('\n'.join(lines),encoding='utf8')
    print(json.dumps({t:{k:v for k,v in s.items() if k!='blockers'} for t,s in summary.items()}))

if __name__=='__main__':main()
