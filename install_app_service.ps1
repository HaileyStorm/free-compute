param(
    [int]$Port = 8766,
    [string]$HostAddress = '127.0.0.1',
    [switch]$AllowLan,
    [string]$TaskName = 'FreeComputeLocalApp',
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
if ($TaskName -notmatch '^[A-Za-z0-9._ -]{1,80}$') { throw 'TaskName is invalid.' }
$parsedHost = $null
$isLoopback = $HostAddress -eq 'localhost' -or (
    [Net.IPAddress]::TryParse($HostAddress, [ref]$parsedHost) -and
    [Net.IPAddress]::IsLoopback($parsedHost)
)
if (-not $isLoopback -and -not $AllowLan) { throw 'A non-loopback HostAddress requires -AllowLan.' }

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$supervisor = Join-Path $projectRoot 'run_app_supervisor.ps1'
$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Port {1} -HostAddress "{2}"{3}' -f $supervisor, $Port, $HostAddress, $(if ($AllowLan) { ' -AllowLan' } else { '' })
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 20 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Keeps the Free Compute dashboard, API, and meter monitor available.' `
    -Force | Out-Null

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
}
Write-Output "Installed user-scoped scheduled task '$TaskName' on $HostAddress`:$Port."
