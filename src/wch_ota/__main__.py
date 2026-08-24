import asyncio
import sys

from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from wch_ota.app import create_application


def _configure_bleak_windows_gui() -> None:
    """Allow Bleak WinRT calls on Qt's STA thread with qasync integration."""
    if sys.platform != "win32":
        return
    from bleak.backends.winrt.util import allow_sta

    allow_sta()


def main() -> int:
    application = QApplication(sys.argv)
    loop = QEventLoop(application)
    asyncio.set_event_loop(loop)
    _configure_bleak_windows_gui()

    window = create_application()
    window.show()
    application.aboutToQuit.connect(loop.stop)

    with loop:
        loop.run_forever()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
