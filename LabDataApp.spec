# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for LabDataApp
# (ISU Civil Engineering Lab Data Analyzer, incl. VW Live Monitor).
#
# Build:
#     conda activate py38
#     pyinstaller LabDataApp.spec --clean --noconfirm
#
# Produces a single windowed executable: dist/LabDataApp.exe
# Target: Python 3.8, Windows 7+. Deps: pandas, numpy, matplotlib, tkinter,
# openpyxl. No new dependencies are introduced by this spec.

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],
    # openpyxl is imported lazily (Excel export) so PyInstaller's static
    # analysis can miss it; declare it explicitly.
    hiddenimports=['openpyxl', 'pyxlsb', 'matplotlib.backends.backend_pdf'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='LabDataApp',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # windowed (no console) — GUI app
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
