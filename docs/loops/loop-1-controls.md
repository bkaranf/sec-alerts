# Loop 1: content and UI control audit

This is a bounded audit of the controls after the P0 reader-value gate was
added. It uses the current renderer, release adapter, reader-value gate, and
the findings in `output/reader-value-review/FINDINGS.md` and
`output/reader-value-review/astra-review.md`. No rendered artifact or canonical
evidence is changed here.

The P0 gate is effective at binding a review to the exact HTML and at holding
`keep`/`revise`/`remove` decisions that have not been resolved. It still leaves
five concrete gaps for a lean servicing-profitability email.

## 1. A truthful coverage-only note cannot be represented safely

**Location:** `servicing_brief/reader_value.py:308-365`, especially the
`has_primary_content` requirement; `output/five-company-review/render_email.py:501-518`
and `:581-636`; `servicing_brief/reader_value_release.py:24-45`.

**Defect and effect:** The gate requires at least one kept non-utility item
with `priority: primary`. A concise note saying that reviewed materials do not
separately report mortgage-servicing earnings is a useful coverage result, but
it is not a primary financial finding. Marking that paragraph `supporting`
blocks release; marking it `primary` or `mortgage_servicing` misstates what it
is. The renderer also requires a canonical headline and summary even when the
overlay is trying to publish only a coverage note. This is the exact failure
needed for RY/BNS-style notes with no supported servicing-profitability
finding.

**Smallest safe fix:** Add an explicit review-level `publication_mode` such as
`coverage_note` (or an equivalent `primary_finding_present: false`) and a
typed coverage-note item. In that mode, require one bounded, source-backed
coverage paragraph, allow zero primary financial blocks, and require that
metrics, chart, insight, call, and question sections are either absent or
explicitly omitted. Keep the ordinary primary-finding requirement for normal
briefs. Do not make the exception a generic “all utility” approval path.

**Loop 1 implementation:** `review.document_kind` now defaults to `analysis`
and accepts `coverage_note` as the only alternative. Coverage mode is bound to
the rendered HTML: exactly one company section must carry
`data-brief-kind="coverage_note"`, it must contain a visible `Coverage note`
label, and it cannot contain a chart or financial table. Every reviewed item
still needs the existing exact text/hash, complete inventory, and `keep`
decision checks; any `primary` item is rejected. At least one kept,
non-utility `supporting` body item must use the exact `coverage_boundary` scope
and contain a public HTTPS source link. Analysis documents retain the original
primary-content requirement. The evaluation result explicitly records
`human_judgment_required: true`, so a structurally valid but semantically weak
coverage sentence is still a reviewer decision. Plain-text callers must apply
the same document-kind structure and preserve HTTPS URLs in the inventory.

## 2. Paragraph and metric scope are reviewer prose, not enforced claim metadata

**Location:** `output/five-company-review/render_email.py:412-489` and
`:538-576`; `:603-616`; `servicing_brief/reader_value.py:334-347`.

**Defect and effect:** The one-off renderer allows an overlay to replace a
metric's unit and to place any canonical rows under arbitrary group scope and
period labels. It only checks that values are present and that every metric is
displayed or explicitly excluded. A paragraph's P0 `scope` is a free-form
string; only three exact broad-scope spellings trigger a bridge check. No
render-time or P0 check binds a paragraph or row to the evidence's period,
geography, population, weighting, denominator, measure type, or definition.
That permits the already observed category errors: lending UPB framed as
servicing, a stale annual mixed population framed as current-quarter context,
or an unmatched period used as a comparison.

**Smallest safe fix:** Make each rendered metric and analytical paragraph carry
an immutable evidence/claim reference plus machine-readable period, scope,
population, measure type, unit, and denominator/weighting fields. Permit an
overlay to select or reorder rows, but reject unit, period, or scope changes
unless the referenced evidence proves the replacement. Validate group rows for
compatible scope and period using the existing evidence compatibility rules.
Keep the human P0 rationale, but do not use it as the scope proof.

