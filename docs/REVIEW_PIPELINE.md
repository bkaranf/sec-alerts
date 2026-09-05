# Independent root integration review

Reviewed after the delivery and scheduling implementation, with no changes
made to \`config.py\`, \`state.py\`, \`pipeline.py\`, or \`cli.py\`.

## Findings

1. **High — \`reconcile\` is documented but not wired into the CLI.**

   \`servicing_brief.delivery.reconcile_message()\` now provides the
   controlled \`accepted\`/\`retry\` transition required for an ambiguous SMTP
   result, and \`docs/DELIVERY.md\` documents the command
   \`servicing-brief --config config.toml reconcile --message-key KEY --decision accepted|retry\`.

   \`servicing_brief/cli.py\` currently registers only \`doctor\`,
   \`bootstrap\`, \`run-once\`, \`scheduled\`, \`send-test\`, and \`status\`.
   Add the \`reconcile\` parser and call the delivery function with
   \`storage / "state.sqlite3"\`. Keep the command explicit and do not make
   routine \`run-once --send\` clear \`ambiguous\` rows.

2. **Medium — offline \`doctor\` does not run the email checks.**

   \`cli.doctor(..., offline=True)\` skips \`delivery.doctor_email(config)\`,
   while the online branch invokes it. A credential-free user following the
   documented \`doctor --offline\` setup command therefore sees only the
   hard-coded \`SMTP_USERNAME\`/\`SMTP_PASSWORD\` probes and not the configured
   password environment name or Gmail TLS check. Run \`doctor_email\` in both
   modes; retain the no-secret booleans. If custom
   \`email.smtp_password_env\` remains supported, avoid reporting only the
   hard-coded \`SMTP_PASSWORD\` variable.

3. **Medium — a missing archive can roll back all documents from one source
   result.**

   \`State.ingest()\` wraps the whole result in one SQLite transaction and
   raises when any returned \`Document.path\` is absent or has a hash mismatch.
   One corrupt document can therefore prevent otherwise valid documents from
   other issuers in that same collection result from being ingested. The
   source worker records errors/pending work, but the state boundary should
   validate and persist valid documents independently, or the collector should
   return only validated documents and report the bad one as a source error.

4. **Low — scheduled invocation intentionally requires two independent
   settings.**

   \`cli.py\` passes \`lambda: run(config, send=args.send, ...)\` to
   \`scheduling.scheduled_run()\`. The scheduler gate returns \`disabled\` when
   \`schedule.enabled\` is false, even if \`scheduled --send\` was typed. This
   is safe and matches the install script, which now requires
   \`schedule.enabled = true\` and \`-EnableSending\`; document the resulting
   \`disabled\` status in CLI help/status output so an operator can distinguish
   “not due” from “schedule not activated”.

5. **Low — the delivery status transition is correct but coarse.**

   \`pipeline._run_locked()\` maps every non-all-accepted delivery response to
   \`pending\`, except when any response is \`ambiguous\`. This preserves
   retryability for missing credentials and failed SMTP responses, while
   \`delivery_messages\` retains the precise status. Keep this mapping, but
   surface the per-message \`delivery\` rows in \`status\` and in the final
   response (already available from \`delivery_status\`) so
   \`oversize_refused\`, \`retry_exhausted\`, and
   \`message_changed_after_acceptance\` are not hidden behind a generic
   briefing-level \`pending\`.

## Verified wiring

- \`pipeline.run()\` sets the private \`_send\` flag only from the explicit
  \`send=True\` argument and calls the contracted
  \`prepare_messages(config, report, documents, output_dir)\` and
  \`deliver_messages(config, paths, state.path)\` functions.
- Previews are prepared before \`State.save_report()\`, so a preparation
  failure leaves ingested source documents available while the briefing
  remains retryable.
- \`State\` uses \`state.sqlite3\`; delivery adds only
  \`delivery_messages\` and \`delivery_attempts\`, and scheduling adds only
  \`schedule_runs\` plus lock files. Collection tables and SMTP acknowledgment
  tables remain separate.
- \`scripts/run-scheduled.ps1\` invokes
  \`python -m servicing_brief ... scheduled --send\`; the installer resolves
  the repository \`.venv\`, validates activation prerequisites, and uses valid
  \`Interactive\`/\`Limited\` Task Scheduler principal values.

## Review limits

This pass inspected local source and deterministic tests only. It did not
perform a live SEC collection, Gmail authentication, Task Scheduler
registration, or provider acceptance test.
