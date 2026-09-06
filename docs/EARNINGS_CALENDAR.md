# Verified earnings calendar

`servicing_brief/calendar.py` is a small, on-demand calendar for the
servicing universe. It stores dates that have been checked against official
issuer evidence and keeps release dates, call dates, and completed calls as
separate facts. The calendar does not create a draft or trigger delivery.

## Row contract

JSON is an object with `version: 1` and a `calendar` list. CSV uses the flat
column names below. A loader also accepts a plain row list and a top-level
`companies`, `candidates`, or `rows` list so the verified universe can seed a
calendar.

Required identity and scope fields are `cik`, `company`, `ticker` (which may
be empty for a non-listed reporter), `servicing_category`, `eligibility`,
`reporting_period`, and `checked_at`. CIK is the deduplication key; duplicate
CIKs keep the first row loaded. A ticker is a display convenience, not an
identity key.

Each row has these event objects:

* `latest_completed_call`: `datetime`, `timezone`, `official_source_url`, and
  an explicit `status`. Only `status: "completed"` is eligible for selection.
  A past date in an announcement does not imply that a call occurred.
* `next_announced_release` and `next_announced_call`: the same date, timezone,
  and source fields plus `status`. Valid statuses are `announced`, `unknown`,
  `cancelled`, and `superseded`. Unknown dates stay unknown. A cancellation or
  supersession remains visible for reconciliation and is never promoted to a
  completed call.

Announced events require an official `http` or `https` URL. The `checked_at`
field records when the dates were verified. `refresh.checked_at` is separate:
the refresh command updates only access/hash provenance and leaves the date
verification timestamp unchanged.

The compact CSV form uses the following event columns:

```text
cik,company,ticker,servicing_category,eligibility,reporting_period,
latest_completed_call_datetime,latest_completed_call_timezone,
latest_completed_call_official_source_url,latest_completed_call_status,
next_announced_release_datetime,next_announced_release_timezone,
next_announced_release_official_source_url,next_announced_release_status,
next_announced_call_datetime,next_announced_call_timezone,
next_announced_call_official_source_url,next_announced_call_status,checked_at
```

Use ISO-8601 dates or datetimes. An exact call time should include an offset or
an IANA timezone such as `America/New_York`. A date-only completed call is
retained but its time is unverified.

## Selection and check queues

```powershell
uv run --extra sec python -m servicing_brief.calendar select `
  --calendar output/servicer-universe/calendar.json `
  --as-of 2026-09-05T17:00:00-04:00 `
  --output output/servicer-universe/selection.json
```

`select_latest5` accepts only `eligibility` values `confirmed` or `included`
(plus the explicit compatibility values `eligible`,
`confirmed_included`, and `included_requested_universe`) and an explicit
completed latest call. Known times sort by UTC, including across local-date
boundaries. If any time in a same-day group is unknown, the whole group sorts
alphabetically by ticker, or by CIK when ticker is empty. A timezone-aware
intraday cutoff excludes a date-only same-day call because occurrence before
the cutoff cannot be confirmed. The result includes `definitive`, excluded
categories, and the exact tie or coverage notes. Unresolved eligibility,
future potential calls, and unverified call timing make the result non-
definitive.

```powershell
uv run --extra sec python -m servicing_brief.calendar due `
  --calendar output/servicer-universe/calendar.json `
  --as-of 2026-09-05T17:00:00-04:00
```

The due queue prioritizes announced releases or calls through the same and
next calendar day, then stale or unresolved checks and cancelled/superseded
announcements. Unknown next dates remain in the queue for another official
IR check. Calendar dates alone never create an earnings draft.

## On-demand official-page refresh

Refresh takes either an existing calendar or the verified candidate rows
provided by the universe worker. It checks only an explicitly supplied issuer
IR URL through the existing robots-aware IR fetcher, records HTTP status, the
final official URL, retrieval time, and a SHA-256 content hash, and does not
parse or invent dates. SEC URLs are rejected by this refresh path; SEC
acquisition remains the existing EdgarTools responsibility.

```powershell
uv run --extra sec python -m servicing_brief.calendar refresh `
  --companies output/servicer-universe/companies.json `
  --output output/servicer-universe/calendar.json `
  --url PFSI=https://pfsi.pennymac.com/news-events/quarterly-earnings/default.aspx
```

After a page check, a reviewer can update the event object from the official
page and set its own `checked_at`. A successful page hash is evidence that the
page changed or stayed the same; it is not evidence that a release or call
occurred.

## Fast check-to-draft workflow

Keep the full SEC discovery audit and reviewed company decisions in
`output/servicer-universe/`. Refresh that broad screen periodically or after
an acquisition. Daily checks can use a small overlapping date range instead
of downloading every annual report again:

```powershell
uv run --extra sec python scripts/check_recent_earnings.py --since 2026-08-25 --through 2026-09-05
```

This command searches 8-K and 6-K disclosures through EdgarTools, joins the
results to the candidate CIK list, and saves a document-review queue in
`output/servicer-universe/recent-earnings/queue.json`. Use the last successful
check date minus several days as the next `--since` date. Review the actual
release and current issuer event archive: an acquisition webcast, dividend
notice or cancelled call must not replace the latest earnings call. The
September 5 audit found examples of all three.

Use the calendar `due` queue to prioritize official IR checks around published
release dates. Keep checking the IR source independently because a release
can appear there before SEC indexing. Save new releases, presentations,
reports and actual call material as evidence. For an already configured
issuer, `uv run --extra sec servicing-brief run-once --dry-run` uses the existing document
pipeline to prepare separate company/event drafts and retain later-document
updates. Review a newly discovered issuer's reporting boundary and source
coverage before adding it to that pipeline. The expanded discovery list does
not silently enable hundreds of collectors.

The five-company email is a separately reviewed one-off compilation. Neither
the calendar nor the SEC queue sends email, activates Windows scheduling, or
sends the existing draft backlog. Current calendar refresh is a page-change
check with source provenance; new dates still require evidence review.
