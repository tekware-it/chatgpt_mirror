#!/usr/bin/env bash
set -euo pipefail

# Build Linux onedir bundle with PyInstaller, then wrap it as AppImage.
#
# Requirements:
# - Python env with PyInstaller + app deps installed
# - linuxdeploy and appimagetool available in PATH
#   (or linuxdeploy AppImage that bundles appimagetool support)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build"
DIST_DIR="$ROOT_DIR/dist"
APP_NAME="chatgpt_mirror"
APPDIR="$BUILD_DIR/AppDir"

cd "$ROOT_DIR"

echo "[1/4] Building PyInstaller onedir bundle..."
python3 -m PyInstaller --noconfirm --clean "packaging/${APP_NAME}.spec"

if [[ ! -d "$DIST_DIR/$APP_NAME" ]]; then
  echo "PyInstaller output not found: $DIST_DIR/$APP_NAME" >&2
  exit 1
fi

if ! command -v linuxdeploy >/dev/null 2>&1; then
  echo "linuxdeploy not found in PATH." >&2
  echo "Install it first, then rerun this script." >&2
  exit 1
fi

echo "[2/4] Preparing AppDir..."
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"

# Keep the PyInstaller onedir layout intact by copying its contents into usr/bin.
cp -a "$DIST_DIR/$APP_NAME/." "$APPDIR/usr/bin/"

cat > "$APPDIR/${APP_NAME}.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ChatGPT Mirror
Exec=${APP_NAME}
Icon=${APP_NAME}
Categories=Utility;Development;
Terminal=false
EOF

# Minimal icon placeholder: linuxdeploy expects an icon file. Replace with a real one later.
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
if command -v convert >/dev/null 2>&1; then
  convert -size 256x256 xc:'#2563eb' -fill white -gravity center -pointsize 80 -annotate 0 'CGM' \
    "$APPDIR/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png"
else
  # Create a tiny valid PNG placeholder only if ImageMagick is unavailable.
  python3 - <<PY
from pathlib import Path
import base64
png = base64.b64decode(
    b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6t0x0AAAAASUVORK5CYII='
)
Path(r"$APPDIR/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png").write_bytes(png)
PY
fi

echo "[3/4] Running linuxdeploy..."
linuxdeploy \
  --appdir "$APPDIR" \
  --desktop-file "$APPDIR/${APP_NAME}.desktop" \
  --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png"

echo "[4/4] Building AppImage..."
if command -v appimagetool >/dev/null 2>&1; then
  appimagetool "$APPDIR" "$DIST_DIR/ChatGPTMirror-x86_64.AppImage"
else
  # Some linuxdeploy builds support --output appimage directly.
  linuxdeploy \
    --appdir "$APPDIR" \
    --desktop-file "$APPDIR/${APP_NAME}.desktop" \
    --icon-file "$APPDIR/usr/share/icons/hicolor/256x256/apps/${APP_NAME}.png" \
    --output appimage
fi

echo "Done. Check $DIST_DIR for the AppImage."

