# Email delivery and Windows scheduling

Delivery produces a local `.eml` preview first. `servicing_brief.delivery.prepare_messages(config, report, documents, output_dir)` never sends; it archives the report as a plain text/HTML multipart message and attaches the original bytes at each `Document.path`. It uses the source filename/issuer, period, and document kind in attachment names. HTML sources remain archived HTML attachments. When an HTML document references archived images, a ZIP contains the unchanged HTML and each original image under its source filename; extract it and open the HTML file to view the deck offline. The ZIP is a packaging copy, not an issuer-published PDF. SEC HTML has the EdgarTools decoded-text fidelity limitation documented in SOURCES.md. A generated PDF print copy is attached only when its document metadata marks the rendering as verified.

The default encoded-message budget is 15 MiB, including MIME headers and base64 transfer encoding. Attachments are prioritized as presentation, release, supplement, current 10-Q/10-K, then other source documents. Packages are split into numbered messages after serializing the complete MIME message. A document that cannot fit by itself is omitted from the attachment and listed with its authoritative source URL and local archive path. `deliver_messages` refuses any `.eml` that still exceeds the configured budget before opening SMTP.

## Gmail setup

The real provider path is Gmail authenticated SMTP. The default endpoint is `smtp.gmail.com` on port 465 with implicit TLS. Port 587 with `smtp_security = "starttls"` is also supported. The implementation reads credentials from environment variables and does not store them in TOML or print them. Set the username and app password as user environment variables. A one-time PowerShell prompt avoids putting the app password in shell history; the commands persist both credentials for future Task Scheduler processes as well as the current session:

```powershell
$env:SMTP_USERNAME = Read-Host "Gmail account address"
[Environment]::SetEnvironmentVariable("SMTP_USERNAME", $env:SMTP_USERNAME, "User")
$secure = Read-Host "Gmail app password (16 characters)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
  $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
  [Environment]::SetEnvironmentVariable("SMTP_PASSWORD", $plain, "User")
  $env:SMTP_PASSWORD = $plain
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
  Remove-Variable plain -ErrorAction SilentlyContinue
}
```

The `$env:` assignments affect the current PowerShell process. The
`SetEnvironmentVariable(..., "User")` call makes the password available to a
later Task Scheduler process under the same Windows user; restart a shell or
sign in again if another process does not see a newly stored value. Windows
user environment variables are local secret storage with the usual account
permissions; revoke the app password in Google if the account or computer is
retired. Gmail's server and port reference is [Google's SMTP/POP/IMAP settings guide](https://support.google.com/mail/answer/7126229).

Set the one explicit recipient and sender in the TOML configuration. The sender should be the same Gmail account (or an account permitted by that account). Do not put a normal Gmail password in the file or send it in chat. Google requires 2-Step Verification before an app password can be created, and some work/school accounts, Advanced Protection accounts, or security-key-only configurations do not offer app passwords. See [Google's app-password guidance](https://support.google.com/accounts/answer/185833).

The setup can be checked without sending:

```powershell
servicing-brief --config config.toml doctor --offline
servicing-brief --config config.toml run-once --dry-run --offline
```

A live test requires the explicit send command and an explicitly configured recipient:

```powershell
servicing-brief --config config.toml send-test --send
```

The runtime send gate is separate from `email.enabled`; a normal dry run never marks provider acceptance. `delivery_messages` and `delivery_attempts` are independent tables in the shared SQLite state database. A message is `accepted` only after `send_message` returns without a provider refusal. `ambiguous` means the connection failed after transmission may have started; the message is not retried automatically. Resolve that case with the provider before any controlled retry. Repeated runs return `already_accepted` for an acknowledged message key.

If an SMTP connection fails after transmission may have started, inspect the Gmail Sent folder or provider logs before deciding. After confirming the message was accepted, record that fact without another send:

```powershell
servicing-brief --config config.toml reconcile --message-key MESSAGE_KEY --decision accepted
```

If the provider confirms that it was not accepted, enable one controlled retry for the stored preview:

```powershell
servicing-brief --config config.toml reconcile --message-key MESSAGE_KEY --decision retry
servicing-brief --config config.toml run-once --send
```

The `reconcile` command is intentionally separate from routine runs so an ambiguous SMTP result is never retried blindly.

## Scheduling

`scheduled` returns `disabled` until `schedule.enabled = true`; it returns `not_due` when activated but no reporting slot is due. The Python gate in `servicing_brief.scheduling` interprets the configured local times in `America/New_York` (default `07:00` and `18:00`) and persists only successful occurrences in `schedule_runs`. The public helpers are:

```python
from servicing_brief.scheduling import due, mark_handled, scheduled_run

slots = due(config, state_db)
mark_handled(state_db, slots[0], success=True)
```

`scheduled_run(config, state_db, callback)` provides the overlap lock and marks a slot only when the callback returns `prepared`, `no_new_disclosures`, `provider_accepted`, or `already_accepted`, with no source failures. Incomplete collection, delivery failures, overlap skips, and exceptions leave the slot due for a later catch-up attempt. The default `max_pending = 1` runs only the newest missed slot to avoid a historical email flood; configure a higher value when intentional catch-up of multiple slots is wanted. A nonexistent local time during a DST spring-forward gap is skipped, and a repeated local time uses the first fold. The default 07:00/18:00 slots are unaffected by either transition.

Windows uses a frequent (default 15-minute) Task Scheduler trigger. The Python gate, rather than Windows' machine timezone, decides whether a New York slot is due. The installer uses the repository's `.venv\Scripts\python.exe` by default (or an explicit `-PythonExe`) and requires `schedule.enabled = true`, one recipient/sender, and configured Gmail environment credentials. It also requires an explicit `-EnableSending` switch and refuses to create a recurring-send task without it:

```powershell
pwsh -File scripts/install-schedule.ps1 `
  -ConfigPath .\config.toml `
  -WorkingDirectory (Get-Location) `
  -EnableSending
```

Use `-WhatIf` to validate the installer arguments and activation checks without
registering a task. The task runs for the logged-in Windows user; the
environment variables used by that user must be available when it runs.

Remove the task with:

```powershell
pwsh -File scripts/remove-schedule.ps1
```

The computer must be running and the configured user must be able to run the task. Task Scheduler does not make this local utility run while the computer is powered off. The schedule is prepared by these scripts; it is not enabled by the project setup or tests.

## Failure and preview behavior

Missing recipient, Gmail username, or app password produces a safe status and leaves the `.eml` preview on disk. SMTP connection failures before message transmission have bounded retries. Authentication, recipient, and data rejections are recorded as failures. A disconnect or timeout after send begins is recorded as ambiguous and stops the package to avoid blind duplicate sends. Provider acceptance is never described as inbox delivery.
