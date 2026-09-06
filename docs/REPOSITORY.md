# Repository map

| Location | Responsibility |
| --- | --- |
| `servicing_brief/` | Installable application: collection, evidence, analysis validation, rendering, delivery and release controls. |
| `servicing_brief/templates/` | Application HTML and text templates, including reviewed editorial rendering and the shared issuer masthead. |
| `servicing_brief/assets/brands/` | Verified PNG assets used in brief and MIME rendering. |
| `assets/brands/` | Original issuer artwork and provenance inputs. |
| `tests/` | Offline tests and deterministic source fixtures. |
| `tools/` | Development, editorial preparation and local review commands. Historical review tools require their existing local inputs. |
| `scripts/` | Operator setup, discovery and scheduling commands. |
| `docs/` | Technical contracts, controls, operation and acceptance records. |
| `output/five-company-review/` | Six tracked compatibility and historical review files, alongside ignored local artifacts. |
| `data/`, other `output/` contents | Local source archives, evidence, generated reports, review records, sent email history and runtime state. Ignored by Git. |
| `config.toml`, environment variables | Local configuration and credentials. Not versioned. |

## Reviewed rendering

`servicing_brief.review_rendering` owns the reusable editorial renderer. Its
`templates/review_email.html.j2` template is distributed inside the Python
package. `tools/build_company_briefs.py` uses the package implementation.

The old `output/five-company-review/render_email.py` script and
`email-template.html.j2` template remain compatibility entry points for existing
local scripts, imports and template loaders. Keep their historical command-line
defaults stable. The remaining utilities in that directory operate on historical
review packets and are retained at their established paths.

Normal output continues to contain exactly one company and earnings event.
Historical combined output remains available only through the existing explicit
archive option. Moving code does not grant publication approval or enable sends.

## Refactoring without changing output

Before changing rendering or validation plumbing, freeze the current worktree
and capture deterministic HTML, plain text and fully encoded MIME from the same
reviewed inputs. Fix the generation timestamp so transport metadata is stable.
Compare complete output bytes, including attachments, after the refactor; any
unexplained difference blocks an output-preserving change.

Hash source archives, branding assets and existing review and sent artifacts
before and after. Generated files and historical scripts can have active local
consumers even when Git ignores them. Audit direct imports, dynamic imports,
template loaders, command-line defaults and package resources before moving or
removing code. Retain compatibility paths where consumers cannot be ruled out.

Run the offline suite, affected command-line entry points and installed-package
resource checks. Exact output equality supplements the financial, punctuation,
contrast and publication controls. Changes to rendered content still require
fresh review under `UI_CONTENT_STANDARD.md`.

Repository cleanup does not include deleting archived evidence, review records,
sent mail, state databases or local configuration. Those files are operational
history, even when they are large or ignored by Git.
