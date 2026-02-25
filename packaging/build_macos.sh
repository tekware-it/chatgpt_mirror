#!/usr/bin/env bash
set -euo pipefail

# Build a macOS .app bundle via PyInstaller and optionally wrap it in a DMG.
#
# Requirements:
# - Python env with PyInstaller + app deps installed
# - macOS host (for hdiutil)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="chatgpt_mirror"
DIST_DIR="$ROOT_DIR/dist"
APP_BUNDLE="$DIST_DIR/${APP_NAME}.app"
APP_ONEDIR="$DIST_DIR/${APP_NAME}"
DMG_PATH="$DIST_DIR/ChatGPTMirror-macos.dmg"

cd "$ROOT_DIR"

echo "[1/2] Building PyInstaller .app bundle..."
python3 -m PyInstaller --noconfirm --clean "packaging/${APP_NAME}.spec"

DMG_SRC=""
if [[ -d "$APP_BUNDLE" ]]; then
  DMG_SRC="$APP_BUNDLE"
elif [[ -d "$APP_ONEDIR" ]]; then
  # Our current PyInstaller spec builds a cross-platform onedir bundle. Until we
  # add a macOS-specific BUNDLE stanza in the spec, package the onedir folder in
  # the DMG so the release job still produces a usable artifact.
  DMG_SRC="$APP_ONEDIR"
else
  echo "PyInstaller output not found. Expected one of:" >&2
  echo "  $APP_BUNDLE" >&2
  echo "  $APP_ONEDIR" >&2
  exit 1
fi

echo "[2/2] Creating DMG..."
rm -f "$DMG_PATH"
hdiutil create -volname "ChatGPT Mirror" -srcfolder "$DMG_SRC" -ov -format UDZO "$DMG_PATH"

echo "Done. DMG: $DMG_PATH"
