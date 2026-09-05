# Source collection

`servicing_brief.sources.collect(config, bootstrap=False, checkpoints=None)` returns a `SourceResult` with independently collected SEC and issuer investor-relations documents. The source layer does not extract financial values or generate a report. It archives the response bytes first, then returns `Document` provenance for the reporting pipeline.

## SEC

SEC discovery uses only the installed EdgarTools package:

```python
from edgar import Company

filings = Company("0000092230").get_filings(
    form=["10-Q", "10-Q/A", "10-K", "10-K/A", "8-K", "8-K/A"],
    filing_date="2026-01-01:",
    amendments=True,
    trigger_full_load=True,
)
```

For each configured CIK, the latest completed periodic package (plus the prior
period and same-quarter prior year when available) and reporting-delay notices
are retained. The primary 10-Q/10-K is retained in full. An optional small
cap can add named servicing/financial schedules; it defaults to zero because
inline-XBRL report rows do not provide separate reporting events. Form 8-K/8-K/A records are retained only
when SEC item metadata or the EdgarTools filing text indicates earnings,
servicing, acquisition, impairment, financing, liquidity, restatement, or
related material activity. Relevant 8-K exhibits are inspected by description
and filename; the implementation does not assume that EX-99.1 is always the
earnings release. HTML exhibit image assets are archived with their parent
exhibit provenance and are not emitted as independent financial documents.

The SEC identity must be set before a live run:

```powershell
$env:EDGAR_IDENTITY = "Your Name your.email@example.com"
```

The application reports only `configured=yes/no` for this setting. It never places the identity in a `SourceResult`, log, report, or error message. EdgarTools' `EDGAR_RATE_LIMIT_PER_SEC` is set to 5 before its lazy import. Every live SEC run also takes the shared per-user lock at `LOCALAPPDATA\MortgageServicingBrief\sec-acquisition.lock` (or an explicit deployment lock path), so overlapping processes cannot exceed the aggregate budget. A process that imported EdgarTools with a higher unchangeable limit fails closed.

Archive paths are beneath `config['_storage']/archive/<TICKER>/<PERIOD>/`. Filenames include ticker, period, form/material kind, accession when available, and a hash prefix. Original content hashes, URL, filing date, acceptance time, report date, form, item numbers, sequence, and document description are retained in each `Document`. EdgarTools 5.56 returns binary exhibits (PDF/XLS/PPT) as raw bytes. Its documented `Attachment.download()` API returns HTML/text exhibits as decoded strings, so those files are archived as the UTF-8 payload returned by EdgarTools and carry `metadata.original_bytes=false` and an explicit `byte_fidelity` note; the collector does not claim unavailable wire-level encoding bytes.

`classification` is `sec-filed` for filed records and `sec-furnished` for Item 2.02/7.01 EX-99 exhibits. The primary 8-K remains a filed SEC report even when it carries furnished information. Amendments are retained as separate source documents.

## Investor relations

IR discovery is independent of SEC discovery. It starts with the configured official `ir_url` and `ir_pages`, follows a bounded number of relevant official pages, and accepts document links (PDF, spreadsheet, presentation, HTML, text, and clearly financial/earnings-labelled links). The document cap applies across the entire issuer run, so a page that lists years of releases cannot fill the archive once the current/prior package budget is used. A document hosted on an issuer CDN is accepted only when discovered from a verified official IR page; its `discovered_from` field preserves that relationship.

The IR client checks each host's `robots.txt`, uses a descriptive user agent, follows bounded retries, honors `Retry-After`, sends conditional cache headers when available, and stops on 401/403 or an explicit robots disallow. IR responses are cached under `archive/.cache/` and original materials are archived beside SEC materials. IR records always carry `source="ir"` and `classification="issuer-published"`; they are never described as SEC filings.

Period labels come only from explicit quarter/year language in titles, requested
URLs, or opening earnings content, or from EdgarTools' report-period metadata for
10-Q/10-K forms. An 8-K event date is never converted to a quarter. A period
 that has not ended as of collection time (for example Q3 on September 4) is
 deferred, and conflicting exhibit hints remain `period="unknown"` for review.
If no period is explicit, `period="unknown"`; the collector does not use a
filing or retrieval date to invent a fiscal period.

