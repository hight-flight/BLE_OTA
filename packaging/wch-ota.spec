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
    datas=[],
    hiddenimports=[
        'bleak.backends.winrt.client',
        'bleak.backends.winrt.scanner',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # qasync 会动态探测多种 Qt 绑定；发行包只允许收集选定的 PySide6。
    excludes=['PyQt5', 'PyQt6', 'PySide2'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='WCH-BLE-OTA',
)
