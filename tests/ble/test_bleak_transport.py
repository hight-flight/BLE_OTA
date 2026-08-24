"""Bleak transport contracts exercised without real Bluetooth hardware."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from bleak.exc import BleakError

from wch_ota.ble.bleak_transport import BleakTransport
from wch_ota.ble.transport import FEE0_UUID, FEE1_UUID, TransportError


pytestmark = pytest.mark.asyncio


class FakeScanner:
    def __init__(self, detection_callback: Any = None) -> None:
        self.detection_callback = detection_callback
        self.started = False
        self.stopped = False
        self.stop_errors: list[Exception] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        if self.stop_errors:
            raise self.stop_errors.pop(0)
        self.stopped = True

    def emit(self, device: object, advertisement: object) -> None:
        assert self.detection_callback is not None
        self.detection_callback(device, advertisement)


class FakeCharacteristic:
    def __init__(
        self,
        uuid: str = FEE1_UUID,
        max_write_size: int = 20,
        properties: list[str] | None = None,
    ) -> None:
        self.uuid = uuid
        self.max_write_without_response_size = max_write_size
        self.properties = properties or ["read", "write-without-response"]


class FakeService:
    def __init__(self, characteristic: FakeCharacteristic | None) -> None:
        self.uuid = FEE0_UUID
        self.characteristic = characteristic

    def get_characteristic(self, uuid: str) -> FakeCharacteristic | None:
        return self.characteristic if uuid.lower() == FEE1_UUID else None


class FakeServices:
    def __init__(self, service: FakeService | None) -> None:
        self.service = service

    def get_service(self, uuid: str) -> FakeService | None:
        return self.service if uuid.lower() == FEE0_UUID else None


class FakeClient:
    def __init__(
        self,
        device: object,
        disconnected_callback: Any = None,
        *,
        service: FakeService | None = None,
        winrt: dict[str, str] | None = None,
    ) -> None:
        self.device = device
        self.disconnected_callback = disconnected_callback
        self.services = FakeServices(service)
        self.is_connected = False
        self.disconnect_calls = 0
        self.read_result = bytearray(b"\x01\x02")
        self.read_calls: list[tuple[object, bool]] = []
        self.write_calls: list[tuple[object, bytes, bool]] = []
        self.disconnect_errors: list[Exception] = []
        self.winrt = winrt

    async def connect(self) -> None:
        self.is_connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        if self.disconnect_errors:
            raise self.disconnect_errors.pop(0)
        self.is_connected = False

    async def read_gatt_char(self, characteristic: object, *, use_cached: bool) -> bytearray:
        self.read_calls.append((characteristic, use_cached))
        return self.read_result

    async def write_gatt_char(
        self, characteristic: object, payload: bytes, *, response: bool
    ) -> None:
        self.write_calls.append((characteristic, bytes(payload), response))

    def emit_disconnect(self) -> None:
        self.is_connected = False
        if self.disconnected_callback is not None:
            self.disconnected_callback(self)


def make_transport(
    *,
    service: FakeService | None = None,
    disconnected_callback: Any = None,
) -> tuple[BleakTransport, list[FakeScanner], list[FakeClient]]:
    scanners: list[FakeScanner] = []
    clients: list[FakeClient] = []

    def scanner_factory(*, detection_callback: Any) -> FakeScanner:
        scanner = FakeScanner(detection_callback)
        scanners.append(scanner)
        return scanner

    def client_factory(
        device: object,
        *,
        disconnected_callback: Any,
        winrt: dict[str, str] | None = None,
    ) -> FakeClient:
        client = FakeClient(
            device,
            disconnected_callback,
            service=service,
            winrt=winrt,
        )
        clients.append(client)
        return client

    transport = BleakTransport(
        scanner_factory=scanner_factory,
        client_factory=client_factory,
        disconnected_callback=disconnected_callback,
    )
    return transport, scanners, clients


async def test_stale_client_disconnect_callback_is_ignored_after_reconnect() -> None:
    notifications: list[object] = []
    service = FakeService(FakeCharacteristic())
    transport, _, clients = make_transport(
        service=service, disconnected_callback=notifications.append
    )

    await transport.connect(object())
    old_client = clients[-1]
    await transport.disconnect()
    await transport.connect(object())
    current_client = clients[-1]

    old_client.emit_disconnect()
    assert notifications == []
    assert transport.is_connected

    current_client.emit_disconnect()
    assert notifications == [current_client]
    assert not transport.is_connected


async def test_scan_stays_active_and_forwards_device_with_advertisement_until_stopped() -> None:
    transport, scanners, _ = make_transport()
    discoveries: list[tuple[object, object]] = []

    await transport.start_scan(lambda device, advertisement: discoveries.append((device, advertisement)))
    device = SimpleNamespace(address="AA:BB")
    advertisement = SimpleNamespace(local_name="WCH")
    scanners[0].emit(device, advertisement)

    assert scanners[0].started is True
    assert scanners[0].stopped is False
    assert discoveries == [(device, advertisement)]

    await transport.stop_scan()
    assert scanners[0].stopped is True


async def test_stop_scan_failure_preserves_scanner_for_retry() -> None:
    transport, scanners, _ = make_transport()
    await transport.start_scan(lambda _device, _advertisement: None)
    failure = BleakError("radio busy")
    scanners[0].stop_errors.append(failure)

    with pytest.raises(TransportError, match="停止蓝牙扫描失败") as caught:
        await transport.stop_scan()

    assert caught.value.__cause__ is failure
    await transport.stop_scan()
    assert scanners[0].stopped is True


async def test_start_and_stop_scan_are_serialized() -> None:
    start_entered = asyncio.Event()
    release_start = asyncio.Event()

    class BlockingScanner(FakeScanner):
        async def start(self) -> None:
            start_entered.set()
            await release_start.wait()
            await super().start()

    scanner = BlockingScanner()
    transport = BleakTransport(scanner_factory=lambda **_kwargs: scanner)

    start_task = asyncio.create_task(transport.start_scan(lambda _device, _advertisement: None))
    await start_entered.wait()
    stop_task = asyncio.create_task(transport.stop_scan())
    await asyncio.sleep(0)
    assert stop_task.done() is False

    release_start.set()
    await asyncio.gather(start_task, stop_task)
    assert scanner.stopped is True


async def test_connect_uses_exact_scanned_device_and_records_fee1_characteristic() -> None:
    characteristic = FakeCharacteristic(max_write_size=244)
    transport, _, clients = make_transport(service=FakeService(characteristic))
    scanned_device = SimpleNamespace(address="AA:BB")

    await transport.connect(scanned_device)

    assert clients[0].device is scanned_device
    assert transport.is_connected is True
    assert transport.max_write_without_response_size == 244
    assert transport.effective_mtu == 247


@pytest.mark.parametrize(
    ("native_address_type", "expected_address_type"),
    [(0, "public"), (1, "random")],
)
async def test_connect_passes_scanned_winrt_address_type_to_client(
    native_address_type: int, expected_address_type: str
) -> None:
    characteristic = FakeCharacteristic()
    transport, _, clients = make_transport(service=FakeService(characteristic))
    event_args = SimpleNamespace(bluetooth_address_type=native_address_type)
    scanned_device = SimpleNamespace(
        address="C8:F0:9E:0A:8C:E2",
        details=SimpleNamespace(adv=event_args, scan=None),
    )

    await transport.connect(scanned_device)

    assert clients[0].device is scanned_device
    assert clients[0].winrt == {
        "address_type": expected_address_type,
        "use_cached_services": False,
    }


async def test_connect_disables_windows_gatt_service_cache_without_address_type() -> None:
    transport, _, clients = make_transport(service=FakeService(FakeCharacteristic()))

    await transport.connect(SimpleNamespace(address="AA:BB"))

    assert clients[0].winrt == {"use_cached_services": False}


@pytest.mark.parametrize(
    ("service", "message"),
    [
        (None, "FEE0"),
        (FakeService(None), "FEE1"),
    ],
)
async def test_connect_rejects_missing_ota_uuid_and_cleans_up(
    service: FakeService | None, message: str
) -> None:
    transport, _, clients = make_transport(service=service)

    with pytest.raises(TransportError, match=message):
        await transport.connect(SimpleNamespace(address="AA:BB"))

    assert clients[0].disconnect_calls == 1
    assert transport.is_connected is False


async def test_connect_rejects_reentry_without_replacing_existing_client() -> None:
    transport, _, clients = make_transport(service=FakeService(FakeCharacteristic()))
    device = SimpleNamespace(address="AA:BB")
    await transport.connect(device)

    with pytest.raises(TransportError, match="已经连接"):
        await transport.connect(SimpleNamespace(address="CC:DD"))

    assert len(clients) == 1
    assert clients[0].device is device
    assert transport.is_connected is True


async def test_connect_and_disconnect_are_serialized() -> None:
    connect_entered = asyncio.Event()
    release_connect = asyncio.Event()
    clients: list[FakeClient] = []

    class BlockingClient(FakeClient):
        async def connect(self) -> None:
            connect_entered.set()
            await release_connect.wait()
            await super().connect()

    def client_factory(
        device: object, *, disconnected_callback: Any, **_kwargs: Any
    ) -> BlockingClient:
        client = BlockingClient(
            device,
            disconnected_callback,
            service=FakeService(FakeCharacteristic()),
        )
        clients.append(client)
        return client

    transport = BleakTransport(client_factory=client_factory)
    connect_task = asyncio.create_task(transport.connect(SimpleNamespace(address="AA:BB")))
    await connect_entered.wait()
    disconnect_task = asyncio.create_task(transport.disconnect())
    await asyncio.sleep(0)
    assert disconnect_task.done() is False

    release_connect.set()
    await asyncio.gather(connect_task, disconnect_task)
    assert clients[0].disconnect_calls == 1
    assert transport.is_connected is False


async def test_read_ota_uses_characteristic_and_cache_option_and_returns_bytes() -> None:
    characteristic = FakeCharacteristic()
    transport, _, clients = make_transport(service=FakeService(characteristic))
    await transport.connect(SimpleNamespace(address="AA:BB"))

    result = await transport.read_ota(use_cached=True)

    assert result == b"\x01\x02"
    assert type(result) is bytes
    assert clients[0].read_calls == [(characteristic, True)]


async def test_write_ota_explicitly_disables_response() -> None:
    characteristic = FakeCharacteristic(max_write_size=4)
    transport, _, clients = make_transport(service=FakeService(characteristic))
    await transport.connect(SimpleNamespace(address="AA:BB"))

    await transport.write_ota(b"1234")

    assert clients[0].write_calls == [(characteristic, b"1234", False)]


async def test_write_ota_can_request_acknowledged_write_when_supported() -> None:
    characteristic = FakeCharacteristic(
        max_write_size=20,
        properties=["read", "write", "write-without-response"],
    )
    transport, _, clients = make_transport(service=FakeService(characteristic))
    await transport.connect(SimpleNamespace(address="AA:BB"))

    await transport.write_ota(b"1234", response=True)

    assert transport.ota_characteristic_properties == (
        "read",
        "write",
        "write-without-response",
    )
    assert transport.supports_write_with_response is True
    assert clients[0].write_calls == [(characteristic, b"1234", True)]


async def test_write_ota_rejects_payload_over_characteristic_limit() -> None:
    characteristic = FakeCharacteristic(max_write_size=4)
    transport, _, clients = make_transport(service=FakeService(characteristic))
    await transport.connect(SimpleNamespace(address="AA:BB"))

    with pytest.raises(TransportError, match="4"):
        await transport.write_ota(b"12345")

    assert clients[0].write_calls == []


async def test_disconnect_is_idempotent() -> None:
    transport, _, clients = make_transport(service=FakeService(FakeCharacteristic()))
    await transport.connect(SimpleNamespace(address="AA:BB"))

    await transport.disconnect()
    await transport.disconnect()

    assert clients[0].disconnect_calls == 1
    assert transport.is_connected is False


async def test_disconnect_failure_preserves_connection_for_retry() -> None:
    transport, _, clients = make_transport(service=FakeService(FakeCharacteristic()))
    await transport.connect(SimpleNamespace(address="AA:BB"))
    failure = BleakError("controller timeout")
    clients[0].disconnect_errors.append(failure)

    with pytest.raises(TransportError, match="断开蓝牙设备失败") as caught:
        await transport.disconnect()

    assert caught.value.__cause__ is failure
    assert transport.is_connected is True
    assert transport.max_write_without_response_size == 20
    await transport.disconnect()
    assert clients[0].disconnect_calls == 2
    assert transport.is_connected is False


async def test_unexpected_disconnect_is_forwarded_to_caller() -> None:
    callbacks: list[object] = []
    transport, _, clients = make_transport(
        service=FakeService(FakeCharacteristic()),
        disconnected_callback=callbacks.append,
    )
    await transport.connect(SimpleNamespace(address="AA:BB"))

    clients[0].emit_disconnect()

    assert callbacks == [clients[0]]
    assert transport.is_connected is False


async def test_bleak_exception_becomes_chinese_transport_error_with_cause() -> None:
    class FailingClient(FakeClient):
        async def connect(self) -> None:
            raise BleakError("adapter unavailable")

    def client_factory(
        device: object, *, disconnected_callback: Any, **_kwargs: Any
    ) -> FailingClient:
        return FailingClient(device, disconnected_callback)

    transport = BleakTransport(client_factory=client_factory)

    with pytest.raises(TransportError, match="连接蓝牙设备失败") as caught:
        await transport.connect(SimpleNamespace(address="AA:BB"))

    assert isinstance(caught.value.__cause__, BleakError)


async def test_service_lookup_bleak_error_disconnects_client_and_preserves_cause() -> None:
    failure = BleakError("service discovery failed")
    clients: list[FakeClient] = []

    class FailingServices:
        def get_service(self, _uuid: str) -> None:
            raise failure

    def client_factory(
        device: object, *, disconnected_callback: Any, **_kwargs: Any
    ) -> FakeClient:
        client = FakeClient(device, disconnected_callback)
        client.services = FailingServices()
        clients.append(client)
        return client

    transport = BleakTransport(client_factory=client_factory)

    with pytest.raises(TransportError, match="验证 OTA 服务失败") as caught:
        await transport.connect(SimpleNamespace(address="AA:BB"))

    assert caught.value.__cause__ is failure
    assert clients[0].disconnect_calls == 1
    assert transport.is_connected is False


async def test_service_lookup_and_cleanup_failure_preserves_client_for_disconnect_retry() -> None:
    lookup_failure = BleakError("service discovery failed")
    cleanup_failure = BleakError("disconnect timed out")
    clients: list[FakeClient] = []

    class FailingServices:
        def get_service(self, _uuid: str) -> None:
            raise lookup_failure

    def client_factory(
        device: object, *, disconnected_callback: Any, **_kwargs: Any
    ) -> FakeClient:
        client = FakeClient(device, disconnected_callback)
        client.services = FailingServices()
        client.disconnect_errors.append(cleanup_failure)
        clients.append(client)
        return client

    transport = BleakTransport(client_factory=client_factory)

    with pytest.raises(TransportError, match="service discovery failed.*断开失败") as caught:
        await transport.connect(SimpleNamespace(address="AA:BB"))

    assert caught.value.__cause__ is cleanup_failure
    assert clients[0].disconnect_calls == 1
    assert transport.is_connected is True

    await transport.disconnect()
    assert clients[0].disconnect_calls == 2
    assert transport.is_connected is False
