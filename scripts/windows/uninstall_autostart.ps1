# Stops Flight Radar and removes the auto-start task created by install_autostart.ps1.
#     powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_autostart.ps1

$TaskName = "FlightRadar"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed the '$TaskName' task."
} else {
    Write-Host "No '$TaskName' task found."
}
# keep_running.py starts main.py as a child process; end any that are left.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match "keep_running.py|main.py`"? run" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Stopped process $($_.ProcessId)" }
