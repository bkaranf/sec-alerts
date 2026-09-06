[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ConfigPath,

    [string] $PythonExe = "",

    [string] $WorkingDirectory = "",

    [switch] $EnableSending
)

$ErrorActionPreference = "Stop"

if (-not $EnableSending) {
    throw "Recurring sends are disabled. The scheduled task must be installed and invoked with -EnableSending after recipient and Gmail credentials are configured."
}

if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
    throw "Configuration file was not found: $ConfigPath"
}

if ($WorkingDirectory) {
    Set-Location -LiteralPath $WorkingDirectory
} else {
    $WorkingDirectory = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
    Set-Location -LiteralPath $WorkingDirectory
}

if (-not $PythonExe) {
    $PythonExe = Join-Path $WorkingDirectory ".venv\Scripts\python.exe"
}
if (Test-Path -LiteralPath $PythonExe -PathType Leaf) {
    $PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
} else {
    $command = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if (-not $command) {
        throw "Python executable was not found: $PythonExe. Run uv sync --extra sec or pass -PythonExe explicitly."
    }
    $PythonExe = $command.Source
}

# The Python due gate interprets the configured America/New_York slots and
# persists successful occurrences. The frequent Task Scheduler trigger is
# intentionally timezone agnostic, so Windows host timezone/DST cannot shift a
# 07:00 or 18:00 New York run.
& $PythonExe -m servicing_brief --config $ConfigPath scheduled --send
exit $LASTEXITCODE
