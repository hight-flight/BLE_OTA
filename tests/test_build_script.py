import importlib.util
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_windows_build_script_validates_and_packages_directory_distribution() -> None:
    script = (PROJECT_ROOT / "build_exe.cmd").read_text(encoding="utf-8")
    normalized = script.lower()

    assert 'cd /d "%~dp0"' in normalized
    assert '.venv\\scripts\\python.exe' in normalized
    assert 'src\\wch_ota\\ble\\wchbledll_v15.dll' in normalized
    assert '"%python_exe%" -m pytest -q' in normalized
    assert (
        '"%python_exe%" -m pyinstaller --noconfirm --clean '
        'packaging\\wch-ota.spec'
    ) in normalized
    assert 'dist\\wch-ble-ota\\wch-ble-ota.exe' in normalized
    assert "if errorlevel 1" in normalized


def _load_python_build_script():
    script_path = PROJECT_ROOT / "build_exe.py"
    spec = importlib.util.spec_from_file_location("build_exe", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_python_build_script_runs_tests_then_pyinstaller(tmp_path) -> None:
    module = _load_python_build_script()
    module.ONEFILE_SPEC_FILE = tmp_path / "wch-ota-onefile.spec"
    module.WCH_DLL = tmp_path / "WCHBLEDLL_v15.dll"
    module.ONEFILE_OUTPUT_EXE = tmp_path / "dist" / "WCH-BLE-OTA.exe"
    module.ONEFILE_SPEC_FILE.write_text("# test spec", encoding="utf-8")
    module.WCH_DLL.write_bytes(b"dll")

    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> None:
        commands.append(command)
        if "PyInstaller" in command:
            module.ONEFILE_OUTPUT_EXE.parent.mkdir(parents=True)
            module.ONEFILE_OUTPUT_EXE.write_bytes(b"exe")

    result = module.build(runner=fake_runner)

    assert commands[0][:-1] == [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    basetemp = commands[0][-1]
    assert basetemp.startswith("--basetemp=")
    pytest_temp = Path(basetemp.removeprefix("--basetemp="))
    assert pytest_temp.name == "run"
    assert pytest_temp.parent.name.startswith("wch-ota-pytest-")
    assert commands[1] == [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        str(module.ONEFILE_SPEC_FILE),
    ]
    assert result == module.ONEFILE_OUTPUT_EXE


def test_python_build_script_can_skip_tests(tmp_path) -> None:
    module = _load_python_build_script()
    module.ONEFILE_SPEC_FILE = tmp_path / "wch-ota-onefile.spec"
    module.WCH_DLL = tmp_path / "WCHBLEDLL_v15.dll"
    module.ONEFILE_OUTPUT_EXE = tmp_path / "dist" / "WCH-BLE-OTA.exe"
    module.ONEFILE_SPEC_FILE.write_text("# test spec", encoding="utf-8")
    module.WCH_DLL.write_bytes(b"dll")

    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> None:
        commands.append(command)
        module.ONEFILE_OUTPUT_EXE.parent.mkdir(parents=True, exist_ok=True)
        module.ONEFILE_OUTPUT_EXE.write_bytes(b"exe")

    module.build(skip_tests=True, runner=fake_runner)

    assert len(commands) == 1
    assert commands[0][1:3] == ["-m", "PyInstaller"]


def test_python_build_script_can_build_directory_distribution(tmp_path) -> None:
    module = _load_python_build_script()
    module.DIRECTORY_SPEC_FILE = tmp_path / "wch-ota.spec"
    module.WCH_DLL = tmp_path / "WCHBLEDLL_v15.dll"
    module.DIRECTORY_OUTPUT_EXE = (
        tmp_path / "dist" / "WCH-BLE-OTA" / "WCH-BLE-OTA.exe"
    )
    module.DIRECTORY_SPEC_FILE.write_text("# test spec", encoding="utf-8")
    module.WCH_DLL.write_bytes(b"dll")
    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> None:
        commands.append(command)
        module.DIRECTORY_OUTPUT_EXE.parent.mkdir(parents=True, exist_ok=True)
        module.DIRECTORY_OUTPUT_EXE.write_bytes(b"exe")

    result = module.build(skip_tests=True, onefile=False, runner=fake_runner)

    assert commands[0][-1] == str(module.DIRECTORY_SPEC_FILE)
    assert result == module.DIRECTORY_OUTPUT_EXE


def test_python_build_script_defaults_to_onefile_cli_mode() -> None:
    module = _load_python_build_script()

    assert module._parse_args([]).onefile is True
    assert module._parse_args(["--onefile"]).onefile is True
    assert module._parse_args(["--directory"]).onefile is False
