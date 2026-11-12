# Starts Flight Radar automatically when you log in to Windows, hidden in the
# background, and keeps it running (scripts/keep_running.py restarts it if it stops).
#
# Run once from the project folder, in a normal (not admin) PowerShell:
#     powershell -ExecutionPolicy Bypass -File scripts\windows\install_autostart.ps1
# Remove it again with:
#     powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1
#
# Nothing is installed or downloaded: this only creates a task in Windows Task
# Scheduler for your own user account.

$ErrorActionPreference = "Stop"
$TaskName = "FlightRadar"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

# Prefer the project's virtual environment; pythonw.exe runs without a console window.
$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "pythonw.exe not found. Create the .venv first (see README, section 1)." }
    $Pythonw = $cmd.Source
}
$Script = Join-Path $Root "scripts\keep_running.py"

$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$Script`"" -WorkingDirectory $Root
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Flight Radar: personal flight price monitor (runs scripts\keep_running.py)" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started the '$TaskName' task. It starts at every log-in."
Write-Host "Logs: $Root\data\logs\  ·  Check: Task Scheduler > Task Scheduler Library > $TaskName"
Write-Host ""
Write-Host "Searches only happen while the PC is on and awake. To stop Windows sleeping:"
Write-Host "  Settings > System > Power > Screen and sleep > 'When plugged in, put my device to sleep after: Never'"
