"""Encode root's explicit review of the frozen loop-1 PFSI HTML/text."""
from pathlib import Path
import hashlib
import json
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from servicing_brief.reader_value import inventory_html, require_reader_value
from servicing_brief.reader_value_release import text_review_html

# One decision for each inspected HTML block. This is a revision-specific record,
# not a function that approves newly generated financial content.
DECISIONS = {
1: ('The Servicing Brief', 'utility', 'publisher', 'Distinguish the independent brief from issuer communications.', 'The publisher name remains visible above issuer branding.'),
2: ('PFSI', 'utility', 'identity', 'Identify the traded issuer without relying on artwork.', 'Ticker identifies the precise company being reviewed.'),
3: ('Q2 2026', 'utility', 'period', 'Place earnings and publication in the correct period.', 'Release date and quarter prevent a current-date assumption.'),
4: ('PennyMac Financial Services wordmark', 'utility', 'identity', 'Recognize the issuer while keeping publisher identity distinct.', 'Official logo alternative text identifies the artwork when blocked.'),
5: ('PennyMac Financial Services', 'supporting', 'identity', 'Read the company identity even when images are blocked.', 'Real company text survives every image fallback condition.'),
6: ('Servicing earnings improved.', 'utility', 'direct_servicing', 'Identify the key change before reading detailed figures.', 'The adjacent chart qualifies improvement as quarterly, below last year.'),
7: ('Funding costs climbed.', 'supporting', 'direct_servicing', 'Notice the financing pressure alongside higher pretax earnings.', 'Interest expense rises from 125 to 140 million in the retained table.'),
8: ('Servicing pretax income', 'primary', 'direct_servicing', 'Assess earnings recovery against both quarter and year comparators.', 'Zero-based bars show 54, 13 and 22 million with explicit selected quarters.'),
9: ('Reported figures', 'utility', 'navigation', 'Find precise figures immediately after the visual comparison.', 'This heading separates lookup data from the earnings visual.'),
10: ('Measure Q2 2025', 'supporting', 'period', 'Read every number against the correct older-to-newer period.', 'Column headings explicitly match the chart chronological sequence.'),
11: ('Pretax income', 'supporting', 'direct_servicing', 'Look up the exact total used in the chart.', 'Deliberate chart/table repetition supports exact lookup without repeated prose.'),
12: ('Pretax before valuation', 'primary', 'direct_servicing', 'Separate reported performance from valuation and hedge effects.', '99 million before valuation explains the stronger operating component; reconciliation stays adjacent.'),
13: ('Valuation effects', 'supporting', 'direct_servicing', 'Reconcile before-valuation earnings to reported pretax results.', 'Negative 77 million reconciles 99 to 22; comparable signs remain explicit.'),
14: ('Interest expense', 'primary', 'direct_servicing', 'Assess the funding cost offset to improved operating earnings.', '140 versus 125 million substantiates the pressure statement and investor question.'),
15: ('Portfolio UPB', 'supporting', 'direct_servicing', 'Understand the servicing scale underlying earnings and financing balances.', '731 billion is total portfolio UPB; the adjacent footnote includes all disclosed components.'),
16: ('¹ Non-GAAP', 'supporting', 'definition', 'Avoid mistaking issuer adjustments and portfolio definitions for GAAP totals.', 'Non-GAAP reconciliation and owned/subserviced/held-for-sale scope qualify the exact rows.'),
17: ('m = million', 'supporting', 'definition', 'Interpret compact monetary scales and portfolio balance abbreviation.', 'Million, billion and unpaid principal balance resolve the table labels.'),
18: ('MSR = mortgage', 'supporting', 'definition', 'Understand the servicing-rights abbreviation in the cost explanation.', 'Expanding MSR helps readers interpret the financing mechanism below.'),
19: ('CPR = conditional', 'supporting', 'definition', 'Understand the annualized prepayment measure used in portfolio context.', 'CPR expansion distinguishes the prepayment measure from a retention outcome.'),
20: ('Why it moved', 'utility', 'navigation', 'Locate management explanation after examining the reported figures.', 'The heading introduces two sourced operating drivers rather than process commentary.'),
21: ('Management says slower', 'primary', 'direct_servicing', 'Understand the operating mechanism behind improved pre-valuation income.', 'Issuer explanation connects slower prepayments and custodial balances with income components.'),
22: ('Management attributed higher', 'supporting', 'direct_servicing', 'Understand why financing expenses increased despite earnings recovery.', 'The release attributes expense pressure to larger MSR financing balances.'),
23: ('Portfolio and risk', 'utility', 'navigation', 'Find exposure and liquidity facts after the earnings explanation.', 'The section separates portfolio behavior and liquidity from income drivers.'),
24: ('Prepayments and arrears.', 'supporting', 'direct_servicing', 'Assess portfolio runoff and borrower stress as separate measures.', '11.6 versus 13.7 CPR and 4.1 versus 4.2 delinquency use the owned portfolio; no retention inference.'),
25: ('Cash and borrowing capacity.', 'supporting', 'companywide_liquidity', 'Assess liquidity capacity alongside the timing of MSR margin requirements.', 'Four billion includes collateralized borrowing capacity, not all cash; same-day margin obligations explain its relevance.'),
26: ('Investor questions', 'utility', 'navigation', 'Distinguish open investor questions from reported company facts.', 'This heading prevents the following questions being read as management guidance.'),
27: ('What portion of servicing', 'supporting', 'direct_servicing', 'Test the durability of improved earnings through valuation and prepayment changes.', 'The question follows the 99-to-22 reconciliation without assuming earnings will persist.'),
28: ('How sensitive are servicing', 'supporting', 'direct_servicing', 'Test the return impact of the rising financing expense.', 'The question follows disclosed MSR balances and expense pressure without inventing sensitivity estimates.'),
29: ('Sources', 'utility', 'navigation', 'Locate original disclosures supporting the retained claims.', 'The source heading provides a clear transition to verification links.'),
30: ('[1] Earnings release', 'supporting', 'reference', 'Verify earnings, comparators and management explanations in the original release.', 'The release supplies the chart, table and two operating explanations.'),
31: ('[2] Presentation', 'supporting', 'reference', 'Verify owned-portfolio behavior and reported liquidity in the presentation.', 'The presentation supports the retained portfolio and liquidity paragraphs.'),
32: ('[3] Quarterly filing', 'supporting', 'reference', 'Verify the timing and nature of collateral margin obligations.', 'The 10-Q supplies the risk qualification on companywide liquidity.'),
33: ('[4] Current report', 'supporting', 'reference', 'Verify the earnings release event and furnished exhibits.', 'The 8-K anchors the publication date; it remains useful without a separate body claim.'),
34: ('Coverage notes', 'utility', 'navigation', 'Find limitations that affect reliance on this brief.', 'The heading introduces the specific missing-call access boundary.'),
35: ('Full call transcript not located', 'supporting', 'availability', 'Know that the brief cannot establish complete call or Q&A coverage.', 'Blocked issuer access remains explicit; absence is not misrepresented as nonpublication.'),
36: ('Sources as of', 'supporting', 'period', 'Know the cutoff for newly available source documents.', 'The precise evidence cutoff bounds the missing-call statement and subsequent updates.'),
}

