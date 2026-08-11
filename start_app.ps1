param(
    [int]$Port = 8766,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
if ($Port -lt 1 -or $Port -gt 65535) {
    throw 'Port must be between 1 and 65535.'
}
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw 'Python is required to validate and run the Free Compute app.'
}

$validator = Join-Path $projectRoot 'scripts\validate_catalog.py'
$catalog = Join-Path $projectRoot 'data\catalog.json'
$orchestrator = Join-Path $projectRoot 'scripts\orchestrator.py'
$runtimeState = Join-Path $projectRoot 'orchestrator\state\usage.json'

$today = Get-Date -Format 'yyyy-MM-dd'
& $python.Source $validator $catalog '--as-of' $today
if ($LASTEXITCODE -ne 0) {
    throw "Catalog validation failed with exit code $LASTEXITCODE."
}

$url = "http://127.0.0.1:$Port/"
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
