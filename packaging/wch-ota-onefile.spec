# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path(SPECPATH).parent

a = Analysis(
    [str(project_root / 'src/wch_ota/__main__.py')],
    pathex=[str(project_root / 'src')],
    binaries=[
        (
            str(project_root / 'src/wch_ota/ble/WCHBLEDLL_v15.dll'),
            'wch_ota/ble',
        ),
    ],
    datas=[
        (
            str(project_root / 'src/wch_ota/BLE.ico'),
            'wch_ota',
        ),
    ],
    hiddenimports=[
        'bleak.backends.winrt.client',
        'bleak.backends.winrt.scanner',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='WCH-BLE-OTA',
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
    exclude_binaries=False,
    icon=str(project_root / 'src/wch_ota/BLE.ico'),
)
