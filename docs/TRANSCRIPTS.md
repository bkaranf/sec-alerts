# Earnings-call coverage

The source record is `data/transcript-coverage.json`; the bounded BAC/USB follow-up is in `data/transcript-proof-addendum.json`. Current earnings-event pages were checked separately from release extraction.

| Company | Current proof |
|---|---|
| TFC | Official event listing contains release, deck and webcast; no transcript link found in reviewed current-event material. |
| WFC | No current transcript link found on reviewed official earnings/events pages. |
| PFSI / RKT | Existing live IR checks returned 403. No bypass or repeated blocked requests. |
| LDI (disabled candidate) | Actual Q2 transcript link found on the rendered official event page; existing fetcher stopped because the CDN robots policy disallowed automated access. |
| JPM (disabled candidate) | Actual Q2 earnings transcript PDF archived through the existing IR fetcher. No eligible servicing/MSR passage was present, so none was promoted into the brief. |
| BAC (disabled candidate) | Official Q2 results page links a transcript PDF. The existing fetcher stopped after the document host's robots request returned 403. No transcript bytes were downloaded. |
| USB (disabled candidate) | Reviewed official Q2 results material lists webcast, release, presentation and supplement, with no transcript or prepared-remarks link found. |

The actual JPM source and hash are recorded in `data/jpm-transcript-proof.json`. It proves collection and conservative filtering of a real transcript, not core-company call coverage. The LDI link is a known missing document, not a reviewed call.

Briefs distinguish blocked investor-relations access, where transcript availability remains unverified, from a transcript not found in checked sources. Neither means the issuer has not published a transcript. Availability notes are scoped to each company, including during a run with several companies.

Offline tests separately verify prepared remarks, analyst questions, management answers, outlook labels, source locations, period matching, a later-transcript update and unchanged-document suppression. Conversational figures cannot enter financial tables. These fixtures are synthetic and do not establish that an inaccessible real call was reviewed.

Current limits: selection requires recognizable speaker/section structure. Some issuer event pages populate links with JavaScript that the HTTP collector cannot discover. An editable official document URL or further source integration may be needed for those pages. Missing call materials remain explicit; the first earnings release draft does not wait for them.
