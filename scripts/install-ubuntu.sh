#!/usr/bin/env bash
# One-time setup for Dikte on Ubuntu (24.04 LTS and newer, GNOME).
# Safe to run again.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/dikte"
WHISPER_DIR="$DATA_DIR/whisper"
# whisper.cpp v1.9.4 prebuilt binaries (checksums from the GitHub release)
WHISPER_TAG="b5130"
WHISPER_SHA_X64="53e7fd8b5764edad916b8848dd0af6abb1ff1d3b86c899e79c78652412536c32"
WHISPER_SHA_ARM64="93532a0e3777f26f041ffa358ee77dd88b1a33a86847c1990745327ff335a5d6"

cd "$ROOT"
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

[[ "$(uname -s)" == "Linux" ]] || { echo "This script is for Linux; use scripts/install.sh on macOS." >&2; exit 1; }
command -v uv >/dev/null || { echo "Please install uv first: sudo snap install astral-uv --classic" >&2; exit 1; }

step "Installing system packages (sudo)"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3-gi python3-gi-cairo gir1.2-ayatanaappindicator3-0.1 libayatana-appindicator3-1 \
  libportaudio2 libnotify-bin gnome-session-canberra sound-theme-freedesktop \
  wl-clipboard xdotool xclip curl

step "Creating the Python environment"
# --system-site-packages so the app can use apt's python3-gi (GTK bindings).
uv venv --python /usr/bin/python3 --system-site-packages .venv
uv pip install --python .venv/bin/python -e .
if ! .venv/bin/python -c "import gi" 2>/dev/null; then
  echo "python3-gi is not visible in the venv; building PyGObject instead"
  sudo apt-get install -y --no-install-recommends libgirepository-2.0-dev libcairo2-dev pkg-config python3-dev gcc
  uv pip install --python .venv/bin/python "pygobject>=3.48"
fi

step "Installing the speech engine (whisper.cpp)"
if command -v whisper-server >/dev/null; then
  echo "✓ whisper-server already installed ($(command -v whisper-server))"
elif [[ -x "$WHISPER_DIR/whisper-server" ]]; then
  echo "✓ whisper-server already downloaded ($WHISPER_DIR)"
else
  case "$(uname -m)" in
    x86_64)  asset="whisper-bin-ubuntu-x64.tar.gz";   sha="$WHISPER_SHA_X64" ;;
    aarch64) asset="whisper-bin-ubuntu-arm64.tar.gz"; sha="$WHISPER_SHA_ARM64" ;;
    *) echo "✗ No prebuilt whisper.cpp for $(uname -m). Build it from source and set whisper_server_path." >&2; exit 1 ;;
  esac
  tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
  echo "Downloading $asset ($WHISPER_TAG)"
  curl -fL -o "$tmp/$asset" "https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/$asset"
  echo "$sha  $tmp/$asset" | sha256sum -c -
  tar -xzf "$tmp/$asset" -C "$tmp"
  server="$(find "$tmp" -name whisper-server -type f | head -1)"
  [[ -n "$server" ]] || { echo "✗ whisper-server not found in the archive" >&2; exit 1; }
  mkdir -p "$WHISPER_DIR"
  cp -a "$(dirname "$server")"/* "$WHISPER_DIR/"
  chmod +x "$WHISPER_DIR/whisper-server"
  echo "✓ Installed to $WHISPER_DIR"
fi

step "Downloading the speech model (skipped if already present)"
.venv/bin/dikte download

step "Registering the app with the desktop"
.venv/bin/python -c "from dikte.platform.linux.login_item import install_desktop_entry; print('✓', install_desktop_entry())"

step "Keyboard access for the hotkey"
if id -nG "$USER" | tr ' ' '\n' | grep -qx input; then
  echo "✓ You are in the 'input' group: hold-to-talk with a single key will work."
else
  cat <<'EOF'
Dikte reads the keyboard device directly so a single key (Right Ctrl) can work
as push-to-talk on Wayland. That needs membership of the "input" group:

    sudo usermod -aG input $USER     # then log out and back in

Note: anyone able to run programs as you could then read everything you type.
If you would rather not, skip it and bind a GNOME shortcut to:

    <project>/.venv/bin/dikte toggle

(Settings → Keyboard → Custom Shortcuts), which gives press-to-start/press-to-stop.
EOF
fi

step "Done"
cat <<EOF
Start Dikte:   .venv/bin/dikte
Check setup:   .venv/bin/dikte doctor
First use:     hold Right Ctrl, speak, release. On Wayland, approve
               "Allow remote interaction" once so Dikte can paste for you.
Terminals:     if Ctrl+V does not paste there, choose
               menu → Output → Paste with → Ctrl+Shift+V.
EOF
