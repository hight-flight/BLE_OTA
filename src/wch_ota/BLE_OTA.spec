# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/script/code_project/BLE_OTA/pc_ota/src/wch_ota/__main__.py'],
    pathex=[],
    binaries=[('D:/script/code_project/BLE_OTA/pc_ota/src/wch_ota/ble/WCHBLEDLL.dll', 'wch_ota/ble'), ('D:/script/code_project/BLE_OTA/pc_ota/src/wch_ota/ble/WCHBLEDLL_v15.dll', 'wch_ota/ble')],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PyQt5'],
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
    name='BLE_OTA',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
