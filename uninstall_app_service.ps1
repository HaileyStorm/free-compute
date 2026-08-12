param([string]$TaskName = 'FreeComputeLocalApp')

$ErrorActionPreference = 'Stop'
if ($TaskName -notmatch '^[A-Za-z0-9._ -]{1,80}$') { throw 'TaskName is invalid.' }
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    Write-Output "Scheduled task '$TaskName' is not installed."
    return
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Output "Removed scheduled task '$TaskName'. Any already-running local app is left untouched."
