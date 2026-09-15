#!/usr/bin/env bash
# One-time setup for Dikte: dependencies, speech model and Dikte.app.
# Safe to run again (e.g. after moving the project folder).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SUPPORT="$HOME/Library/Application Support/Dikte"
cd "$ROOT"

step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ "$(uname -s)" == "Darwin" ]] || { echo "Dikte only runs on macOS." >&2; exit 1; }
command -v uv >/dev/null || { echo "Please install uv first: brew install uv" >&2; exit 1; }

if ! command -v whisper-server >/dev/null && [[ ! -x /opt/homebrew/bin/whisper-server ]]; then
  step "Installing whisper.cpp with Homebrew"
  brew install whisper-cpp
fi

step "Installing Python dependencies"
uv sync

step "Downloading the speech model (skipped if already present)"
uv run dikte download

step "Building Dikte.app"
"$ROOT/scripts/build_app.sh"

mkdir -p "$SUPPORT"
# The launcher refuses a config that others could modify.
( umask 077 && printf 'python=%s\nworkdir=%s\n' "$ROOT/.venv/bin/python" "$ROOT" > "$SUPPORT/launcher.conf.tmp" )
mv "$SUPPORT/launcher.conf.tmp" "$SUPPORT/launcher.conf"
chmod 600 "$SUPPORT/launcher.conf"

step "Done"
cat <<EOF
Start Dikte:   open ~/Applications/Dikte.app
Then allow Microphone and Accessibility when macOS asks
(System Settings → Privacy & Security). If the hotkey does nothing,
also allow Input Monitoring there and choose "Restart Dikte" from the menu.

Use it:        hold Right ⌘, speak, release → the text appears at your cursor.
               Double-tap Right ⌘ for hands-free; tap again (or pause) to finish.
Check setup:   uv run dikte doctor
EOF