## 3. Public copy and internal editorial decisions are not separated by type

**Location:** `output/five-company-review/render_email.py:692-704` and
`:813-824`; `output/five-company-review/email-template.html.j2:460-475`;
`servicing_brief/reader_content.py:32-37` and `:98-182`.

**Defect and effect:** Omission reasons are kept out of the normal template,
but arbitrary `reader_note` and `attachment_note` values from selection
metadata/CLI arguments are emitted directly into the HTML and text. The
reader-content check rejects only four exact research-process phrases. Text
such as “No retained claim requires this annual document,” “Review status:
blocked,” or “Internal omission reason: this broad bank metric has no servicing
bridge” passes the check. A future caller can therefore publish a research
decision, review status, or exclusion rationale as a coverage note. This
contradicts the standard's requirement that those decisions stay internal.

**Smallest safe fix:** Split public fields from internal metadata at the input
contract. Render only typed public `coverage_note`, `availability_note`, and
`attachment_note` values with an explicit public purpose; keep omission,
review, and exclusion reasons in the internal review/evidence record. Make the
renderer reject the internal fields rather than relying on a keyword denylist.
The P0 record should identify whether a utility note is public copy, while its
editorial rationale remains internal.

## 4. Source IDs and public URLs are not claim-level traceability

**Location:** `output/five-company-review/render_email.py:184-226` and
`:229-240`; `output/five-company-review/email-template.html.j2:441-465`;
`servicing_brief/reader_value.py:349-355`.

**Defect and effect:** `_source_records` preserves a source `location` in the
in-memory record, but the template publishes only a short label and URL. The
`_references` helper checks only that an ID exists. The P0 gate checks item
text/hash, but ignores the review item's `links` and has no required claim
reference, source location, excerpt, or archived-byte hash. A paragraph can
therefore be marked source-backed, or a citation can be attached to the wrong
claim, while the gate still approves it. This repeats the Astra audit's
RY/management-comments concern: document-level presence is not proof for the
specific assertion.

**Smallest safe fix:** Add a per-item `claims`/`evidence_refs` field to the
review record and require it for every financial, operating, outlook, and
coverage item. Validate source ID, issuer, period, scope, location, excerpt,
and archived hash against canonical evidence; reject links that are not the
declared public URL for those claims. Keep local paths, hashes, and full
excerpts internal, while retaining the public URL and optionally a page/section
anchor in the reader-facing citation.

## 5. Highlight styling still has no materiality or servicing-bridge control

**Location:** `output/five-company-review/render_email.py:542-561` and
`:553-574`; `servicing_brief/reader_value.py:329-345`.

**Defect and effect:** The renderer accepts canonical `green`/`red` highlights
after checking only the allowed values, source presence, and at-most-one-per-
color count. The P0 record has no required materiality basis, prominence
decision, or evidence link for the color. A small movement or a broad bank
profit/NIM row can still receive the visual authority of a servicing
improvement or pressure point if a reviewer marks the row `keep`. This is the
same availability/direction substitution identified as O5/I4 in the Astra
review and is especially misleading in a lean email where color carries much
of the argument.

**Smallest safe fix:** Treat highlight as an explicit editorial decision with
`highlight_reason`, `materiality_basis`, and a claim/evidence reference. Permit
no highlight by default and require a documented servicing-economics,
borrower-risk, retention, or operating-execution implication for each color.
Reject highlights on broad or mixed scope unless the record has a concrete
bridge, and allow a brief with zero colored rows.

These controls should be implemented against the revised local draft and then
re-reviewed on its actual desktop, mobile, HTML, and plain-text outputs. The
coverage-note exception must remain narrow enough that it cannot be used to
rescue the bank metrics and generic questions already rejected in the Astra
audit. Loop 1's gate and regression tests now cover the bounded HTML contract;
the release adapter still owns passing the same `document_kind` and linked
HTTPS URLs into plain-text review. Targeted reader-value tests and the full
suite pass (`18` and `177` tests, respectively).
