import importlib


def test_application_uses_pyside6_widgets() -> None:
    """桌面应用应基于 PySide6 的 Qt Widgets 绑定运行。"""
    widgets = importlib.import_module("PySide6.QtWidgets")

    assert widgets.QMainWindow.__module__.startswith("PySide6.")
