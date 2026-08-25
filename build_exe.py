"""使用当前 Python 环境构建 WCH BLE OTA Windows 程序。"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
SPEC_FILE = PROJECT_ROOT / "packaging" / "wch-ota.spec"
WCH_DLL = PROJECT_ROOT / "src" / "wch_ota" / "ble" / "WCHBLEDLL_v15.dll"
OUTPUT_EXE = PROJECT_ROOT / "dist" / "WCH-BLE-OTA" / "WCH-BLE-OTA.exe"

CommandRunner = Callable[[list[str]], None]


def run_command(command: list[str]) -> None:
    """在项目根目录执行命令，失败时立即终止构建。"""
    subprocess.run(command, cwd=PROJECT_ROOT, check=True)


def _validate_inputs() -> None:
    if sys.version_info < (3, 11):
        raise RuntimeError("需要 Python 3.11 或更高版本")
    if not SPEC_FILE.is_file():
        raise FileNotFoundError(f"未找到 PyInstaller 配置：{SPEC_FILE}")
    if not WCH_DLL.is_file():
        raise FileNotFoundError(f"未找到 WCH 蓝牙 DLL：{WCH_DLL}")


def build(
    *,
    skip_tests: bool = False,
    runner: CommandRunner = run_command,
) -> Path:
    """运行测试并通过 PyInstaller 构建目录分发包。"""
    _validate_inputs()

    if not skip_tests:
        print("[1/2] 运行测试……", flush=True)
        with tempfile.TemporaryDirectory(
            prefix="wch-ota-pytest-",
            ignore_cleanup_errors=True,
        ) as temp_dir:
            runner(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    f"--basetemp={Path(temp_dir) / 'run'}",
                ]
            )
    else:
        print("[1/2] 已跳过测试", flush=True)

    print("[2/2] 构建 Windows 程序……", flush=True)
    runner(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            str(SPEC_FILE),
        ]
    )

    if not OUTPUT_EXE.is_file():
        raise FileNotFoundError(f"构建结束但未找到输出程序：{OUTPUT_EXE}")
    return OUTPUT_EXE


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="打包 WCH BLE OTA Windows 程序")
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="跳过 pytest（仅在已经完成测试时使用）",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        output = build(skip_tests=args.skip_tests)
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"[错误] 打包失败：{exc}", file=sys.stderr)
        return 1

    print(f"[完成] 程序位置：{output}")
    print(f"[提示] 分发时请复制整个目录：{output.parent}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
