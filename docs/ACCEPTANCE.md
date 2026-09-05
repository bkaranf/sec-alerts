# Acceptance record

Updated September 5, 2026 (America/New_York). The current local design is `astra-led-financial-brief-v6`. Gmail acceptance and recurring execution remain unactivated and untested.

## Completed verification

| Requirement | Evidence |
|---|---|
| Separate company and event drafts | Per-company event tests, separate PFSI/TFC/WFC/RKT packages, and final offline replay producing zero new reports |
| Modern, readable brief | Astra directed Luna template implementation and inspected the actual in-app browser at 390, 481 and 1280 pixels. Clear main finding and supporting line, chart directly above a compact table, concise explanations, no horizontal overflow and no displayed em dashes. `output/qa/layout-audit.json` |
| Bank and nonbank financial examples | PFSI `data/reports/25174bfc47fbc6ac6062ea4a` and TFC `data/reports/9c4443fc56c58105a4c18ccc` |
| Numerical fidelity | Verified issuer-specific layouts, Decimal arithmetic, source/period/scope checks and exact chart-to-table comparisons. Final audits checked 52 PFSI and 96 TFC evidence records |
| Additional source analysis | PFSI presentation slides 13/16, current 10-Q liquidity terms and FY2025 10-K advance-recovery context, guarded by source and slide-image hashes |
| Shorter explanations | Independent review confirmed the two exact-source-matched PFSI release paraphrases. Full original commentary remains in evidence. The annual recovery-counterparty qualifier was restored |
| Original materials and email packaging | All five prepared packages passed local artifact audits, including archive hashes, MIME alternatives, original binary/HTML-image ZIP attachments and the encoded 15 MiB limit. `data/final-artifact-audits.json` |
| Source metadata repair | Reviewed IR repairs and five WFC SEC metadata corrections applied with source bytes unchanged. The main WFC 8-K cover and SEC presentation now belong to the Q2 package. Audit logs are in `data/applied-ir-metadata-repair.json` and `data/applied-wfc-metadata-repair.json` |
| Deduplication after repair | `data/final-offline-dedup.json`: `no_new_disclosures`, zero new reports and no delivery |
| Delivery and scheduling safeguards | Offline TLS/acknowledgment/ambiguity, retries, overlap, timezone/DST and catch-up tests; explicit activation remains required |

The latest offline suite passed **103 tests in 5.34 seconds**. This includes company-scoped transcript availability notes and per-report delivery isolation, including older prepared reports surviving a new preparation failure and accepted messages remaining durable after bookkeeping errors. Luna also passed 16 focused template tests and checked the final headline split/fallback. Browser checks and all five artifact audits followed the final copy changes. No live network request or email send was part of those checks.

## Coverage and remaining limits

- TFC and PFSI have verified servicing numerical extraction. WFC and RKT have document coverage with explicit numerical-extraction limits; no generic figures were promoted into their tables.
- Four enabled companies are TFC, PFSI, WFC and RKT. JPM, BAC, USB, UWMC, RITM, ONIT and LDI were checked as expansion candidates and remain disabled. Reporting boundaries and unresolved transactions are in `data/entity-validation.json`.
- Current TFC/WFC official pages exposed no transcript or prepared-remarks link. PFSI/RKT IR returned 403, leaving call availability unknown there; their briefs now say so explicitly. JPM's archived transcript was a disabled-candidate QA check and yielded zero eligible servicing passages. LDI and BAC transcript downloads were blocked by the robots-aware fetcher. USB's reviewed official results page exposed no transcript link. No current core draft claims a reviewed call passage or includes a transcript attachment.
- Transcript selection and later-transcript update/dedup behavior have offline integration coverage. Actual eligible core call content remains unverified until an authoritative accessible transcript is available.
- PFSI's reviewed presentation/filing notes are bound to the actual Q2 2026 package. They do not claim automatic interpretation of every future image deck. Annual context stays in FY2025 and outside current-quarter financial figures.
- SEC HTML archives contain UTF-8 encoding of library-returned text, not byte-identical SEC downloads. Original image/PDF bytes are retained. No generated PDF print copy is claimed.
- Browser and local MIME previews were inspected. Rendering in Gmail/Outlook clients has not been tested with live delivery.

Three defective unsent drafts are preserved as withdrawn audit records: the earlier TFC mixed-event report, the earlier WFC historical-period mix, and the separate unknown-period WFC 8-K cover report. The last cover is now included in the correct unsent Q2 package. None of the withdrawn records is queued for sending. TFC's separate later supporting-document update is valid.

## Activation status

Sender and recipient are `bkaranf5@gmail.com`. SMTP username/app password and optional OpenAI API credentials are absent. Optional runtime AI is disabled. Delivery attempts, provider acceptance and schedule runs remain zero; `schedule.enabled=false` and no Windows task was registered.

Setup and explicit activation commands are in [README](../README.md) and [DELIVERY](DELIVERY.md). The implementation and local preview work are reviewable; operational email delivery and recurring monitoring are not claimed complete.
