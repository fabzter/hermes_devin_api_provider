#!/usr/bin/env bash
# install.sh - install devin-provider for Hermes. Idempotent.
#   ./install.sh            symlink the CLI into ~/.local/bin and merge the Hermes provider entry
#   ./install.sh --cutover  also remove the old devin-acp source patch from Hermes
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./install.sh [--cutover]

  --cutover   also run scripts/remove_devin_acp.py --apply (removes the old devin-acp patch)
EOF
}

CUTOVER=0
for arg in "$@"; do
  case "$arg" in
    --cutover) CUTOVER=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "install.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
PY="${PYTHON:-python3}"
BIN_DIR="$HOME/.local/bin"
LINK="$BIN_DIR/devin-provider"
SHIM="$REPO/bin/devin-provider"

echo "==> checking Python"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "install.sh: Python 3.11+ required (found: $("$PY" -V 2>&1))" >&2
  exit 1
fi

echo "==> linking CLI into $BIN_DIR"
mkdir -p "$BIN_DIR"
chmod +x "$SHIM"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "install.sh: $LINK exists and is not a symlink; remove it manually and re-run" >&2
  exit 1
fi
ln -sfn "$SHIM" "$LINK"
echo "    $LINK -> $SHIM"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "    note: $BIN_DIR is not on PATH; add: export PATH=\"$BIN_DIR:\$PATH\"" ;;
esac

if ! command -v devin >/dev/null 2>&1; then
  echo "    warning: 'devin' CLI not found; install it, then run: devin auth login" >&2
fi

echo "==> merging Hermes provider entry"
"$PY" "$REPO/scripts/install_hermes.py"

if [ "$CUTOVER" -eq 1 ]; then
  echo "==> cutover: removing old devin-acp integration"
  "$PY" "$REPO/scripts/remove_devin_acp.py" --apply
fi

cat <<'EOF'

Done. Next steps:
  1. Log in once:         devin auth login
  2. Run the provider:    devin-provider serve      (or: make launchd, to run as a LaunchAgent)
  3. Probe it:            curl -s http://127.0.0.1:8787/healthz
  4. In Hermes:           hermes model  ->  pick "Devin (Teams)"
  List catalog models:    devin-provider models
EOF
