import os
from typing import Any

from .application.ota_controller import OtaController
from .ble.bleak_transport import BleakTransport
from .ble.wch_dll_transport import WchDllTransport
from .ui.main_window import MainWindow


def create_application(
    *, transport: Any | None = None, controller: Any | None = None
) -> MainWindow:
    """Create the main application window without displaying it."""
    window_reference: dict[str, MainWindow] = {}

    def disconnected_callback(client: Any) -> None:
        window = window_reference.get("window")
        if window is not None:
            window.handle_transport_disconnected(client)

    def trace_callback(message: str) -> None:
        window = window_reference.get("window")
        if window is not None:
            window.handle_transport_trace(message)

    if transport is not None:
        active_transport = transport
    elif os.environ.get("WCH_OTA_BLE_BACKEND", "wch").lower() == "bleak":
        active_transport = BleakTransport(disconnected_callback=disconnected_callback)
    else:
        active_transport = WchDllTransport(
            disconnected_callback=disconnected_callback,
            trace_callback=trace_callback,
        )
    active_controller = controller or OtaController(active_transport)
    window = MainWindow(transport=active_transport, controller=active_controller)
    window_reference["window"] = window
    return window
