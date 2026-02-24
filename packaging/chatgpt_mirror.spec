# PyInstaller spec for ChatGPT Mirror (PySide6 + QtWebEngine).
#
# Notes:
# - We build an onedir bundle because QtWebEngine is significantly easier to ship
#   and debug in onedir mode than in onefile mode.
# - QtWebEngine runtime files are collected explicitly because they are a common
#   source of packaging issues across Linux/macOS/Windows.

from __future__ import annotations

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


# PyInstaller executes spec files in a custom namespace where __file__ may be
# unavailable. The build scripts invoke PyInstaller from the project root, so
# using cwd is the most robust default here.
PROJECT_ROOT = Path.cwd().resolve()
APP_NAME = "chatgpt_mirror"


hiddenimports = []

# Pygments is optional at runtime but often installed in production for PDF syntax highlighting.
# Collecting lexer submodules avoids "missing lexer" surprises in packaged builds.
hiddenimports += collect_submodules("pygments.lexers")
hiddenimports += collect_submodules("pygments.formatters")


datas = []
binaries = []

# Pull PySide6 runtime libraries/plugins and QtWebEngine assets. This is intentionally broad:
# size is acceptable for an MVP, while reliability matters more than micro-optimizing bundle size.
binaries += collect_dynamic_libs("PySide6")
datas += collect_data_files("PySide6")


a = Analysis(
    [str(PROJECT_ROOT / "chatgpt_mirror.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    exclude_binaries=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name=APP_NAME,
)
