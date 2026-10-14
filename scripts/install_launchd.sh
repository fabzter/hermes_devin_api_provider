#!/usr/bin/env bash
# Install (or refresh) and start the devin-provider LaunchAgent. Idempotent.
set -euo pipefail

LABEL="com.fabzter.devin-provider"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SRC="$REPO/launchd/$LABEL.plist"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DEST="$AGENTS_DIR/$LABEL.plist"
BIN="$HOME/.local/bin/devin-provider"
DOMAIN="gui/$(id -u)"

[ -f "$SRC" ] || { echo "install_launchd.sh: missing $SRC" >&2; exit 1; }
[ -x "$BIN" ] || { echo "install_launchd.sh: missing $BIN; run ./install.sh first" >&2; exit 1; }

mkdir -p "$AGENTS_DIR"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT
# The template hardcodes /Users/fabzter; re-root it for the current user.
sed "s#/Users/fabzter#$HOME#g" "$SRC" > "$TMP"
plutil -lint "$TMP" >/dev/null
chmod 644 "$TMP"
if cmp -s "$TMP" "$DEST" 2>/dev/null; then
  echo "plist unchanged: $DEST"
else
  mv -f "$TMP" "$DEST"
  echo "installed plist: $DEST"
fi

if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
  launchctl bootout "$DOMAIN" "$DEST" >/dev/null 2>&1 \
    || launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 \
    || true
fi
launchctl bootstrap "$DOMAIN" "$DEST"
launchctl kickstart -k "$DOMAIN/$LABEL"

STATE_OUT="$(launchctl print "$DOMAIN/$LABEL" 2>/dev/null || true)"
STATE="$(printf '%s\n' "$STATE_OUT" | awk '/state =/ { s = $3 } END { print s }')"
echo "launchd job $LABEL: ${STATE:-unknown}"
echo "logs: /tmp/devin-provider.log"
