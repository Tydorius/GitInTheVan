# Provision one throwaway GitInTheVan install on Windows.
#
# Invoked by testing/harness.py. When TARGET_WINDOWS is 'localhost' this runs
# directly with no SSH involved. Calls the repo's real deploy-windows.bat
# rather than reimplementing it -- the end-user install path is what is under
# test.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunDir,
    [Parameter(Mandatory = $true)][string]$RepoUrl,
    [Parameter(Mandatory = $true)][string]$Branch,
    [Parameter(Mandatory = $true)][int]$Port,
    [Parameter(Mandatory = $true)][int]$MockPort
)

$ErrorActionPreference = 'Stop'

$src  = Join-Path $RunDir 'GitInTheVan'
$logs = Join-Path $RunDir 'harness-logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null

function Log([string]$m) { Write-Host "[provision] $m" }
function Fail([string]$m) { Write-Error "[provision] ERROR: $m"; exit 1 }

# Native executables must not be run under ErrorActionPreference='Stop'.
# Windows PowerShell 5.1 turns *any* stderr output from a native command into a
# terminating error, and git writes its ordinary progress to stderr -- so a
# perfectly successful clone aborted the script. Exit codes are the only
# trustworthy signal here.
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$LogPath,
        [switch]$PassOutput
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $File @Arguments 2>&1
        if ($LogPath) { $output | Out-File -FilePath $LogPath -Encoding utf8 }
        if ($PassOutput) { return @{ Code = $LASTEXITCODE; Output = $output } }
        return @{ Code = $LASTEXITCODE; Output = $null }
    } finally {
        $ErrorActionPreference = $previous
    }
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'git is not installed on this host'
}

# ---------------------------------------------------------------- clone -----

Log "cloning $RepoUrl branch $Branch"
$clone = Invoke-Native -File 'git' `
    -Arguments @('clone', '--depth', '1', '-b', $Branch, $RepoUrl, $src) `
    -LogPath (Join-Path $logs 'clone.log')
if ($clone.Code -ne 0) {
    Get-Content (Join-Path $logs 'clone.log') -ErrorAction SilentlyContinue | Write-Host
    Fail 'clone failed'
}

$rev = Invoke-Native -File 'git' -Arguments @('-C', $src, 'rev-parse', 'HEAD') -PassOutput
if ($rev.Code -ne 0) { Fail 'could not read the cloned commit' }
$commit = ($rev.Output | Select-Object -First 1).ToString().Trim()
Set-Content -Path (Join-Path $RunDir '.commit') -Value $commit
Log "commit $commit"

# ----------------------------------------------------------------- port -----

New-Item -ItemType Directory -Force -Path (Join-Path $src 'data') | Out-Null
$envExample = Join-Path $src '.env.example'
$envFile    = Join-Path $src '.env'
if (Test-Path $envExample) { Copy-Item $envExample $envFile -Force }
else { Set-Content -Path $envFile -Value '' }
Add-Content -Path $envFile -Value "GITV_PORT=$Port"
Add-Content -Path $envFile -Value 'GITV_HTTP_REDIRECT_PORT=0'
Add-Content -Path $envFile -Value 'GITV_LOG_LEVEL=INFO'

# ---------------------------------------------------------- mock upstream ---

$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue) }
if (-not $py) { Fail 'no python on PATH' }

if ($MockPort -ne 0) {
    Log "starting mock upstream on port $MockPort"
    $mock = Start-Process -FilePath $py.Source `
        -ArgumentList @((Join-Path $RunDir 'mock_upstream.py'), '--port', "$MockPort") `
        -RedirectStandardOutput (Join-Path $logs 'mock-upstream.log') `
        -RedirectStandardError  (Join-Path $logs 'mock-upstream.err.log') `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RunDir '.mock.pid') -Value $mock.Id
}

# --------------------------------------------------------------- deploy -----

# deploy-windows.bat ends by running the server in the foreground, so it is
# started detached and readiness is decided by polling /health. Its exit code
# is not trustworthy alone: it exits 0 when the port is already in use.
Log 'running scripts\deploy-windows.bat (detached; readiness polled separately)'
$deploy = Start-Process -FilePath (Join-Path $src 'scripts\deploy-windows.bat') `
    -WorkingDirectory $src `
    -RedirectStandardOutput (Join-Path $logs 'deploy.log') `
    -RedirectStandardError  (Join-Path $logs 'deploy.err.log') `
    -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $RunDir '.deploy.pid') -Value $deploy.Id

# ------------------------------------------------------------- readiness ----

# Delegated to wait_health.py rather than Invoke-RestMethod: a default deploy
# enables HTTPS with a self-signed certificate, and -SkipCertificateCheck does
# not exist in Windows PowerShell 5.1.
Log "waiting for /health on port $Port"
$wait = Invoke-Native -File $py.Source `
    -Arguments @((Join-Path $RunDir 'wait_health.py'), '--port', "$Port", '--timeout', '900') `
    -PassOutput
if ($wait.Code -ne 0) {
    if (Test-Path (Join-Path $logs 'deploy.log')) {
        Write-Host '--- deploy.log (tail) ---'
        Get-Content (Join-Path $logs 'deploy.log') -Tail 60 | Write-Host
    }
    Fail 'server did not become healthy within 900s'
}

$match = ($wait.Output | Out-String) | Select-String -Pattern 'SCHEME=(\w+)'
if (-not $match) { Fail 'readiness check did not report a scheme' }
$scheme = $match.Matches[0].Groups[1].Value

$pidFile = Join-Path $src 'data\gitv.pid'
if (Test-Path $pidFile) {
    Copy-Item $pidFile (Join-Path $RunDir '.server.pid') -Force
}

Log "healthy on $scheme port $Port"
Write-Output "PROVISION_OK commit=$commit port=$Port scheme=$scheme"
