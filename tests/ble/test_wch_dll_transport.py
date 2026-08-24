"""WCH 官方 Windows BLE DLL 传输后端契约。"""

from dataclasses import dataclass
import ctypes
import asyncio
import threading
from types import SimpleNamespace

import pytest

from wch_ota.ble.transport import TransportError
from wch_ota.ble.wch_dll_transport import (
    WchDevice,
    WchDllBinding,
    WchDllTransport,
    _default_dll_path,
)


@dataclass
class Record:
    name: str
    device_id: str
    address: str
    rssi: int


class FakeBinding:
    def __init__(self) -> None:
        self.records = [
            Record(
                "JGS_CHICKEN_1.4.04",
                "BluetoothLE#adapter-dc:32:62:1a:fd:22",
                "DC:32:62:1A:FD:22",
                -51,
            )
        ]
        self.initialized = False
        self.handle = object()
        self.connected_id = None
        self.connection_callback = None
        self.closed = []
        self.writes = []
        self.read_result = b"\x00\x00"
        self.ota_properties = ("read", "write", "write-without-response")
        self.characteristics = [0xFEE1]
        self.notify_subscriptions = []
        self.indicate_subscriptions = []
        self.notify_result = 0
        self.indicate_result = 0
        self.read_results: dict[int, bytes] = {}
        self.thread_ids = []

    def _record_thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def initialize(self) -> None:
        self._record_thread()
        self.initialized = True

    def enumerate_devices(self, _scan_ms: int):
        self._record_thread()
        return list(self.records)

    def open_device(self, device_id: str, callback):
        self._record_thread()
        self.connected_id = device_id
        self.connection_callback = callback
        return self.handle

    def has_ota_characteristic(self, handle) -> bool:
        self._record_thread()
        return handle is self.handle

    def get_mtu(self, handle) -> int:
        self._record_thread()
        assert handle is self.handle
        return 247

    def get_ota_characteristic_properties(self, handle):
        self._record_thread()
        assert handle is self.handle
        return self.ota_properties

    def write_ota(self, handle, payload: bytes, response: bool) -> None:
        self._record_thread()
        self.writes.append((handle, payload, response))

    def list_characteristics(self, handle) -> list[int]:
        self._record_thread()
        assert handle is self.handle
        return list(self.characteristics)

    def register_read_notify(self, handle, characteristic_uuid: int) -> int:
        self._record_thread()
        assert handle is self.handle
        self.notify_subscriptions.append(characteristic_uuid)
        return self.notify_result

    def register_read_indicate(self, handle, characteristic_uuid: int) -> int:
        self._record_thread()
        assert handle is self.handle
        self.indicate_subscriptions.append(characteristic_uuid)
        return self.indicate_result

    def read_notify(self, handle, characteristic_uuid: int) -> bytes:
        self._record_thread()
        assert handle is self.handle
        return self.read_results.get(characteristic_uuid, b"")

    def unregister_read_notify(self, handle, characteristic_uuid: int) -> None:
        self._record_thread()
        assert handle is self.handle
        self.notify_subscriptions.remove(characteristic_uuid)

    def read_characteristic(self, handle, characteristic_uuid: int) -> bytes:
        self._record_thread()
        assert handle is self.handle
        return self.read_results.get(characteristic_uuid, self.read_result)

    def close_device(self, handle) -> None:
        self._record_thread()
        self.closed.append(handle)


