from pathlib import Path
import tomllib


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_readme_and_project_metadata_use_pyside6_bleak_stack() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    metadata = tomllib.loads(pyproject_text)

    assert "PyQt5" not in readme
    assert "PyQt5" not in pyproject_text
    assert "PySide6-Essentials>=6.6" in metadata["project"]["dependencies"]
    assert "bleak>=0.22" in metadata["project"]["dependencies"]
    assert "qasync>=0.27" in metadata["project"]["dependencies"]
    assert "pyinstaller>=6" in metadata["project"]["optional-dependencies"]["dev"]
    assert metadata["tool"]["pytest"]["ini_options"]["qt_api"] == "pyside6"
    assert metadata["project"]["scripts"]["wch-ota"] == "wch_ota.__main__:main"
    assert metadata["project"]["version"] == "1.1.1"
    assert metadata["tool"]["setuptools"]["package-data"]["wch_ota.ble"] == [
        "WCHBLEDLL*.dll"
    ]
    assert metadata["tool"]["setuptools"]["package-data"]["wch_ota"] == [
        "BLE.ico"
    ]


def test_pyinstaller_spec_builds_windowed_directory_distribution() -> None:
    spec = (PROJECT_ROOT / "packaging" / "wch-ota.spec").read_text(
        encoding="utf-8"
    )

    assert "src/wch_ota/__main__.py" in spec.replace("\\\\", "/")
    assert "WCHBLEDLL_v15.dll" in spec
    assert "BLE.ico" in spec
    assert "'wch_ota'" in spec
    assert "icon=str(project_root / 'src/wch_ota/BLE.ico')" in spec
    assert "'wch_ota/ble'" in spec
    assert "name='WCH-BLE-OTA'" in spec
    assert "console=False" in spec
    assert "collect_all('PySide6')" not in spec
    assert "'bleak.backends.winrt.client'" in spec
    assert "'bleak.backends.winrt.scanner'" in spec
    assert "excludes=['PyQt5', 'PyQt6', 'PySide2']" in spec
    assert "COLLECT(" in spec


def test_pyinstaller_onefile_spec_builds_a_single_windowed_executable() -> None:
    spec = (PROJECT_ROOT / "packaging" / "wch-ota-onefile.spec").read_text(
        encoding="utf-8"
    )

    normalized = spec.replace("\\", "/")
    assert "src/wch_ota/__main__.py" in normalized
    assert "WCHBLEDLL_v15.dll" in spec
    assert "BLE.ico" in spec
    assert "icon=str(project_root / 'src/wch_ota/BLE.ico')" in spec
    assert "exclude_binaries=False" in spec
    assert "console=False" in spec
    assert "COLLECT(" not in spec


def test_windows_ble_icon_is_a_valid_ico_file() -> None:
    icon = PROJECT_ROOT / "src" / "wch_ota" / "BLE.ico"
    content = icon.read_bytes()

    assert content[:4] == b"\x00\x00\x01\x00"
    assert int.from_bytes(content[4:6], "little") >= 4


def test_pyinstaller_entrypoint_uses_package_absolute_imports() -> None:
    entrypoint = (PROJECT_ROOT / "src/wch_ota/__main__.py").read_text(
        encoding="utf-8"
    )

    assert "from wch_ota.app import create_application" in entrypoint
    assert "from .app import create_application" not in entrypoint


def test_gitignore_excludes_local_build_and_test_artifacts() -> None:
    ignored = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    for pattern in (".venv/", "build/", "dist/", "*.egg-info/", ".pytest_cache/"):
        assert pattern in ignored


def test_readme_matches_python_builder_default_onefile_distribution() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert "python build_exe.py" in readme
    assert "默认生成单文件" in readme
    assert "python build_exe.py --directory" in readme
