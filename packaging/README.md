# Packaging

This folder contains a pragmatic packaging setup for `chatgpt_mirror` using **PyInstaller**.

## Why PyInstaller

`PySide6 + QtWebEngine` packaging is mostly about shipping the right Qt runtime files.
PyInstaller is the fastest path to reliable `.exe` / `.app` / Linux bundle outputs.

## Outputs

- Linux: PyInstaller `onedir` + AppImage wrapper
- Windows: PyInstaller `onedir` + optional portable `.zip`
- macOS: PyInstaller `.app` + `.dmg`

## Prerequisites

Install app dependencies plus PyInstaller:

```bash
python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller
```

## Build commands

### Linux AppImage

Requires `linuxdeploy` (and optionally `appimagetool`) in `PATH`.

```bash
chmod +x packaging/build_linux_appimage.sh
./packaging/build_linux_appimage.sh
```

### macOS DMG

```bash
chmod +x packaging/build_macos.sh
./packaging/build_macos.sh
```

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\build_windows.ps1
```

## Notes

- The `.spec` file intentionally collects PySide6 runtime files broadly for reliability.
- Bundle size can be optimized later after validating runtime behavior on each OS.
- Code signing / notarization is not included in these scripts yet.

