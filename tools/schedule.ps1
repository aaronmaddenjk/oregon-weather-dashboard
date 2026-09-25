# Register (or update) the Windows scheduled task that builds and publishes the dashboard.
# Run:     powershell -ExecutionPolicy Bypass -File tools\schedule.ps1
# Remove:  Unregister-ScheduledTask -TaskName "Oregon Weather Dashboard" -Confirm:$false
#
# Times: a full build uses ~4,600 of Open-Meteo's ~10,000 free calls a day, so twice a day
# leaves room for local dev builds. Runs only while you're logged in (git's GitHub login lives
# in your Windows session); a missed run (PC asleep/off) starts as soon as it's back.

$Name = "Oregon Weather Dashboard"
$Times = "5:00AM", "3:00PM"
$Script = Join-Path $PSScriptRoot "publish.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Script`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$triggers = $Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers -Settings $settings `
    -Principal $principal -Description "Builds the weather dashboard and publishes it to GitHub Pages (tools\publish.ps1)" -Force |
    Out-Null
Get-ScheduledTask -TaskName $Name | Get-ScheduledTaskInfo | Select-Object TaskName, NextRunTime
