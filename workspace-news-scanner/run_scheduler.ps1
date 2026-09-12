
$scriptPath = "C:\Users\Aditya Singh\OneDrive\Desktop\News Agent\workspace-orchestrator\skills\pipeline"
$logDir = "$env:USERPROFILE\.openclaw\logs"
$logFile = "$logDir\pool-scheduler.log"

# Check if pool_scheduler.py is already running
$isAlreadyRunning = Get-Process python, pythonw -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like "*pool_scheduler.py*" }
if ($isAlreadyRunning) {
    Write-Host "pool_scheduler already running"
    exit 0
}

# Create log directory if it doesn't exist
if (-not (Test-Path $logDir)) {
    New-Item -Path $logDir -ItemType Directory | Out-Null
}

# Set environment variables
if ($null -eq $env:TZ) { $env:TZ = "Asia/Kolkata" }
if ($null -eq $env:SCAN_EVERY_MIN) { $env:SCAN_EVERY_MIN = "30" }
if ($null -eq $env:FEED_EVERY_MIN) { $env:FEED_EVERY_MIN = "180" }
if ($null -eq $env:FEED_QUIET_START_HOUR) { $env:FEED_QUIET_START_HOUR = "0" }
if ($null -eq $env:FEED_QUIET_END_HOUR) { $env:FEED_QUIET_END_HOUR = "6" }
if ($null -eq $env:FEED_QUIET_TIMEZONE) { $env:FEED_QUIET_TIMEZONE = "Asia/Kolkata" }
if ($null -eq $env:DISPATCH_EVERY_MIN) { $env:DISPATCH_EVERY_MIN = "1" }

# Start cmd.exe to run the python command detached with redirection
# This ensures that stdout and stderr are redirected to the same file correctly
$cmdCommand = "python `"$scriptPath\pool_scheduler.py`" >> `"$logFile`" 2>&1"
Start-Process -FilePath "cmd.exe" -ArgumentList "/c", $cmdCommand -WorkingDirectory $scriptPath -NoNewWindow

Write-Host "started pool_scheduler.py (TZ=$env:TZ SCAN_EVERY_MIN=$env:SCAN_EVERY_MIN FEED_EVERY_MIN=$env:FEED_EVERY_MIN FEED_QUIET=$env:FEED_QUIET_START_HOUR-$env:FEED_QUIET_END_HOUR DISPATCH_EVERY_MIN=$env:DISPATCH_EVERY_MIN)"
