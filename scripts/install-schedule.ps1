[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string] $ConfigPath,

    [string] $TaskName = "MortgageServicingBrief",

    [string] $PythonExe = "",

    [string] $WorkingDirectory = "",

    [ValidateRange(5, 60)]
    [int] $IntervalMinutes = 15,

    [switch] $EnableSending
)

$ErrorActionPreference = "Stop"

if (-not $EnableSending) {
    throw "Recurring sends were not enabled. Re-run this installer with -EnableSending only after email.recipient and Gmail app-password setup are complete."
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Configuration file was not found: $ConfigPath"
}

if (-not $WorkingDirectory) {
    $WorkingDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot ".." )).Path
}

if (-not $PythonExe) {
    $PythonExe = Join-Path $WorkingDirectory ".venv\Scripts\python.exe"
}
if (Test-Path -LiteralPath $PythonExe -PathType Leaf) {
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
} else {
    $command = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Python executable was not found: $PythonExe. Run uv sync or pass -PythonExe explicitly."
    }
    $PythonExe = $command.Source
}

# Validate the complete activation configuration before constructing or
# registering a task. The helper emits booleans only; no credential values are
# returned to PowerShell output.
$validationCode = @'
import json
import os
import sys
from servicing_brief.config import load_config

config = load_config(sys.argv[1])
email = config.get("email", {})
schedule = config.get("schedule", {})
password_env = str(email.get("smtp_password_env", "SMTP_PASSWORD"))
print(json.dumps({
    "schedule_enabled": schedule.get("enabled") is True,
    "recipient": bool(email.get("recipient")),
    "sender": bool(email.get("sender") or email.get("from_address")),
    "username": bool(os.environ.get("SMTP_USERNAME") or email.get("smtp_username")),
    "password": bool(os.environ.get(password_env)),
    "password_env": password_env,
}))
'@
$validationJson = & $PythonExe -c $validationCode $ConfigPath
if ($LASTEXITCODE -ne 0) {
    throw "Configuration validation failed; run the doctor command before installing the task."
}
$validation = $validationJson | ConvertFrom-Json
if (-not $validation.schedule_enabled) {
    throw "schedule.enabled must be true before installing a recurring task."
}
if (-not $validation.recipient) {
    throw "email.recipient is unset; configure one explicit recipient before installing."
}
if (-not $validation.sender) {
    throw "email.sender is unset; configure the Gmail sender before installing."
}
if (-not $validation.username) {
    throw "SMTP_USERNAME (or email.smtp_username) is unset; configure the Gmail account before installing."
}
if (-not $validation.password) {
    throw "$($validation.password_env) is unset; configure the Gmail app password before installing."
}

$wrapper = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "run-scheduled.ps1")).Path
$escapedWrapper = $wrapper.Replace('"', '\"')
$escapedConfig = ((Resolve-Path -LiteralPath $ConfigPath).Path).Replace('"', '\"')
$escapedPython = $PythonExe.Replace('"', '\"')
$escapedWorking = $WorkingDirectory.Replace('"', '\"')
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$escapedWrapper`" -ConfigPath `"$escapedConfig`" -PythonExe `"$escapedPython`" -WorkingDirectory `"$escapedWorking`" -EnableSending"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $WorkingDirectory

# A 15-minute trigger keeps Task Scheduler's machine-local behavior simple.
# run-scheduled.ps1 and scheduling.due perform the authoritative New York
# timezone/DST due check and suppress duplicate occurrences.
$firstTrigger = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $firstTrigger -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Mortgage servicing disclosure brief; Python gate enforces America/New_York schedule."

if ($PSCmdlet.ShouldProcess($TaskName, "Register scheduled task with recurring sends enabled")) {
    Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
    Write-Output "Installed scheduled task '$TaskName' with a $IntervalMinutes-minute trigger and Python New York due gate."
}
