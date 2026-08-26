"""BLE OTA transport contract shared by production and state-machine fakes."""

from collections.abc import Callable
from typing import Any, Protocol


FEE0_UUID = "0000fee0-0000-1000-8000-00805f9b34fb"
FEE1_UUID = "0000fee1-0000-1000-8000-00805f9b34fb"

ScanCallback = Callable[[Any, Any], None]
DisconnectCallback = Callable[[Any], None]


class TransportError(RuntimeError):
    """A Bluetooth transport failure suitable for display to Chinese users."""


class Transport(Protocol):
    """Minimal asynchronous interface consumed by the OTA state machine."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def max_write_without_response_size(self) -> int: ...

    @property
    def effective_mtu(self) -> int: ...

    @property
    def ota_characteristic_properties(self) -> tuple[str, ...]: ...

    @property
    def supports_write_with_response(self) -> bool: ...

    async def start_scan(self, callback: ScanCallback) -> None: ...

    async def stop_scan(self) -> None: ...

    async def connect(self, device: Any) -> None: ...

    async def read_ota(self, use_cached: bool = False) -> bytes: ...

    async def discard_ota_responses(self) -> None: ...

    async def write_ota(self, payload: bytes, *, response: bool = False) -> None: ...

    async def disconnect(self) -> None: ...
