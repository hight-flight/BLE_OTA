@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
set "WCH_DLL=%CD%\src\wch_ota\ble\WCHBLEDLL_v15.dll"
set "OUTPUT_EXE=%CD%\dist\WCH-BLE-OTA\WCH-BLE-OTA.exe"

if not exist "%PYTHON_EXE%" (
    echo [错误] 找不到虚拟环境：%PYTHON_EXE%
    echo 请先执行：py -3.11 -m venv .venv
    echo 然后执行：.venv\Scripts\python.exe -m pip install -e ".[dev]"
    exit /b 1
)

if not exist "%WCH_DLL%" (
    echo [错误] 找不到 WCH DLL：%WCH_DLL%
    exit /b 1
)

"%PYTHON_EXE%" -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 当前虚拟环境未安装 PyInstaller。
    echo 请执行：.venv\Scripts\python.exe -m pip install -e ".[dev]"
    exit /b 1
)

if /I "%~1"=="--skip-tests" goto package

echo [1/2] 正在运行自动化测试...
"%PYTHON_EXE%" -m pytest -q
if errorlevel 1 (
    echo [错误] 测试失败，已停止打包。
    exit /b 1
)

:package
echo [2/2] 正在生成 Windows 发布目录...
"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean packaging\wch-ota.spec
if errorlevel 1 (
    echo [错误] PyInstaller 打包失败。
    exit /b 1
)

if not exist "%OUTPUT_EXE%" (
    echo [错误] 打包命令已结束，但没有找到：%OUTPUT_EXE%
    exit /b 1
)

echo [完成] 可执行程序：%OUTPUT_EXE%
echo 发布时请复制整个 dist\WCH-BLE-OTA 目录，不要只复制 EXE。
exit /b 0
