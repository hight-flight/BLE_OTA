"""日志导出。"""

from pathlib import Path


def export_log(file_path: str | Path, text: str) -> None:
    Path(file_path).write_text(text, encoding="utf-8", newline="")
