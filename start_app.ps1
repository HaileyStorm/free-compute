param(
    [int]$Port = 8766,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
if ($Port -lt 1 -or $Port -gt 65535) {
    throw 'Port must be between 1 and 65535.'
}
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$url = "http://127.0.0.1:$Port/"
$validator = Join-Path $projectRoot 'scripts\validate_catalog.py'
$catalog = Join-Path $projectRoot 'data\catalog.json'
$orchestrator = Join-Path $projectRoot 'scripts\orchestrator.py'
$runtimeState = Join-Path $projectRoot 'orchestrator\state\usage.json'

function Get-VerifiedListenerProcess {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listeners.Count -ne 1) { return $null }
    $process = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $listeners[0].OwningProcess) -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $commandLine = [string]$process.CommandLine
    if ($commandLine.IndexOf($orchestrator, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
        return $null
    }
    return $process
}

function Stop-VerifiedStaleApp {
    $process = Get-VerifiedListenerProcess
    if ($null -eq $process) {
        throw "Port $Port is occupied by an unverified process. Refusing to stop it or start another app."
    }
    Stop-Process -Id $process.ProcessId -ErrorAction Stop
    foreach ($attempt in 1..50) {
        if (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
            return
        }
        Start-Sleep -Milliseconds 100
    }
    throw "The verified stale Free Compute process did not release port $Port within 5 seconds."
}

$existing = $null
try { $existing = Invoke-WebRequest -UseBasicParsing -Uri ($url + 'health') -TimeoutSec 2 } catch {}
if ($null -ne $existing) {
    try { $health = $existing.Content | ConvertFrom-Json } catch { $health = $null }
    if (
        $existing.StatusCode -eq 200 -and
        $health.service -eq 'free-compute-app' -and
        $health.status -eq 'ok' -and
        $health.version -eq 2
    ) {
        if (-not $NoBrowser) { Start-Process $url }
        Write-Output "Free Compute app is already available at $url"
        return
    }
    if ($existing.StatusCode -eq 200 -and $health.service -eq 'free-compute-app') {
        Stop-VerifiedStaleApp
    }
    else {
        throw "Port $Port returned an unexpected health response. Refusing to replace it."
    }
}
elseif (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    if (Get-VerifiedListenerProcess) {
        Stop-VerifiedStaleApp
    }
    else {
        throw "Port $Port is occupied by a foreign or unverifiable service. Refusing to replace it."
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python is required to validate and run the Free Compute app.'
}

$today = Get-Date -Format 'yyyy-MM-dd'
& $python.Source $validator $catalog '--as-of' $today
if ($LASTEXITCODE -ne 0) {
    throw "Catalog validation failed with exit code $LASTEXITCODE."
}

$arguments = @(
    ('"{0}"' -f $orchestrator),
    '--catalog', ('"{0}"' -f $catalog),
    '--runtime-state', ('"{0}"' -f $runtimeState),
    'serve', '--host', '127.0.0.1', '--port', $Port
)
$server = Start-Process -FilePath $python.Source -ArgumentList $arguments -WindowStyle Hidden -PassThru
try {
    $ready = $false
    foreach ($attempt in 1..50) {
        if ($server.HasExited) {
            throw "Free Compute app exited with code $($server.ExitCode)."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri ($url + 'health') -TimeoutSec 1
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            Start-Sleep -Milliseconds 100
        }
    }
    if (-not $ready) {
        throw 'Free Compute app did not become ready.'
    }
    if (-not $NoBrowser) {
        Start-Process $url
    }
    Wait-Process -Id $server.Id
}
finally {
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id
    }
}
