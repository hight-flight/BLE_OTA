import sys

from bleak.backends.winrt import util

from wch_ota import __main__ as entrypoint


def test_windows_gui_allows_bleak_sta(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(util, "allow_sta", lambda: calls.append("allow_sta"))

    entrypoint._configure_bleak_windows_gui()

    assert calls == ["allow_sta"]
