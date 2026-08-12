param(
    [int]$Port = 8766,
    [int]$RestartDelaySeconds = 3
)

$ErrorActionPreference = 'Continue'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($RestartDelaySeconds -lt 1 -or $RestartDelaySeconds -gt 300) {
    throw 'RestartDelaySeconds must be between 1 and 300.'
}

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $projectRoot 'start_app.ps1'
$healthUrl = "http://127.0.0.1:$Port/health"

while ($true) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $healthUrl -TimeoutSec 2
        $health = $response.Content | ConvertFrom-Json
        if (
            $response.StatusCode -eq 200 -and
            $health.service -eq 'free-compute-app' -and
            $health.status -eq 'ok' -and
            $health.version -eq 2
        ) {
            Start-Sleep -Seconds 5
            continue
        }
    }
    catch {
        # A missing health response is the only condition that starts the local app.
    }

    try {
        & $launcher -Port $Port -NoBrowser
    }
    catch {
        Write-Warning ("Free Compute app launch failed: {0}" -f $_.Exception.Message)
    }
    Start-Sleep -Seconds $RestartDelaySeconds
}