class FakeRawDll:
    def __init__(self) -> None:
        self.initialized = False
        self.handle = 0x1234
        self.closed = []
        self.written = []
        self.cache_modes = []
        self.read_result = b"\x02\x00"
        self.write_status = 0
        self.notification_registrations = []

    def WCHBLEInit(self) -> None:
        self.initialized = True

    def WCHBLEIsBluetoothOpened(self) -> bool:
        return True

    def WCHBLEIsLowEnergySupported(self) -> bool:
        return True

    def WCHBLEEnumDevice(self, _duration, _filter, records, count) -> None:
        name = b"JGS_CHICKEN_1.4.04"
        device_id = b"BluetoothLE#adapter-dc:32:62:1a:fd:22"
        records[0].Name[: len(name)] = name
        records[0].DevID[: len(device_id)] = device_id
        records[0].Rssi = -48
        count._obj.value = 1

    def WCHBLEOpenDevice(self, _device_id, is_cache_mode, callback):
        self.cache_modes.append(bool(is_cache_mode))
        self.connection_callback = callback
        return self.handle

    def WCHBLECloseDevice(self, handle) -> None:
        self.closed.append(handle)

    def WCHBLEGetCharacteristicByUUID(
        self, _handle, _service, uuids, count
    ) -> int:
        uuids[0] = 0xFEE1
        count._obj.value = 1
        return 0
    def WCHBLEGetMtu(self, _handle, mtu) -> int:
        mtu._obj.value = 247
        return 0

    def WCHBLEGetCharacteristicAction(
        self, _handle, _service, _characteristic, action, attribute_handle
    ) -> int:
        action._obj.value = 0x02 | 0x04 | 0x08
        attribute_handle._obj.value = 0x0042
        return 0

    def WCHBLEWriteCharacteristic(
        self, _handle, _service, _characteristic, response, buffer, length
    ) -> int:
        size = int(getattr(length, "value", length))
        self.written.append((ctypes.string_at(buffer, size), bool(response)))
        return self.write_status

    def WCHBLEGetLastError(self):
        return SimpleNamespace(
            code=10,
            os_error=0x80070005,
            att_error=0x03,
            message=b"write failed",
        )

    def WCHBLEReadCharacteristic(
        self, _handle, _service, _characteristic, buffer, length
    ) -> int:
        ctypes.memmove(buffer, self.read_result, len(self.read_result))
        length._obj.value = len(self.read_result)
        return 0

    def WCHBLERegisterReadNotify(
        self, _handle, _service, _characteristic, callback, param_inf
    ) -> int:
        self.notification_registrations.append(
            ("notify", int(_characteristic), callback)
        )
        return 0

    def WCHBLERegisterReadIndicate(
        self, _handle, _service, _characteristic, callback, param_inf
    ) -> int:
        self.notification_registrations.append(
            ("indicate", int(_characteristic), callback)
        )
        return 0


def test_default_binding_uses_official_v15_dll() -> None:
    assert _default_dll_path().name == "WCHBLEDLL_v15.dll"


@pytest.mark.asyncio
async def test_scan_uses_dll_name_and_normalized_mac_address() -> None:
    binding = FakeBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=10)
    discoveries = []

    await transport.start_scan(
        lambda device, advertisement: discoveries.append((device, advertisement))
    )

    assert binding.initialized
    assert len(discoveries) == 1
    device, advertisement = discoveries[0]
    assert isinstance(device, WchDevice)
    assert device.name == "JGS_CHICKEN_1.4.04"
    assert device.address == "DC:32:62:1A:FD:22"
    assert advertisement.local_name == "JGS_CHICKEN_1.4.04"
    assert advertisement.rssi == -51


def test_binding_decodes_wch_dll_scan_records() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)

    binding.initialize()
    records = binding.enumerate_devices(10)

    assert raw.initialized
    assert len(records) == 1
    assert records[0].name == "JGS_CHICKEN_1.4.04"
    assert records[0].device_id == "BluetoothLE#adapter-dc:32:62:1a:fd:22"
    assert records[0].address == "DC:32:62:1A:FD:22"
    assert records[0].rssi == -48


def test_binding_wraps_fee1_connection_and_io() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    states = []

    handle = binding.open_device("device-id", states.append)

    assert handle == raw.handle
    assert raw.cache_modes == [False]
    assert binding.has_ota_characteristic(handle)
    assert binding.get_ota_characteristic_properties(handle) == (
        "read",
        "write",
        "write-without-response",
    )
    assert binding.get_mtu(handle) == 247
    binding.write_ota(handle, b"\x84\x12", False)
    assert raw.written == [(b"\x84\x12", False)]
    assert binding.read_ota(handle) == b"\x02\x00"
    binding.close_device(handle)
    assert raw.closed == [handle]