TEXT_MAP = [1,3,5,6,7,8,8,8,8,8,8,9,10,11,12,13,14,15,16,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,30,31,31,32,32,33,33,34,35,36]

def main():
    out = ROOT/'output/brief-improvement'
    reviews = out/'reader-value-reviews'
    reviews.mkdir(exist_ok=True)
    for text_mode in (False, True):
        source = (out/f'PFSI-review.{"txt" if text_mode else "html"}').read_text(encoding='utf8')
        html = text_review_html(source) if text_mode else source
        blocks = inventory_html(html)
        assert len(blocks) == (44 if text_mode else 36)
        items=[]
        for index, block in enumerate(blocks, 1):
            key = TEXT_MAP[index-1] if text_mode else index
            prefix, priority, scope, need, value = DECISIONS[key]
            if not text_mode:
                assert block['text'].startswith(prefix), (index, block['text'])
            items.append(dict(id=block['id'], inventory_text=block['text'], verdict='keep',
                priority=priority, scope=scope, reader_need=need, incremental_value=value,
                inclusion_logic='Root inspected this exact rendered item against the archived PFSI release, presentation and 10-Q. '+value,
                reason=need+' '+value))
        record=dict(version=1, priority='P0', objective='mortgage_servicing',
                    reviewer='Root, explicit loop-1 PFSI review', document_kind='analysis',
                    html_sha256=hashlib.sha256(html.encode()).hexdigest(), items=items)
        path=reviews/f'PFSI-review{"-text" if text_mode else ""}.json'
        path.write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf8')
        result=require_reader_value(html,record)
        print(path.name, result['status'], len(items))

if __name__=='__main__': main()
