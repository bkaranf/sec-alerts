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

Initial combined verification: `uv run --extra sec pytest -q -rs` passed **498
tests**, with **3 skipped** because the local PFSI Q2 source archive required by
`tests/test_reviewed_context.py` is unavailable. `git diff --check` passed. These
skips are an explicit evidence gap, not verified archived-context behavior on this
checkout.

No source refresh, live email send, deployment or Task Scheduler activation was
performed for this review. Historical rendered-brief approvals do not certify
changed future output.

## Pending work and immutable image versions

The two follow-up findings from the initial review are addressed in a second
implementation pass:

- Specifically pending older-period documents retain a durable notification
  marker when recovered. Source checkpoint cleanup cannot erase it, and failed
  draft preparation can retry after restart or in offline mode. Matching requires
  the same issuer and source plus the document URL, IR discovery URL, SEC
  attachment identity, or whole-accession discovery reference. Unrelated older
  backfill remains historical; already covered content remains quiet.
- A source bundle has a separate fingerprint of its original wrapper hash and
  canonical image filename/hash manifest. `content_hash` continues to describe
  the exact wrapper bytes. New or changed verified images create immutable
  document versions, including when an older quarter already has coverage.
  Partial image recovery retains previously known images. Rediscovery of known
  older images cannot roll back a corrected image, but can repair identical local
  copies used by an older outstanding draft.
- A separate SQLite fingerprint table adds coverage identity without rewriting
  legacy document IDs, saved report payloads, report associations or delivery
  records. Identical bundles at another URL share coverage for the same CIK;
  differing images do not. Malformed legacy manifests stay isolated rather than
  borrowing another document's coverage. Invalid new files retain pending work
  and withhold the affected source checkpoint.
- Original analysis for image-bearing sources requires a complete fingerprint
  proof and intact image bytes. The authoring packet supplies that fingerprint;
  old wrapper-only essays cannot silently apply to recovered images. Reviewed
  contextual claims may bind specific intact slide hashes when their support
  remains unchanged. Historical rendered essays and approvals are preserved;
  changed future output requires fresh review.

Regression coverage exercises SQLite reopen, failed preparation, Q2/Q3 event
separation, legacy accepted/prepared/pending history, partial image merges,
corrections, stale subsets, corrupt assets, coverage aliases, archive relocation,
and original ZIP attachment bytes. All delivery effects remain mocked.

Final combined verification after the recovery changes:
`uv run --extra sec pytest -q -rs` passed **535 tests**, with **3 skipped** for the
same unavailable PFSI archive. `git diff --check` passed. An independent Astra
acceptance run passed 66 focused tests, with those same 3 skips, and accepted the
backend recovery changes with no blocking findings. The MIME regression decodes
the production email package and confirms byte-identical regeneration of the
older ZIP alongside the distinct corrected ZIP.

The local PFSI Q2 source archive remains unavailable for the three historical
context tests. Offline regression coverage and historical visual approval do not
establish live-source availability or certify newly generated financial content.
