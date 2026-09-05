# Source insight control

`servicing_brief.editorial_control.validate_source_insights` is a small offline gate for the call note in each company brief. It checks provenance, source-file bytes, editorial text bookkeeping, and whether a claimed new insight contains more than source-availability prose. It does not fetch URLs, render email, change canonical financial metrics, or decide semantic paraphrase fidelity.

Import the one exported API:

```python
from servicing_brief.editorial_control import validate_source_insights

report = validate_source_insights(control, published_calls)
```

Use `version: 2` for new review records, with a `companies` list. Version 1 remains readable for legacy records. Each company record contains:

| Field | Requirement |
| --- | --- |
| `ticker` | Unique company key matching `published_calls`. |
| `availability` | `available` or `unavailable`. |
| `disposition` | For available material: `insight_added` or `no_material_incremental_insight`; for unavailable material: `unavailable`. |
| `source_id` | The source identifier used by the canonical review. |
| `source_url` | Public HTTPS URL. `url` is accepted as a compatibility alias. |
| `source_path` | Local archived source path for available material. An unavailable record may leave it blank when only the official discovery URL was retained. |
| `sha256` | SHA-256 of `source_path`; `hash` is accepted as an alias. It is required and byte-checked whenever a path is supplied. |
| `location` | Page, section, or official results-page location. |
| `evidence_excerpt` | The reviewed passage or the official archive-search evidence. |
| `insight_type` | For an added insight: `outlook`, `reported_result`, `management_explanation`, or `strategy`. Leave blank for an unavailable call. |
| `published_text` | Exact call text emitted by the rendered editorial layer for `insight_added`; empty for `no_material_incremental_insight` and `unavailable`. |
| `why_it_matters` | Human-written investor relevance for `insight_added`. |
| `documented_reason_for_no_increment` | Human-written explanation for `no_material_incremental_insight` or `unavailable`. |
| `document_review` | Required for available version 2 material: a nonempty review `scope` and `passages` list. Each passage requires `location`, `evidence_excerpt`, an `include` or `exclude` decision, and a specific `reader_value_reason`. |

An added insight needs at least one included passage. A no-increment decision requires every candidate passage to be excluded with a reason. These decisions share the verified whole-document hash. Review relevant passages across the document before excluding it: retention and renewal risk may appear far from margin guidance. The record makes that judgment reviewable; it does not prove that every useful passage was found. Never display this internal passage inventory or exclusion reasoning in the brief.

The caller should build `published_calls` from the actual rendered call text supplied by the packaging or UI layer, after display-only process wording has been removed. It must contain exactly the same ticker set as `control["companies"]`. For example:

```python
published_calls = {
    "TD": rendered_td_call_text,
    "RY": rendered_ry_call_text,
    "CM": rendered_cm_call_text,
    "BNS": rendered_bns_call_text,
    "BMO": rendered_bmo_call_text,
}
```

The gate requires exact equality between each record's `published_text` and this mapping. Empty text is valid for an unavailable call or a reviewed source that adds no material increment; the corresponding internal reason is required. For available material it verifies that the archived file exists and its current SHA-256 matches the recorded hash. It requires a location and excerpt for every reviewed record, and requires a non-empty investor rationale for an added insight. A procedural-only call note such as “comments were reviewed” or “no transcript was available” is rejected when marked `insight_added`. An available source with no incremental finding can pass only when its reviewer records why the source adds nothing material.

An unavailable call can pass with an official HTTPS discovery URL, an honest archive-search excerpt, and a documented reason. It must not carry an `insight_type` or `why_it_matters`, which prevents an unavailable source from gaining an invented finding. Internal no-increment reasons belong to this control record and audit report; they are not inserted into the reader-facing analysis or call note automatically.

The return value is a compact audit report with `valid`, `checked`, disposition counts, per-ticker provenance summaries, `human_judgment_required: true`, and a method note. Any failed check raises `ValueError`. The procedural screen is intentionally narrow. It is a structured safeguard around human review, not semantic proof that a paraphrase is faithful or material.

## Reviewer workflow

For each earnings call, search the issuer's prepared remarks and Q&A before finalizing the brief. Look for a reported result, a management explanation, an outlook, a strategy change, or an operating change that adds information beyond the release. Record the exact passage, speaker or section, page or location, source URL, local archive path, SHA-256, and scope. Then put the useful finding in the brief and explain why an investor should care.

If the call adds no material information, record that judgment as `no_material_incremental_insight` with a concrete reason. If an official transcript or remarks file cannot be found, record `unavailable` with the official results-page URL and the precise search limitation. Do not fill a gap with a generic statement about having reviewed a document.

Use the same source-first review habit for presentations, releases, filings, annual reports, and covered-bond documents: identify the section and scope, preserve the source bytes, and distinguish lending exposure, servicing-for-others balances, whole-segment results, and servicing economics. The initial code gate covers call material only. Human review remains responsible for deciding whether a presentation or filing passage changes the brief and belongs in reader-facing copy.

The current revised control is [`output/brief-improvement/source-insight-control.json`](../output/brief-improvement/source-insight-control.json). The earlier five-company record is retained with its original release artifacts. Tests in `tests/test_editorial_control.py` cover availability-only prose, source-backed findings, exact text and hash failures, unavailable material, and missing, unresolved or inconsistent version 2 passage decisions.
