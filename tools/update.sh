#!/bin/bash
# Update neTV from GitHub and restart the service when something changed.
#
# Usage: ./tools/update.sh [--force]
#
# Run every 30 minutes by the com.augustin.netv.update LaunchAgent (see install-mac.sh).
# A restart pauses running downloads, so without --force the update waits (next run) while a
# download or a playback is in progress. Local edits are never overwritten: the update is
# skipped when the folder has uncommitted changes.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${NETV_PORT:-8765}"
LABEL="${NETV_LABEL:-com.augustin.netv}"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1
log() { echo "$(date '+%F %T') $*"; }

cd "$APP_DIR"
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "local changes, update skipped"
    exit 0
fi
branch="$(git rev-parse --abbrev-ref HEAD)"
git fetch -q origin "$branch"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$branch")" ]; then
    exit 0  # already up to date
fi

if [ $FORCE -eq 0 ]; then
    if curl -s -m 5 "http://127.0.0.1:$PORT/api/downloads" | grep -q '"status": *"\(downloading\|queued\)"'; then
        log "download in progress, update postponed"
        exit 0
    fi
    pid="$(launchctl list | awk -v l="$LABEL" '$3 == l {print $1}')"
    if [ -n "$pid" ] && [ "$pid" != "-" ] && pgrep -P "$pid" ffmpeg >/dev/null; then
        log "playback in progress, update postponed"
        exit 0
    fi
fi

log "updating $(git rev-parse --short HEAD) -> $(git rev-parse --short "origin/$branch")"
git merge -q --ff-only "origin/$branch"
uv sync --quiet
launchctl kickstart -k "gui/$(id -u)/$LABEL"
log "updated and restarted"
