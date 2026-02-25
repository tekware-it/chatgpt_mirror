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
DMG_PATH="$DIST_DIR/ChatGPTMirror-macos.dmg"

cd "$ROOT_DIR"

echo "[1/2] Building PyInstaller .app bundle..."
python3 -m PyInstaller --noconfirm --clean "packaging/${APP_NAME}.spec"

if [[ ! -d "$APP_BUNDLE" ]]; then
  echo "PyInstaller .app bundle not found: $APP_BUNDLE" >&2
  exit 1
fi

echo "[2/2] Creating DMG..."
rm -f "$DMG_PATH"
hdiutil create -volname "ChatGPT Mirror" -srcfolder "$APP_BUNDLE" -ov -format UDZO "$DMG_PATH"

echo "Done. DMG: $DMG_PATH"

