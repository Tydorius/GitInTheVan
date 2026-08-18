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

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail 'git is not installed on this host'
}

# ---------------------------------------------------------------- clone -----

Log "cloning $RepoUrl branch $Branch"
git clone --depth 1 -b $Branch $RepoUrl $src *> (Join-Path $logs 'clone.log')
if ($LASTEXITCODE -ne 0) {
    Get-Content (Join-Path $logs 'clone.log') | Write-Host
    Fail 'clone failed'
}

$commit = (git -C $src rev-parse HEAD).Trim()
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

if ($MockPort -ne 0) {
    $py = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue) }
    if (-not $py) { Fail 'no python on PATH to run the mock upstream' }
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

Log "waiting for http://127.0.0.1:$Port/health"
$deadline = (Get-Date).AddSeconds(900)
$ok = 0
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($r.status -eq 'ok') {
            # Twice: the updater's maintenance page binds the same port and
            # serves HTML for every path, so one response proves nothing.
            $ok++
            if ($ok -ge 2) { break }
        } else { $ok = 0 }
    } catch { $ok = 0 }
    Start-Sleep -Seconds 3
}

if ($ok -lt 2) {
    if (Test-Path (Join-Path $logs 'deploy.log')) {
        Write-Host '--- deploy.log (tail) ---'
        Get-Content (Join-Path $logs 'deploy.log') -Tail 60 | Write-Host
    }
    Fail 'server did not become healthy within 900s'
}

$pidFile = Join-Path $src 'data\gitv.pid'
if (Test-Path $pidFile) {
    Copy-Item $pidFile (Join-Path $RunDir '.server.pid') -Force
}

Log "healthy on port $Port"
Write-Output "PROVISION_OK commit=$commit port=$Port"
