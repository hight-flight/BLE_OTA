import wch_ota.app as app
from wch_ota.app import create_application


def test_create_application_returns_hidden_main_window(qapp):
    window = create_application()

    assert window.windowTitle() == "WCH BLE OTA 工具"
    assert window.size().width() == 1200
    assert window.size().height() == 800
    assert window.isHidden()


def test_wch_dll_transport_is_default_and_disconnect_callback_is_wired(qapp, monkeypatch):
    captured = {}

    class Transport:
        is_connected = False

        def __init__(self, *, disconnected_callback, trace_callback):
            captured["callback"] = disconnected_callback
            captured["trace"] = trace_callback

    class Controller:
        def __init__(self, transport):
            self._event_callback = None

        def cancel(self):
            pass

    monkeypatch.setattr(app, "WchDllTransport", Transport)
    monkeypatch.setattr(app, "OtaController", Controller)
    window = app.create_application()
    window._set_connected(True)
    window._set_upgrading(True)

    captured["callback"](object())
    captured["trace"]("WCH TX 测试诊断")
    qapp.processEvents()

    assert not window.disconnect_button.isEnabled()
    assert not window.cancel_button.isEnabled()
    assert "设备意外断开" in window.log_view.toPlainText()
    assert "WCH TX 测试诊断" not in window.log_view.toPlainText()

    window.detailed_log_checkbox.setChecked(True)
    captured["trace"]("WCH RX 详细诊断")
    qapp.processEvents()

    assert "WCH RX 详细诊断" in window.log_view.toPlainText()


def test_bleak_transport_remains_available_by_environment_override(qapp, monkeypatch):
    captured = {}

    class Transport:
        is_connected = False

        def __init__(self, *, disconnected_callback):
            captured["callback"] = disconnected_callback

    class Controller:
        def __init__(self, _transport):
            self._event_callback = None

    monkeypatch.setenv("WCH_OTA_BLE_BACKEND", "bleak")
    monkeypatch.setattr(app, "BleakTransport", Transport)
    monkeypatch.setattr(app, "OtaController", Controller)

    window = app.create_application()

    assert isinstance(window.transport, Transport)
