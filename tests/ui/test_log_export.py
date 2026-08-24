from pathlib import Path

from wch_ota.ui.log_export import export_log


def test_export_log_writes_utf8(tmp_path: Path):
    target = tmp_path / "ota.log"

    export_log(target, "[12:00:00] 升级完成\n")

    assert target.read_bytes() == "[12:00:00] 升级完成\n".encode("utf-8")
