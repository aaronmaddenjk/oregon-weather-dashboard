# Build the dashboard on this PC and publish docs/ to GitHub Pages (the gh-pages branch).
#
# Why here and not in GitHub Actions: Open-Meteo's free tier limits by IP address, and
# GitHub's shared runner IPs are throttled to the point of timeouts. This PC's IP is fine.
#
# Run by hand:   powershell -ExecutionPolicy Bypass -File tools\publish.ps1
# Scheduled by:  tools\schedule.ps1 (Windows Task Scheduler)
# Log:           logs\publish.log
#
# Each publish force-pushes a single fresh commit to gh-pages, so the repo doesn't grow by
# ~10 MB of build output every run.

param([switch]$NoBuild)   # -NoBuild: publish the docs/ already built
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = "C:\Users\aaron\AppData\Local\Python\pythoncore-3.14-64\python.exe"
$Remote = "https://github.com/aaronmaddenjk/oregon-weather-dashboard.git"
$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir "publish.log"
if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 2MB) { Move-Item $Log "$Log.old" -Force }
function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File $Log -Append -Encoding utf8 }

Set-Location $Root
if (-not $NoBuild) {
    Log "=== build start"
    # Fresh data for a scheduled build, but still store responses so dev rebuilds reuse them.
    $env:WX_CACHE_TTL_HOURS = "0.5"
    $env:PYTHONUNBUFFERED = "1"
    $env:PYTHONIOENCODING = "utf-8"
    # (through cmd: PowerShell 5.1 turns a native program's stderr into script errors)
    cmd /c "`"$Python`" weather_dashboard.py >> `"$Log`" 2>&1"
    if ($LASTEXITCODE -ne 0) { Log "=== build FAILED (exit $LASTEXITCODE); site not updated"; exit 1 }
}

# Publish: copy docs/ to a temp folder and force-push it as the only commit on gh-pages.
$Tmp = Join-Path $env:TEMP "wx-pages"
if (Test-Path $Tmp) { Remove-Item $Tmp -Recurse -Force }
Copy-Item (Join-Path $Root "docs") $Tmp -Recurse
New-Item -ItemType File (Join-Path $Tmp ".nojekyll") | Out-Null   # serve files as-is, no Jekyll
Push-Location $Tmp
try {
    git init -q -b gh-pages
    git config user.name "aaronmaddenjk"
    git config user.email "242111455+aaronmaddenjk@users.noreply.github.com"
    git add -A
    git commit -q -m "Dashboard build $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    cmd /c "git push -q -f $Remote gh-pages >> `"$Log`" 2>&1"
    if ($LASTEXITCODE -ne 0) { Log "=== push FAILED (exit $LASTEXITCODE)"; exit 1 }
} finally {
    Pop-Location
    Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
}
Log "=== published"
