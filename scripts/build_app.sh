#!/usr/bin/env bash
# Builds Dikte.app (a tiny native launcher, signed ad hoc) into ~/Applications.
#
# macOS ties privacy permissions to the launcher's code signature, so a rebuild
# means granting Microphone/Accessibility again. This script therefore only
# rebuilds when the launcher sources change. Pass --force to rebuild anyway.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${DIKTE_APP_DIR:-$HOME/Applications}/Dikte.app"
BUNDLE_ID="local.dikte.Dikte"
SOURCES=("$ROOT/launcher/launcher.c" "$ROOT/launcher/Info.plist" "$ROOT/launcher/entitlements.plist" "$ROOT/scripts/make_icon.py" "$0")
HASH="$(cat "${SOURCES[@]}" | shasum -a 256 | cut -d' ' -f1)"
STAMP="$DEST/Contents/Resources/source.sha256"

if [[ "${1:-}" != "--force" && -f "$STAMP" && "$(cat "$STAMP")" == "$HASH" ]]; then
  echo "✓ Dikte.app is up to date ($DEST)"
  exit 0
fi

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
APP="$BUILD/Dikte.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

clang -O2 -Wall -Wextra -Werror -mmacosx-version-min=14.0 \
  -o "$APP/Contents/MacOS/Dikte" "$ROOT/launcher/launcher.c"
cp "$ROOT/launcher/Info.plist" "$APP/Contents/Info.plist"
if ! "$ROOT/.venv/bin/python" "$ROOT/scripts/make_icon.py" "$APP/Contents/Resources/Dikte.icns"; then
  echo "(icon generation failed; continuing with the default icon)"
fi
echo "$HASH" > "$APP/Contents/Resources/source.sha256"
# Hardened runtime blocks DYLD_* injection into the process that holds the permissions.
codesign --force --sign - --identifier "$BUNDLE_ID" --options runtime \
  --entitlements "$ROOT/launcher/entitlements.plist" "$APP"

mkdir -p "$(dirname "$DEST")"
if [[ -d "$DEST" ]]; then
  # Only ever replace a Dikte bundle (any bundle id, so older builds can be upgraded).
  existing_exe="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$DEST/Contents/Info.plist" 2>/dev/null || true)"
  if [[ "$existing_exe" != "Dikte" ]]; then
    echo "✗ $DEST exists but is not Dikte; not replacing it." >&2
    exit 1
  fi
  rm -rf "$DEST"
fi
mv "$APP" "$DEST"
echo "✓ Built $DEST"
echo "  (macOS will ask for Microphone and Accessibility permission again after a rebuild)"
