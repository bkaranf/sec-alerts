# Repository refactor verification, 2026-09-05

The comparison baseline is commit `2e75971`, which preserves the completed
public-voice, original-analysis and contrast work that was present before this
refactor. The refactor comparison excludes those earlier feature changes.

## Changes

- Moved the reusable reviewed-brief renderer and its unchanged template into the
  installable `servicing_brief` package.
- Retained the historical renderer and template paths, CLI defaults and
  `tools.build_company_briefs.renderer_module()` compatibility function.
- Isolated each legacy renderer import from the canonical package and other
  imports. A per-instance reentrant lock protects temporary legacy overrides.
- Consolidated brand-registry validation into one read and one validation pass
  per load. Reused source hashes within one analysis-validation call.
- Documented application code, compatibility tools and protected local history
  in `REPOSITORY.md`. No historical source or sent artifact was deleted.

## Verification

| Check | Result |
| --- | --- |
| Offline suite | 404 tests passed. |
| Report and email comparison | 35 surfaces across PFSI, TD, RY, CM, BNS, BMO and neutral TFC are byte-identical. |
| MIME comparison | Complete encoded messages compared with a fixed generation timestamp; no transport normalization. |
| Legacy and installed-package CLI | Each route reproduces all 14 baseline files, including explicit historical combined output. |
| Packaged resources | 46 wheel resources match the candidate source; four templates and six verified brand assets load outside the checkout import path. |
| Rendered QA | 60 desktop, phone, light, dark and email fallback cases passed the contrast and overflow checks. |
| Protected local files | 4,942 source, evidence, review, sent-email and brand files retain their original hashes. |
| Validation behavior | Invalid-input, cross-call freshness, distinct archive/candidate path and legacy isolation/concurrency regressions pass. |

The PFSI comparison uses the reviewed Version 2 source packet. Its HTML and
plain-text content match the accepted sent brief. The before/after guarantee for
complete MIME applies to deterministic regenerated packages; the previously
sent original is preserved separately with its original approval metadata.

Detailed local evidence is retained under `output/repo-refactor-20260905/`:
`before/`, `after/`, `comparison.json`, `protected-before.json`, CLI manifests,
wheel checks, `qa/`, and independent review records in `review/`. Generated
artifacts and email/source history remain excluded from Git.