def test_binding_reports_official_dll_error_details() -> None:
    raw = FakeRawDll()
    raw.write_status = 10
    binding = WchDllBinding(library=raw)

    with pytest.raises(TransportError) as caught:
        binding.write_ota(raw.handle, b"\x81", False)

    detail = str(caught.value)
    assert "SDK=10" in detail
    assert "OS=0x80070005" in detail
    assert "ATT=0x03" in detail
    assert "write failed" in detail


@pytest.mark.asyncio
async def test_connect_read_write_and_disconnect_use_fee1_binding() -> None:
    binding = FakeBinding()
    traces = []
    transport = WchDllTransport(binding=binding, trace_callback=traces.append)
    device = WchDevice(
        "JGS_CHICKEN_1.4.04",
        "DC:32:62:1A:FD:22",
        binding.records[0].device_id,
    )

    await transport.connect(device)

    assert transport.is_connected
    assert transport.backend_name == "WCH DLL"
    assert transport.effective_mtu == 247
    assert transport.max_write_without_response_size == 244
    assert transport.ota_characteristic_properties == (
        "read",
        "write",
        "write-without-response",
    )
    assert transport.supports_write_with_response
    assert transport.prefer_compact_erase is False
    assert transport.erase_settle_delay == 3.0
    await transport.write_ota(b"\x84\x12", response=False)
    assert binding.writes == [(binding.handle, b"\x84\x12", False)]
    assert await transport.read_ota() == b"\x00\x00"
    assert any("TX" in trace and "84 12" in trace for trace in traces)
    assert any("RX" in trace and "00 00" in trace for trace in traces)
    assert any("MTU=247" in trace for trace in traces)

    await transport.disconnect()

    assert binding.closed == [binding.handle]
    assert not transport.is_connected


@pytest.mark.asyncio
async def test_empty_read_diagnostics_are_aggregated_at_useful_milestones() -> None:
    binding = FakeBinding()
    binding.read_result = b""
    traces = []
    transport = WchDllTransport(binding=binding, trace_callback=traces.append)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)
    await transport.connect(device)

    for _ in range(50):
        assert await transport.read_ota() == b""

    empty_traces = [trace for trace in traces if "空响应累计" in trace]
    assert [int(trace.split("空响应累计=")[1].split("；")[0]) for trace in empty_traces] == [
        1,
        10,
        25,
        50,
    ]


@pytest.mark.asyncio
async def test_all_dll_calls_stay_on_one_dedicated_worker_thread() -> None:
    binding = FakeBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=10)
    await transport.start_scan(lambda _device, _advertisement: None)

    blocker_started = threading.Event()
    blocker_release = threading.Event()

    def occupy_default_worker() -> None:
        blocker_started.set()
        blocker_release.wait(timeout=5)

    blocker = asyncio.create_task(asyncio.to_thread(occupy_default_worker))
    while not blocker_started.is_set():
        await asyncio.sleep(0)
    try:
        await transport.connect(
            WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)
        )
    finally:
        blocker_release.set()
        await blocker

    assert len(set(binding.thread_ids)) == 1


@pytest.mark.asyncio
async def test_connect_rejects_device_without_fee1() -> None:
    binding = FakeBinding()
    binding.has_ota_characteristic = lambda _handle: False
    transport = WchDllTransport(binding=binding)
    device = WchDevice("bad", "AA:BB", "bad-id")

    with pytest.raises(TransportError, match="FEE1"):
        await transport.connect(device)

    assert binding.closed == [binding.handle]


def test_binding_lists_characteristics_and_registers_notify() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)

    assert binding.list_characteristics(raw.handle) == [0xFEE1]
    assert binding.has_ota_characteristic(raw.handle)
    assert binding.register_read_notify(raw.handle, 0xFEE2) == 0
    assert binding.register_read_indicate(raw.handle, 0xFEE2) == 0
    assert binding.read_characteristic(raw.handle, 0xFEE1) == b"\x02\x00"


