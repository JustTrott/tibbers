# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build of the Windows app. Run through scripts/build_windows.ps1,
# which resolves the venv and the output; this file just describes the bundle.
#
# What is and is not inside:
#   * tibbers/static and assets/ ARE bundled -- the server reads
#     tibbers/static and the tray reads assets/tibbers.png at runtime, both via
#     Path(__file__), so the tree has to survive the freeze at those paths.
#   * tools/ is NOT bundled. The cslol and LTK patcher binaries are fetched
#     onto the user's machine on first run (tibbers/wintools.py); they are not
#     ours to redistribute. The frozen app looks for them in the data dir.
#
# The WebView2 and system-tray backends pull native pieces (pythonnet/clr, the
# WebView2 runtime shims, pystray's win32 backend) that a bare Analysis misses,
# so those packages are collected whole.

import os
from PyInstaller.utils.hooks import collect_all

datas = [
    ('tibbers/static', 'tibbers/static'),
    ('assets', 'assets'),
]
binaries = []
hiddenimports = ['clr']

for package in ('webview', 'pystray', 'PIL'):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

icon = 'assets/tibbers.ico' if os.path.exists('assets/tibbers.ico') else None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Tibbers',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # a tray app; it logs to <data dir>\tibbers.log
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='Tibbers',
)
