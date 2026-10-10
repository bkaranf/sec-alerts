# Reliability review, October 9, 2026

This engineering review continues the general project-improvement goal. It does
not replace the historical source, financial, editorial or visual acceptance
records, and does not authorize email delivery or recurring execution.

The connected checkout is `C:\dev\ChatGPT\SEC Alerts`. The prior OneDrive
workspace path no longer exists. Development started from commit `11fb555`, with
a clean checkout matching `origin/codex/lightweight-refactor-20260905`, on the new
branch `codex/reliability-20261009`.

## Recovery and scheduling changes

- Rebuilding a withdrawn unsent draft allocates a new report ID and directory.
  The withdrawn database payload and generated files remain audit history. A
  replacement requires its own editorial approval. Saving a duplicate durable
  report ID now fails instead of silently reassociating source documents.
- Rediscovery can repair a missing or corrupt archive location using verified
  identical bytes. Document ID, reporting company, source URL and content hash
  must agree. Matching image-asset paths are repaired separately. Original
  retrieval/discovery dates, reviewed metadata and first-seen history remain
  unchanged, so recovery cannot reorder financial document revisions.
- Outstanding reports resolve repaired annual-context attachment paths during
  preparation. This updates the in-memory package input without rewriting saved
  report evidence or reviewed HTML/plain text.
- Scheduling compares UTC instants during the repeated fall daylight-saving
  hour. A first-occurrence slot remains due when the wall clock repeats an earlier
  time. The first occurrence still runs once; nonexistent spring times remain
  skipped.

## Configuration and original attachments

- Configuration failures now identify malformed tables, fields, company entries,
  addresses, schedule times, booleans and numeric limits before execution. TOML
  syntax and file-read failures return safe configuration errors without echoing
  file contents. Existing defaults, numeric CIKs and consumer limit clamping remain
  supported. A company without an explicit enabled flag stays disabled consistently
  in both collection and reporting.
- An HTML image deck with no collected assets no longer bypasses completeness
  checks. Incomplete copies retain the exact original HTML and source link, with
  an omission notice in both email alternatives. Responsive image references,
  embedded data images, external URLs and HTML base paths are checked within the
  flat image-bundle contract. This is not a general CSS, SVG or website archiver.
- Attachment deduplication now compares the completed payload. Two disclosures
  with identical HTML wrappers and different verified images retain both ZIP
  copies. Identical completed bundles still deduplicate. Tests decode the actual
  MIME attachments and check image bytes and omission text.

The responsive-image URL handling follows the token boundaries in the
[HTML standard's srcset parser](https://html.spec.whatwg.org/multipage/images.html#parse-a-srcset-attribute).

## Verification

The baseline `uv run --extra sec pytest -q` completed with 420 passed and 3 skipped.
Focused regression tests cover draft withdrawals, exact artifact preservation,
archive and image recovery, provenance guards, annual-context send preparation,
and both daylight-saving transitions. External delivery is mocked.
The focused run of `tests/test_state_pipeline.py`,
`tests/test_pipeline_integration.py` and `tests/test_scheduling.py` passed all 38
tests. Astra independently inspected the implementation and regression coverage.
Configuration and attachment changes also received independent code review;
attachment review included a separate real-MIME probe with filesystem writes and
delivery mocked.

Final combined verification: `uv run --extra sec pytest -q -rs` passed **498
tests**, with **3 skipped** because the local PFSI Q2 source archive required by
`tests/test_reviewed_context.py` is unavailable. `git diff --check` passed. These
skips are an explicit evidence gap, not verified archived-context behavior on this
checkout.

No source refresh, live email send, deployment or Task Scheduler activation was
performed for this review. Historical rendered-brief approvals do not certify
changed future output.

## Open follow-up findings

The independent review identified additional recovery cases that require further
implementation and verification before the broad improvement goal is complete:

1. A previously pending document for an older reporting period can be classified
   as historical backfill when it is recovered after a newer quarter arrives.
   Recovery intent needs to survive both collection and failed report preparation,
   while ordinary historical backfill must remain quiet.
2. An HTML source can gain previously missing image assets without changing its
   wrapper hash or document ID. Path repair handles already known identical assets;
   it does not yet version newly recovered image content or trigger a corresponding
   follow-up for an already covered document. Collection, evidence identity,
   deduplication and delivery need a coordinated regression case.

These are concrete remaining issues. Passing this pass's tests does not establish
that the project is flawless or operationally complete.
