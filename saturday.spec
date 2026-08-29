# PyInstaller spec for the Saturday desktop app — Windows / Linux / macOS.
# Build:  python -m PyInstaller saturday.spec --noconfirm
# Output: dist/Saturday/            (Windows + Linux, onedir)
#         dist/Saturday/Saturday.app (macOS, plus onedir)
# The per-OS installers/scripts in scripts/ and installer/ wrap these outputs.
# -*- mode: python ; coding: utf-8 -

import sys

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

a = Analysis(
    ["packaging/launcher.py"],
    pathex=["src"],
    binaries=[],
    datas=[("src/saturday/webui_assets", "saturday/webui_assets")],
    hiddenimports=[
        # pywebview picks its backend lazily; PyInstaller needs them explicit
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pydoc_data",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="Saturday",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="packaging/icons/saturday.ico" if IS_WIN else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Saturday",
)

if IS_MAC:
    app = BUNDLE(
        coll,
        name="Saturday.app",
        icon="packaging/icons/saturday.icns",
        bundle_identifier="org.saturday.app",
    )