def test_binding_preserves_notification_frame_boundaries() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    binding.register_read_notify(raw.handle, 0xFEE1)
    callback = raw.notification_registrations[-1][2]

    first = ctypes.create_string_buffer(b"\x00\x11")
    second = ctypes.create_string_buffer(b"\x00\x22")
    callback(None, first, 2)
    callback(None, second, 2)

    assert binding.read_notify(raw.handle, 0xFEE1) == b"\x00\x11"
    assert binding.read_notify(raw.handle, 0xFEE1) == b"\x00\x22"
    assert binding.read_notify(raw.handle, 0xFEE1) == b""


def test_binding_unregisters_indicate_before_releasing_callback() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    binding.register_read_indicate(raw.handle, 0xFEE1)

    binding.unregister_read_notify(raw.handle, 0xFEE1)

    mode, characteristic, callback = raw.notification_registrations[-1]
    assert mode == "indicate"
    assert characteristic == 0xFEE1
    assert callback is None
    assert 0xFEE1 not in binding._notify_callbacks


@pytest.mark.asyncio
async def test_transport_subscribes_fee1_and_falls_back_to_fee2_gatt_read() -> None:
    binding = FakeBinding()
    binding.characteristics = [0xFEE1, 0xFEE2]
    binding.read_result = b""
    binding.read_results[0xFEE2] = b"\x00"
    traces = []
    transport = WchDllTransport(binding=binding, trace_callback=traces.append)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)

    await transport.connect(device)

    # 只订阅 FEE1（与 Android 一致）；WCHBLEDLL 仅保留最后一次读通知回调，
    # 订阅 FEE2 会覆盖 FEE1 导致收不到响应。
    assert transport._notify_characteristic == 0xFEE1
    assert binding.notify_subscriptions == [0xFEE1]
    assert any("订阅 0xFEE1 通知" in trace for trace in traces)
    # 通知缓冲为空时，回退到 GATT 读取（FEE2 可读值兜底）。
    assert await transport.read_ota() == b"\x00"


@pytest.mark.asyncio
async def test_transport_without_fee2_reads_fee1_only() -> None:
    binding = FakeBinding()
    binding.read_result = b""
    transport = WchDllTransport(binding=binding)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)

    await transport.connect(device)

    assert transport._notify_characteristic == 0xFEE1
    assert binding.notify_subscriptions == [0xFEE1]
    assert await transport.read_ota() == b""


@pytest.mark.asyncio
async def test_transport_falls_back_to_indicate_when_notify_rejected() -> None:
    binding = FakeBinding()
    binding.notify_result = 10  # WCHBLEDLL 未上报 notify 属性时返回非 0
    binding.indicate_result = 0
    traces = []
    transport = WchDllTransport(binding=binding, trace_callback=traces.append)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)

    await transport.connect(device)

    assert transport._notify_characteristic == 0xFEE1
    assert binding.notify_subscriptions == [0xFEE1]
    assert binding.indicate_subscriptions == [0xFEE1]
    assert any("订阅 0xFEE1 通知" in trace for trace in traces)
    assert any("订阅 0xFEE1 指示" in trace for trace in traces)


@pytest.mark.asyncio
async def test_transport_keeps_gatt_read_fallback_when_both_subscriptions_fail() -> None:
    binding = FakeBinding()
    binding.notify_result = 10
    binding.indicate_result = 10
    binding.read_result = b""
    binding.read_results[0xFEE1] = b"\x00\x00"
    transport = WchDllTransport(binding=binding)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)

    await transport.connect(device)

    assert transport._notify_characteristic is None
    assert binding.notify_subscriptions == [0xFEE1]
    assert binding.indicate_subscriptions == [0xFEE1]
    assert await transport.read_ota() == b"\x00\x00"
