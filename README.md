# Mortgage Servicing Earnings Brief

A local, email-first Python utility for mortgage-servicing disclosure monitoring. SEC and issuer materials are archived, facts retain source locations and business scope, and HTML/plain-text briefings include original document attachments. No dashboard, paid data service or public hosting is required.

## Repository contents

The private [bkaranf/sec-alerts repository](https://github.com/bkaranf/sec-alerts) is the source of truth for future work. Make changes in a connected checkout or worktree, then verify, commit and push them. The standing workflow is recorded in [AGENTS.md](AGENTS.md).

The repository versions the Python code, templates, controls and documentation, deterministic source fixtures, and official brand assets. Source collection archives, email history, local configuration and credentials, generated review snapshots, and runtime state remain local for the initial private publication. Tools under `tools` that inspect historical snapshots therefore require the corresponding locally collected inputs.

Six legacy path source files under `output/five-company-review` are intentionally retained for tools and tests: `render_email.py`, `email-template.html.j2`, `package_email.py`, `add_charts.py`, `audit_redesign.py`, and `validate_reviews.py`. They are explicit exceptions to the generated-output ignore rule and stay at their current paths.

## Install on Windows

Open PowerShell in this folder. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if needed, then:

```powershell
uv sync
Copy-Item config.example.toml config.toml
uv run servicing-brief doctor --offline
```

All settings and the editable CIK-based watchlist are in `config.toml`. Credentials belong in environment variables; `.env.example` lists their names but is not automatically loaded. Python 3.12 is pinned. uv uses file copies because OneDrive can reject hard links.

Set your own SEC identity before live discovery, for example through the Windows user environment editor. `EDGAR_IDENTITY` must contain your name and contact email in the format required by EdgarTools. The program reports only whether it is configured and never uses it as the email recipient.

## Run

```powershell
uv run servicing-brief doctor
uv run servicing-brief bootstrap --dry-run
uv run servicing-brief run-once --dry-run
uv run servicing-brief status
```

Each company gets its own draft when a new earnings release is detected. If three companies report on the same day, the check prepares three independent drafts. Each draft covers one company and reporting event, with useful comparisons to that company's compatible prior periods. The first check prepares only the latest baseline for each company. Later presentations, filings and corrections produce company-specific updates; unchanged runs produce no routine email. `bootstrap` is not a reset and can be run again safely.

When an issuer replaces a document at the same URL and reporting period, the brief uses the most recently retrieved version and retains the earlier facts in its evidence. Missing retrieval timestamps leave every version for review; ties at the newest timestamp retain those conflicting candidates. A run with source failures exits with code 2 even if another company produced a draft or an outstanding message was accepted; inspect the per-source and per-message results before retrying.

The brief leads with the useful company finding and follows it with evidence, interpretation and sources. Include charts and tables when they earn their space; show earlier periods on the left and the newest on the right. Reader-facing copy omits em dashes and internal research-check commentary. Astra directs the design and reviews complete phone, desktop and email layouts; Luna implements precise changes. The governing rules are in [UI and content standards](docs/UI_CONTENT_STANDARD.md).

Company branding is a core UI principle. Each single-company brief uses the issuer's verified official logo and palette across the whole page: the filled summary panel, backgrounds, data surfaces, headings, links and rules. The shared theme validates the company palette and readable foreground contrast; corporate colors remain separate from financial improvement or risk signals. The shared registry is `servicing_brief/company_branding.json`; packaged email artwork is in `servicing_brief/assets/brands`. Record issuer-source provenance and hashes when adding a brand. Unknown or invalid assets receive neutral styling and a real-text company name, with the gap recorded internally. Inspect both the branded and images-blocked email views before release.

Project execution guidelines are in [AGENTS.md](AGENTS.md) and [GOAL_PROMPT.txt](GOAL_PROMPT.txt), including autonomy, instruction conflicts, writing style and proportionate verification. Two P0 design and review controls apply to every new brief: no em dashes in reader-facing content, and parentheses for every negative financial value. The checks run on rendered reports and actual packaged HTML/plain-text email bodies and subjects, and run again before delivery. Signed source data and original issuer documents remain unchanged.

Earnings-call transcripts and issuer-published prepared remarks are part of the reporting scope. Retain supported incremental findings from management discussion and analyst Q&A; keep reasons for excluding unhelpful passages internal. A later transcript produces a company-specific update. State a bounded availability limitation near Sources when missing or blocked materials affect reader reliance.

Outputs live beneath the configured storage folder (default `data`): `archive` holds originals, `reports/<id>` holds HTML, plain text, evidence JSON and `.eml` previews, `state.sqlite3` records collection and delivery separately, and `brief.log` holds rotating operational logs. Open `briefing.html` in a browser and the `.eml` in an email client to inspect the complete package. HTML sources are attached with authoritative links. Image-based HTML presentations are packaged as ZIP files containing the archived HTML and unchanged slide images; extract the ZIP and open its HTML file. No generated PDF copy is claimed. EdgarTools returns SEC HTML as decoded text: its UTF-8 archive preserves that returned content, with the byte-fidelity limitation recorded in provenance. Binary SEC exhibits and issuer downloads retain original bytes.

Use `--config PATH` before the command for another configuration. `run-once --dry-run --offline` replays the existing local archive and clearly labels freshness as unchecked. A preview is not provider acceptance. Preview history is retained; a later explicit send submits outstanding prepared reports, so review `status` before the first send.

## Connect Gmail

Gmail was selected for this installation. Follow [the delivery guide](docs/DELIVERY.md) to enable 2-Step Verification, create a Gmail app password where your account permits it, and configure one explicit sender and recipient. Do not put the normal Google password or the app password in the TOML file or in chat.

After creating the app password, run this in PowerShell to store it locally through a hidden prompt:

```powershell
& .\scripts\configure-gmail.ps1
```

The script uses the sender already configured in TOML, saves the credentials in your Windows user environment, and checks setup without sending. Restart existing shells or apps to refresh their environment. Use `&` as shown to make the credentials available to subsequent commands in the same PowerShell session.

Once you have configured and reviewed the recipient and credentials, these are the explicit activation commands:

```powershell
uv run servicing-brief send-test --send
uv run servicing-brief run-once --send
```

The tool records Gmail/SMTP acceptance only after an acknowledgment. It cannot prove inbox arrival. An interrupted connection after possible acceptance is held as ambiguous for manual reconciliation, rather than automatically resent.

## Schedule

See [the delivery and scheduling guide](docs/DELIVERY.md) and the scripts in `scripts`. Scheduling is prepared separately from activation. The default reporting times are 07:00 and 18:00 America/New_York, editable in TOML. The Python due check uses timezone data to handle daylight saving time and catches up after missed runs. Overlapping runs are prevented by file locks.

Local scheduling requires this computer to be available and the configured Windows account/environment to have source and email access. It cannot run while the computer is powered off. Neither Task Scheduler jobs nor recurring sends are enabled merely by installing dependencies or creating a preview.

## Tests and coverage

```powershell
uv run pytest -q
```

Normal tests use deterministic local fixtures and do not contact SEC, issuers or Gmail. Live smoke results, exact artifacts and remaining limits are recorded in `docs/ACCEPTANCE.md` at handoff. Source API choices, verified issuer identities and access limitations are documented in [SOURCES.md](docs/SOURCES.md). Development model routing and the execution plan are in [EXECUTION.md](docs/EXECUTION.md).

Numerical tables currently use verified Truist and PennyMac source-layout rules. Other disclosures remain available as documents and cited excerpts, with numerical extraction limits stated explicitly.

The PennyMac Q2 2026 example also includes reviewed presentation and filing context. These notes are bound to the issuer, reporting period, document hashes and original slide images in `servicing_brief/reviewed_context.json`; they are omitted if a source changes. This is a reviewed example package, not a claim of automatic interpretation of every future presentation. Transcript selection uses recognizable speaker and section structure and keeps conversational figures out of financial tables.

The **AI Analysis** section follows **Questions** and contains original source-grounded prose in [the public voice](PUBLIC_VOICE.md). An AI agent authors the complete argument from the issuer documents, financial evidence and available call material. `analysis.build_analysis_prompt` reads the guide and prepares the authoring packet. Save the reviewed essay in `servicing_brief/reviewed_analysis/{ticker}-{event}.json`, or set `analysis.catalog_path` to its JSON path. The renderer verifies the company, event and original source hashes and preserves citations and supporting rationale. A changed event or source needs fresh authorship and review; the section is omitted when no matching essay exists. This authoring workflow does not require a separate API key.

The separate optional API feature selects additional cited source excerpts. It requires an API key and `uv sync --extra ai` and is disabled by default. Financial tables come from validated evidence, and deterministic briefings remain available when credentials are missing, the API fails or selected excerpts fail validation. Enabling this excerpt selector alone does not create original AI Analysis.

## Troubleshooting

* `configuration_error`: copy the example TOML and review the indicated setting. Only one recipient is supported.
* SEC identity missing: configure `EDGAR_IDENTITY` in the environment used by PowerShell and scheduled tasks. Restart the shell after editing user environment variables.
* Source blocked or missing document: consult `status`, the report coverage section and `brief.log`. The next run rechecks pending work with an overlap window. Do not bypass access controls.
* No new disclosures: expected after unchanged runs. Existing previews remain in `data/reports`.
* Gmail authentication fails: verify app-password availability and account policy using the delivery guide. Normal account passwords are unsupported by this integration.
* Ambiguous SMTP acceptance: inspect Gmail Sent and the recipient inbox, then follow the reconciliation procedure in the delivery guide. Do not delete state to force a resend.
* SQLite or archive trouble: keep the workspace locally available in OneDrive. Back up the complete storage folder while the tool is stopped. Never delete the database just to clear a transient failure, since it contains deduplication history.
