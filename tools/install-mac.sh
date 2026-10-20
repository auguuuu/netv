#!/bin/bash
# Install neTV on a Mac as an always-on service (starts at login, restarts if it crashes)
# that updates itself from GitHub every 30 minutes (tools/update.sh).
#
# Usage (from the project folder):
#   ./tools/install-mac.sh [--settings netv-settings.tar.gz] [--port 8765]
#
#   --settings FILE  archive made by tools/export-settings.sh on the old Mac: IPTV accounts,
#                    favorites, watch history. Without it, the first visit asks to create a user.
#   --port PORT      default 8765 (http://localhost:8765)
#
# The server listens on 127.0.0.1 only and opens without a password (NETV_AUTO_LOGIN), so it
# is reachable from this Mac only. Run it again at any time: it is idempotent.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8765
SETTINGS=""
LABEL="com.augustin.netv"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/netv"

while [[ $# -gt 0 ]]; do
    case $1 in
        --port) PORT="$2"; shift 2 ;;
        --settings) SETTINGS="$2"; shift 2 ;;
        *) echo "Usage: $0 [--settings FILE] [--port PORT]"; exit 1 ;;
    esac
done

# 1. Homebrew, ffmpeg, uv
if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
fi
if ! command -v brew >/dev/null 2>&1; then
    echo "==> Installing Homebrew (asks for your Mac password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
        [ -x "$b" ] && eval "$("$b" shellenv)" && break
    done
fi
for pkg in ffmpeg uv gh; do
    command -v "$pkg" >/dev/null 2>&1 || { echo "==> brew install $pkg"; brew install "$pkg"; }
done
BREW_BIN="$(dirname "$(command -v ffmpeg)")"

# 2. Python environment
echo "==> Python dependencies"
cd "$APP_DIR"
uv sync --quiet

# 3. Settings from the old Mac
if [ -n "$SETTINGS" ]; then
    echo "==> Restoring settings from $SETTINGS"
    mkdir -p "$APP_DIR/.cache"
    tar -xzf "$SETTINGS" -C "$APP_DIR/.cache"
fi

# 4. LaunchAgent
echo "==> Service $LABEL on http://localhost:$PORT"
mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>WorkingDirectory</key><string>$APP_DIR</string>
  <key>ProgramArguments</key><array>
    <string>$APP_DIR/.venv/bin/python</string>
    <string>main.py</string><string>--port</string><string>$PORT</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BREW_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>NETV_AUTO_LOGIN</key><string>admin</string>
    <key>NETV_HOST</key><string>0.0.0.0</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- playback first: ahead of background jobs when the Mac is busy -->
  <key>ProcessType</key><string>Interactive</string>
  <key>Nice</key><integer>-10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/netv.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/netv.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
sleep 2
launchctl bootstrap "gui/$(id -u)" "$PLIST"

# 5. Automatic updates from GitHub every 30 minutes (tools/update.sh)
UPDATE_PLIST="$HOME/Library/LaunchAgents/$LABEL.update.plist"
if gh auth status >/dev/null 2>&1; then gh auth setup-git >/dev/null 2>&1 || true; fi
cat > "$UPDATE_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL.update</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$APP_DIR/tools/update.sh</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BREW_BIN:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>NETV_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/update.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/update.log</string>
</dict></plist>
EOF
launchctl bootout "gui/$(id -u)/$LABEL.update" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$UPDATE_PLIST"

# 6. Check
for _ in $(seq 1 30); do
    if curl -s -o /dev/null "http://localhost:$PORT/"; then
        echo "==> neTV is running: http://localhost:$PORT"
        if [ ! -f "$APP_DIR/.cache/server_settings.json" ]; then
            echo "    No settings restored: open the page to create the 'admin' user and add your IPTV source."
        fi
        exit 0
    fi
    sleep 1
done
echo "neTV did not answer, see $LOG_DIR/netv.log"
exit 1
