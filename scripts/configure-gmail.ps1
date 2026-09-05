param([string]$ConfigPath = (Join-Path $PSScriptRoot '..\config.toml'))
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Run uv sync in the project folder first.' }
$resolvedConfig = (Resolve-Path -LiteralPath $ConfigPath).Path
$gmailAddress = & $pythonExe -c 'import sys,tomllib; print(tomllib.load(open(sys.argv[1],"rb")).get("email",{}).get("sender",""))' $resolvedConfig
if ($LASTEXITCODE -ne 0 -or $gmailAddress -notmatch '^[^\s@]+@gmail\.com$') {
    throw 'Set email.sender to your Gmail account in config.toml first.'
}
Write-Host 'Create an app password after enabling Google 2-Step Verification:'
Write-Host 'https://support.google.com/accounts/answer/185833'
Write-Host 'This stores Gmail credentials in your Windows user environment. It does not send mail or activate a schedule.'
$gmailSecure = Read-Host 'Enter the Google app password (not your normal password)' -AsSecureString
$gmailPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($gmailSecure)
try {
    $gmailPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($gmailPointer) -replace '\s', ''
    if ($gmailPassword -notmatch '^[A-Za-z0-9]{16}$') { throw 'Expected a 16-character Google app password; nothing was saved.' }
    [Environment]::SetEnvironmentVariable('SMTP_USERNAME', $gmailAddress.Trim(), 'User')
    [Environment]::SetEnvironmentVariable('SMTP_PASSWORD', $gmailPassword, 'User')
    $env:SMTP_USERNAME = $gmailAddress.Trim()
    $env:SMTP_PASSWORD = $gmailPassword
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($gmailPointer)
    Remove-Variable gmailPassword, gmailSecure -ErrorAction SilentlyContinue
}
Write-Host 'Gmail credentials configured. Restart existing shells/apps to refresh their environment.'
& $pythonExe -m servicing_brief --config $resolvedConfig doctor --offline
