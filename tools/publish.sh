#!/usr/bin/env bash
# Build the dashboard and publish docs/ to GitHub Pages (the gh-pages branch): the Linux version of
# tools/publish.ps1, for the Oracle Cloud server (Oracle Linux 9, ~/dash, Python 3.12 in .venv).
#
# Scheduled by cron (crontab -l): 5 AM and 3 PM Pacific.   Run by hand: tools/publish.sh
# Publish what's already built: tools/publish.sh --no-build          Log: logs/publish.log
#
# Before building it takes the latest code from main (git reset --hard origin/main: files the build
# rewrites, like verification/*.json, are not kept here; .env, .cache/, docs/ and logs/ are ignored by
# git and stay). Pushing uses the server's deploy key for this repo (~/.ssh/gh_deploy).
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REMOTE="git@github.com:aaronmaddenjk/oregon-weather-dashboard.git"
LOG="$ROOT/logs/publish.log"
mkdir -p "$ROOT/logs"
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 2097152 ]; then mv -f "$LOG" "$LOG.old"; fi
log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" >> "$LOG"; }

exec 9>/tmp/wx-publish.lock
flock -n 9 || { log "=== skipped: a build is already running"; exit 0; }
cd "$ROOT"

if [ "${1:-}" != "--no-build" ]; then
    { git fetch -q origin main && git reset -q --hard origin/main; } >> "$LOG" 2>&1 \
        || log "(couldn't update the code from GitHub; building what's here)"
    log "=== build start ($(git log --oneline -1 | cut -c1-60))"
    # fresh data for a scheduled build, but keep the responses so a rerun soon after reuses them
    export WX_CACHE_TTL_HOURS=0.5 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
    if ! .venv/bin/python weather_dashboard.py >> "$LOG" 2>&1; then
        log "=== build FAILED; site not updated"
        exit 1
    fi
fi

# publish: docs/ as the only commit on gh-pages
TMP="$(mktemp -d)"
cp -r docs/. "$TMP"/
touch "$TMP/.nojekyll"   # serve files as-is, no Jekyll
if ( cd "$TMP" && git init -q -b gh-pages \
       && git -c user.name=aaronmaddenjk -c user.email=242111455+aaronmaddenjk@users.noreply.github.com add -A \
       && git -c user.name=aaronmaddenjk -c user.email=242111455+aaronmaddenjk@users.noreply.github.com \
              commit -q -m "Dashboard build $(date '+%Y-%m-%d %H:%M')" \
       && git push -q -f "$REMOTE" gh-pages ) >> "$LOG" 2>&1; then
    log "=== published"
else
    log "=== push FAILED"
    rm -rf "$TMP"
    exit 1
fi
rm -rf "$TMP"
