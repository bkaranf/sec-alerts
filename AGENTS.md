# Project instructions

Read `GOAL.md`, `GOAL_PROMPT.txt`, `docs/UI_CONTENT_STANDARD.md`, and the applicable interfaces in `CONTRACTS.md` before relevant work. Preserve other agents' edits and existing evidence and sent artifacts.

Before creating or editing user-facing prose, read the repository-root PUBLIC_VOICE.md and apply its mode-specific rules. Keep engineering documentation technical. Preserve factual safeguards and product behavior.

## Project memory: GitHub is the source of truth

- All future project work belongs to the private repository [bkaranf/sec-alerts](https://github.com/bkaranf/sec-alerts). Use a clone or worktree connected to this repository, including this existing local checkout. Do not create a disconnected project copy or a separate repository for continuing work.
- Before changes, verify the repository root, `origin`, current branch and worktree status. Fetch current remote state and synchronize safely before building on it; preserve uncommitted work and other contributors' changes.
- Use `codex/` branches for new development branches. Follow existing branch protection and review requirements; never force-push or overwrite unrelated changes merely to synchronize.
- Carry completed, authorized code, tests, documentation and project-instruction changes through relevant verification, commit and push to this repository without asking again for routine publication. Verify the remote branch contains the resulting commit before reporting it saved to GitHub. If access or a required check blocks the push, preserve the local work and state precisely what remains unpublished.
- Keep credentials, local configuration, downloaded archives, email history and generated runtime artifacts excluded according to `.gitignore`. This GitHub workflow does not authorize email sends, deployment, destructive changes or bypassing required approval gates.

## Core brief controls

- Each servicing brief and each default email contains exactly one company and one earnings event. Several releases produce separate drafts.
- Apply that company's verified official logo and palette across the entire page, including the filled summary panel, page backgrounds, data surfaces, headings, links and rules. Preserve contrast and distinguish corporate colors from financial red/green meanings.
- Show useful findings and necessary qualifications. Remove content that only describes our research process. Keep exclusion reasons internal.
- Keep periods, populations, units, denominators and forecast assumptions clear.
- **P0: NO EM DASHES.** This is a mandatory design, implementation, review and release control. Reject literal U+2014, decoded entities and generated/escaped em dashes in headings, prose, tables, charts, captions, source labels, tooltips, accessible names, browser titles, email subjects, HTML/plain-text alternatives and generated brief PDFs. Check actual final rendered and packaged output after all transformations. Any occurrence fails review and blocks release until corrected and rechecked. Use a period, comma, colon, semicolon or parentheses instead. Preserve source addresses and original issuer documents. Never carry an older clean result forward to changed output.
- **P0: Negative financial values use parentheses.** Display negative amounts, percentages, basis points and changes as `($77m)`, `(2.5%)` or `(12 bps)`, consistently in summaries, tables, charts, accessible labels, email and generated brief PDFs. Parentheses mean negative, never missing or uncertain. Preserve signed underlying values, arithmetic, chart geometry, units and source evidence. Do not strip a negative sign and leave a positive-looking value. Reject minus-prefixed negative display values during design review and the final output check. Positive values and zero remain unparenthesized; original issuer documents and source addresses remain unchanged.
- Astra leads design and final acceptance. Luna Max executes precise, bounded instructions. Apply the active goal's independent review requirements to the actual rendered candidate.

### Task Execution & Autonomy

- For implementation or fix requests, carry the authorized work through implementation and relevant verification. Do not stop at a proposed plan when you can proceed.
- Make reasonable assumptions for routine, reversible decisions. Ask a focused question when missing information materially affects correctness, scope, or authorization.
- Continue with authorized read-only actions, local worktrees, branch edits, and appropriate tests without repeatedly asking.
- Before requesting approval, finish the preparation that is already authorized and present a concrete, reviewable result.
- Respect required approval gates. Ask before destructive, irreversible, or otherwise unauthorized actions.
- Avoid boilerplate warnings about hypothetical risks. Explain concrete blockers or material risks when relevant.

### Instruction Conflicts

- Explicit user instructions take precedence over conflicting skill guidelines, subject to higher-priority instructions and actual permission boundaries.
- If a skill causes a pause or deviation, identify the file and relevant rule, and explain whether it is an explicit requirement or your interpretation. Continue any unaffected authorized work.

### Style & Output

- Lead with the result. Use plain language, active voice, and concise paragraphs. Include technical details that help assess the work.
- Use lists when they improve readability; avoid repetitive transitions and stock phrases such as "it's worth noting", "delve", "leverage", and "Bottom line".
- Report what changed, what was verified, and any remaining uncertainty.

### Verification

- Match verification to the scope and impact of the change. Complete required checks; expand testing when a concrete unresolved concern justifies it.
