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

## Verification

The baseline `uv run --extra sec pytest -q` completed with 420 passed and 3 skipped.
Focused regression tests cover draft withdrawals, exact artifact preservation,
archive and image recovery, provenance guards, annual-context send preparation,
and both daylight-saving transitions. External delivery is mocked.
The focused run of `tests/test_state_pipeline.py`,
`tests/test_pipeline_integration.py` and `tests/test_scheduling.py` passed all 38
tests. Astra independently inspected the implementation and regression coverage.

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
