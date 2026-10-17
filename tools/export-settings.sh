#!/bin/bash
# Pack the neTV settings to move them to another machine (see tools/install-mac.sh).
#
# Usage: ./tools/export-settings.sh [OUTPUT]   (default: ~/Desktop/netv-settings.tar.gz)
#
# Contains the IPTV accounts (with their passwords), users, favorites and watch history.
# Keep it private: never commit it, send it by AirDrop or USB stick.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$HOME/Desktop/netv-settings.tar.gz}"
cd "$APP_DIR/.cache"
tar -czf "$OUT" --exclude '*.bak-*' server_settings.json users
chmod 600 "$OUT"
echo "Settings saved to $OUT"