An issuer CDN redirect does not replace the requested package link when period
matching: an older link such as `2Q25 Earnings Deck` keeps its own period even
if the final CDN URL is generic/current. Filename markers such as compact
`2q26pres` are accepted only as attachment metadata; long exhibit text is
limited to its visible opening so later comparison footnotes cannot relabel the
current package. HTTP `Last-Modified` and `Date` headers are retained as
transport/cache metadata and never populate `Document.published`; that field
is populated only when an issuer-explicit publication date is supported.
Transcript discovery is a separate bounded pass over the current official IR
event/results pages. A webcast destination or event landing page is not a
transcript, and a blocked page does not establish that a transcript is absent.
The current-period availability record is [data/transcript-availability.json](../data/transcript-availability.json); it records page evidence, exact
access status, excluded webcast links, and any archived transcript records.

## Initial watchlist and boundaries

The editable watchlist is in [`config.example.toml`](../config.example.toml). The enabled proof set is Truist (`TFC`, CIK `0000092230`), PennyMac Financial Services (`PFSI`, CIK `0001745916`), Wells Fargo (`WFC`, CIK `0000072971`), and Rocket Companies (`RKT`, CIK `0001805284`). The disabled review candidates are JPMorgan (`JPM`, `0000019617`), Bank of America (`BAC`, `0000070858`), U.S. Bancorp (`USB`, `0000036104`), UWM Holdings (`UWMC`, `0001783398`), Rithm Capital (`RITM`, `0001556593`), Onity Group (`ONIT`, `0000873860`), and loanDepot (`LDI`, `0001831631`). A serialized EdgarTools 5.56 metadata query on 2026-09-04 verified the current ticker/name and latest filing metadata for all eleven CIKs; the non-secret record is [data/entity-validation.json](../data/entity-validation.json). Preferred tickers for diversified issuers can coexist with preferred shares in the metadata and are kept under the same CIK.

Rocket completed its acquisition of Mr. Cooper on October 1, 2025. Current Rocket coverage therefore includes the acquired servicing platform; legacy `COOP` should not be enabled as a separate peer unless a historical comparison explicitly requires it. Onity's current mortgage operating boundary is Onity Mortgage Corporation, while legacy PHH links may appear in source material. UWM's latest verified page may link a prior-quarter deck; period matching is required before attachment. Rithm's IR overview is dynamic and requires live link verification. These boundary notes are carried in each company configuration and surfaced by `doctor_sources`.

## Failures and checkpoints

Failures are per issuer and per source. An unavailable IR page does not suppress SEC coverage, and a missing SEC identity or blocked SEC request does not suppress IR coverage. `SourceResult.errors` retains source, ticker, CIK, URL/accession, retryability, and blocked status. `SourceResult.pending` records uncapped work and missing/unavailable source material. Checkpoint keys are `<TICKER>:sec` and `<TICKER>:ir`; each source uses the configured overlap on the next run. The root state layer retains pending accession/document work so a failed attachment is retried even after the normal lookback window moves forward. The live doctor probes one enabled SEC issuer inside the shared guard and the first official IR page for each enabled issuer; disabled candidates report `access_status="not_tested"` until enabled. A redacted source-only probe record is kept at `data/doctor-sources.json`.

For live smoke diagnostics, serialize `Document.to_dict()` and `SourceResult` to a local file such as `data/live-collection.json`. The checked-in proof artifact records a cached strict reprocess of the TFC/PFSI/WFC/RKT smoke under `data/live-core-strict/archive`; it used no additional network requests and keeps the earlier broad scratch archive only for audit. The bounded SEC repair probes are recorded in `data/rkt-sec-repro.json` and `data/wfc-sec-repair.json`; after inspection, their authoritative local `Document` records may be replayed into the existing State as a local repair. That replay is not a second network acquisition; do not launch another broad network run merely to recreate those records. The offline TFC IR metadata repair is [data/tfc-ir-metadata-repair.json](../data/tfc-ir-metadata-repair.json): it covers 15 existing archived IR records, verifies every hash, preserves each State ID/path/bytes, clears transport-derived publication timestamps, keeps nine records' withdrawn-event attachment marker, and reports one kind correction (the LMtest deck from `release` to `presentation`). Four TFC navigation/event landing pages are explicitly excluded from reporting documents; the only TFC period metadata change is clearing the period from the archived event landing page. The all-IR companion [data/ir-metadata-repair.json](../data/ir-metadata-repair.json) covers the 30 existing TFC/WFC records, excludes six navigation/event pages, and records the same one material kind correction with no PDF-cover mismatches. The withdrawn report grouped correctly perioded records from 2025-Q2, 2026-Q1, and 2026-Q2 under one 2026-Q2 event; the manifest records that event-assignment defect without rewriting the underlying archive. Do not include environment variables or credentials in a manifest.
