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
    _ADVERTISING_SNAPSHOT_SIZE,
    _decode_native_string,
    _default_dll_path,
    _verify_dll_abi,
    _parse_advertising_snapshot,
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
        self.enumerate_calls = 0

    def _record_thread(self) -> None:
        self.thread_ids.append(threading.get_ident())

    def initialize(self) -> None:
        self._record_thread()
        self.initialized = True

    def enumerate_devices(self, _scan_ms: int):
        self._record_thread()
        self.enumerate_calls += 1
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
        self.open_timeouts = []

    def WCHBLEInit(self) -> None:
        self.initialized = True

    def WCHBLESetOpenTimeOut(self, timeout_ms) -> bool:
        self.open_timeouts.append(int(getattr(timeout_ms, "value", timeout_ms)))
        return True

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
            message=getattr(self, "error_message", b"write failed"),
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


def test_native_device_name_prefers_utf8_before_windows_code_page() -> None:
    value = bytearray(260)
    encoded = "设备名称".encode("utf-8")
    value[: len(encoded)] = encoded

    assert _decode_native_string(value) == "设备名称"


def test_official_dll_abi_guard_accepts_bundled_binary() -> None:
    _verify_dll_abi(_default_dll_path())


def test_official_dll_abi_guard_rejects_replaced_binary(tmp_path) -> None:
    replaced = tmp_path / "WCHBLEDLL_v15.dll"
    replaced.write_bytes(_default_dll_path().read_bytes() + b"modified")

    with pytest.raises(TransportError, match="版本或内容不匹配"):
        _verify_dll_abi(replaced)


@pytest.mark.asyncio
async def test_default_scan_window_returns_results_within_one_second() -> None:
    class ScanDurationBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.scan_durations: list[int] = []

        def enumerate_devices(self, scan_ms: int):
            self.scan_durations.append(scan_ms)
            return super().enumerate_devices(scan_ms)

    binding = ScanDurationBinding()
    transport = WchDllTransport(binding=binding)

    await transport.start_scan(lambda _device, _advertisement: None)
    await transport.stop_scan()

    assert binding.scan_durations[0] == 1000


@pytest.mark.asyncio
async def test_default_continuous_scan_uses_long_window_to_find_slow_devices() -> None:
    class ScanDurationBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.scan_durations: list[int] = []

        def enumerate_devices(self, scan_ms: int):
            self.scan_durations.append(scan_ms)
            return super().enumerate_devices(scan_ms)

    binding = ScanDurationBinding()
    transport = WchDllTransport(binding=binding)

    await transport.start_scan(lambda _device, _advertisement: None)
    for _ in range(50):
        if len(binding.scan_durations) >= 2:
            break
        await asyncio.sleep(0.01)
    await transport.stop_scan()

    assert binding.scan_durations[:2] == [1000, 3000]


@pytest.mark.asyncio
async def test_live_scan_periodically_enumerates_devices_as_driver_watchdog() -> None:
    class SilentLiveScanBinding(FakeBinding):
        @property
        def supports_live_scan(self) -> bool:
            return True

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            return True

        def open_device_address(self, _address: str, _callback):
            return self.handle

    binding = SilentLiveScanBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=10)
    transport.live_scan_watchdog_interval = 0
    transport.live_scan_watchdog_duration_ms = 1

    await transport.start_scan(lambda _device, _advertisement: None)
    for _ in range(50):
        if binding.enumerate_calls >= 2:
            break
        await asyncio.sleep(0.01)
    await transport.stop_scan()

    assert binding.enumerate_calls >= 2


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
    assert advertisement.name_priority == 10


@pytest.mark.asyncio
async def test_wch_scan_repeats_until_explicitly_stopped() -> None:
    binding = FakeBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    discoveries = []

    await transport.start_scan(
        lambda device, advertisement: discoveries.append((device, advertisement))
    )
    for _ in range(50):
        if binding.enumerate_calls >= 2:
            break
        await asyncio.sleep(0.01)

    assert transport.scan_is_continuous
    assert binding.enumerate_calls >= 2
    assert len(discoveries) >= 2

    await transport.stop_scan()
    calls_after_stop = binding.enumerate_calls
    await asyncio.sleep(0.05)

    assert binding.enumerate_calls == calls_after_stop


@pytest.mark.asyncio
async def test_wch_continuous_scan_recovers_after_one_enumeration_error() -> None:
    class FlakyScanBinding(FakeBinding):
        def enumerate_devices(self, _scan_ms: int):
            self._record_thread()
            self.enumerate_calls += 1
            if self.enumerate_calls == 2:
                raise RuntimeError("临时扫描失败")
            return list(self.records)

    binding = FlakyScanBinding()
    traces = []
    transport = WchDllTransport(
        binding=binding, scan_duration_ms=1, trace_callback=traces.append
    )

    await transport.start_scan(lambda _device, _advertisement: None)
    for _ in range(100):
        if binding.enumerate_calls >= 3:
            break
        await asyncio.sleep(0.01)

    assert binding.enumerate_calls >= 3
    assert any("持续扫描失败，将继续重试" in message for message in traces)
    await transport.stop_scan()


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


def test_parses_live_advertising_name_mac_and_rssi() -> None:
    snapshot = bytearray(_ADVERTISING_SNAPSHOT_SIZE)
    snapshot[0:6] = bytes.fromhex("DC 32 62 1A FD 22")
    snapshot[16:20] = (4).to_bytes(4, "little")  # scan response
    snapshot[20:24] = (1).to_bytes(4, "little")
    snapshot[56:60] = (1).to_bytes(4, "little")
    name = b"JGS_CHICKEN_1.4.04"
    snapshot[60:64] = (0x09).to_bytes(4, "little")
    snapshot[64:68] = len(name).to_bytes(4, "little")
    snapshot[68 : 68 + len(name)] = name
    snapshot[0x10F10:0x10F14] = (-47).to_bytes(4, "little", signed=True)

    record = _parse_advertising_snapshot(snapshot)

    assert record.address == "DC:32:62:1A:FD:22"
    assert record.name == "JGS_CHICKEN_1.4.04"
    assert record.rssi == -47
    assert record.device_id == ""
    assert record.name_priority == 40


def test_binding_registers_and_unregisters_live_advertising_callback() -> None:
    class AdvertisingDll(FakeRawDll):
        def __init__(self) -> None:
            super().__init__()
            self.callbacks = []

        def WCHBLERegisterAdvertisingNotify(self, callback) -> int:
            self.callbacks.append(callback)
            if callback:
                snapshot = (ctypes.c_ubyte * _ADVERTISING_SNAPSHOT_SIZE)()
                snapshot[0:6] = bytes.fromhex("DC 32 62 1A FD 22")
                snapshot[0x10F10:0x10F14] = (-55).to_bytes(
                    4, "little", signed=True
                )
                callback(ctypes.addressof(snapshot))
            return 1

    raw = AdvertisingDll()
    binding = WchDllBinding(library=raw)
    records = []

    assert binding.register_advertising_notify(records.append)
    assert records[0].address == "DC:32:62:1A:FD:22"
    assert records[0].rssi == -55
    assert binding.unregister_advertising_notify()
    assert raw.callbacks[-1] is None


def test_failed_advertising_unregistration_keeps_callback_alive_for_retry() -> None:
    class FlakyAdvertisingDll(FakeRawDll):
        def __init__(self) -> None:
            super().__init__()
            self.stop_results = [0, 1]

        def WCHBLERegisterAdvertisingNotify(self, callback) -> int:
            if callback:
                self.callback = callback
                return 1
            return self.stop_results.pop(0)

    binding = WchDllBinding(library=FlakyAdvertisingDll())
    assert binding.register_advertising_notify(lambda _record: None)

    assert not binding.unregister_advertising_notify()
    assert binding._advertising_callback is not None
    assert binding.unregister_advertising_notify()
    assert binding._advertising_callback is None


@pytest.mark.asyncio
async def test_transport_retries_failed_native_scan_unregistration() -> None:
    class FlakyStopBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.stop_results = [False, True]

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def open_device_address(self, _address, _callback):
            return self.handle

        def unregister_advertising_notify(self) -> bool:
            return self.stop_results.pop(0)

    transport = WchDllTransport(binding=FlakyStopBinding())
    await transport.start_scan(lambda *_args: None)

    with pytest.raises(TransportError, match="注销实时广播扫描失败"):
        await transport.stop_scan()
    assert transport._scan_active
    assert transport._native_scan_active

    await transport.stop_scan()
    assert not transport._native_scan_active


@pytest.mark.asyncio
async def test_stop_waits_for_in_progress_native_scan_registration() -> None:
    class BlockingStartBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()
            self.unregistered = False

        def register_advertising_notify(self, _callback) -> bool:
            self.entered.set()
            self.release.wait(timeout=2)
            return True

        def open_device_address(self, _address, _callback):
            return self.handle

        def unregister_advertising_notify(self) -> bool:
            self.unregistered = True
            return True

    binding = BlockingStartBinding()
    transport = WchDllTransport(binding=binding)
    start_task = asyncio.create_task(transport.start_scan(lambda *_args: None))
    assert await asyncio.to_thread(binding.entered.wait, 1)
    stop_task = asyncio.create_task(transport.stop_scan())
    await asyncio.sleep(0)
    assert not stop_task.done()

    binding.release.set()
    await start_task
    await stop_task

    assert binding.unregistered
    assert not transport._native_scan_active


@pytest.mark.asyncio
async def test_transport_enriches_names_before_live_advertising_updates() -> None:
    class LiveBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.advertising_callback = None
            self.unregistered = False

        def register_advertising_notify(self, callback) -> bool:
            self.advertising_callback = callback
            return True

        def unregister_advertising_notify(self) -> bool:
            self.unregistered = True
            self.advertising_callback = None
            return True

        def open_device_address(self, _address, _callback):
            return self.handle

    binding = LiveBinding()
    transport = WchDllTransport(binding=binding)
    discoveries = []
    await transport.start_scan(lambda *args: discoveries.append(args))

    assert binding.enumerate_calls == 1
    assert discoveries[0][0].name == "JGS_CHICKEN_1.4.04"
    assert discoveries[0][0].device_id == binding.records[0].device_id

    binding.advertising_callback(Record("", "", "DC:32:62:1A:FD:22", -60))
    await asyncio.sleep(0)
    assert discoveries[-1][0].name == "JGS_CHICKEN_1.4.04"

    binding.advertising_callback(
        Record("Feeder_e8f322", "", "DC:32:62:1A:FD:22", -58)
    )
    await asyncio.sleep(0)

    assert discoveries[-1][0].name == "Feeder_e8f322"
    assert discoveries[-1][1].local_name == "Feeder_e8f322"
    await transport.stop_scan()
    assert binding.unregistered


def test_binding_opens_live_advertising_device_by_address() -> None:
    class AddressDll(FakeRawDll):
        def WCHBLEOpenDeviceAddress(self, address, is_cache_mode, callback):
            self.opened_address = bytes(address)
            self.cache_modes.append(bool(is_cache_mode))
            self.connection_callback = callback
            return self.handle

    raw = AddressDll()
    binding = WchDllBinding(library=raw)

    handle = binding.open_device_address(
        "DC:32:62:1A:FD:22", lambda _handle, _state: None
    )

    assert handle == raw.handle
    assert raw.opened_address == bytes.fromhex("DC 32 62 1A FD 22")
    assert raw.cache_modes == [False]


def test_binding_keeps_failed_native_connection_callback_alive() -> None:
    class EmptyHandleDll(FakeRawDll):
        def WCHBLEOpenDeviceAddress(self, _address, _is_cache_mode, callback):
            self.failed_callback = callback
            return None

    raw = EmptyHandleDll()
    binding = WchDllBinding(library=raw)

    with pytest.raises(TransportError, match="空句柄"):
        binding.open_device_address(
            "DC:32:62:1A:FD:22", lambda _handle, _state: None
        )

    assert raw.failed_callback in binding._retired_connection_callbacks


def test_binding_empty_handle_includes_native_error_details() -> None:
    class EmptyHandleDll(FakeRawDll):
        def WCHBLEOpenDevice(self, _device_id, _is_cache_mode, callback):
            self.connection_callback = callback
            return None

    binding = WchDllBinding(library=EmptyHandleDll())

    with pytest.raises(TransportError) as caught:
        binding.open_device("device-id", lambda _handle, _state: None)

    assert "SDK=10" in str(caught.value)
    assert "OS=0x80070005" in str(caught.value)


def test_binding_configures_native_open_timeout_during_initialization() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw, open_timeout_ms=12_000)

    binding.initialize()

    assert raw.open_timeouts == [12_000]


def test_binding_reports_rejected_native_open_timeout() -> None:
    class TimeoutRejectedDll(FakeRawDll):
        def WCHBLESetOpenTimeOut(self, timeout_ms) -> bool:
            self.open_timeouts.append(int(getattr(timeout_ms, "value", timeout_ms)))
            return False

    binding = WchDllBinding(library=TimeoutRejectedDll())

    with pytest.raises(TransportError, match="连接超时"):
        binding.initialize()


def test_binding_retires_native_connection_callback_after_close() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    handle = binding.open_device("device-id", lambda _handle, _state: None)
    callback = raw.connection_callback

    binding.close_device(handle)

    assert callback in binding._retired_connection_callbacks


@pytest.mark.asyncio
async def test_transport_connects_live_scan_result_by_address() -> None:
    class AddressBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.connected_address = None

        def open_device_address(self, address: str, callback):
            self._record_thread()
            self.connected_address = address
            self.connection_callback = callback
            return self.handle

    binding = AddressBinding()
    transport = WchDllTransport(binding=binding)

    await transport.connect(WchDevice("Feeder_e8f322", "78:21:84:E8:F3:22", ""))

    assert binding.connected_address == "78:21:84:E8:F3:22"
    assert binding.connected_id is None
    await transport.disconnect()


@pytest.mark.asyncio
async def test_live_scan_is_not_used_without_address_connection_capability() -> None:
    class NotifyOnlyBinding(FakeBinding):
        def register_advertising_notify(self, _callback) -> bool:
            raise AssertionError("缺少按地址连接能力时不应注册实时广播")

    binding = NotifyOnlyBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)

    await transport.start_scan(lambda *_args: None)
    await transport.stop_scan()

    assert binding.enumerate_calls >= 1


@pytest.mark.asyncio
async def test_address_connection_failure_falls_back_to_resolved_device_id() -> None:
    class FallbackBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.address_attempts = []
            self.unregister_calls = 0

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            self.unregister_calls += 1
            return True

        def open_device_address(self, address: str, _callback):
            self.address_attempts.append(address)
            raise TransportError("按地址连接失败")

    binding = FallbackBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    await transport.start_scan(lambda *_args: None)

    await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", ""))

    assert binding.address_attempts == ["DC:32:62:1A:FD:22"]
    assert binding.unregister_calls == 1
    assert binding.connected_id == binding.records[0].device_id
    await transport.disconnect()


@pytest.mark.asyncio
async def test_address_connection_retries_device_id_resolution_when_first_scans_miss() -> None:
    class DelayedDiscoveryBinding(FakeBinding):
        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            return True

        def open_device_address(self, _address: str, _callback):
            raise TransportError("按地址连接失败")

        def enumerate_devices(self, _scan_ms: int):
            self._record_thread()
            self.enumerate_calls += 1
            return [] if self.enumerate_calls < 3 else list(self.records)

    binding = DelayedDiscoveryBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    await transport.start_scan(lambda *_args: None)

    await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", ""))

    assert binding.enumerate_calls == 3
    assert binding.connected_id == binding.records[0].device_id
    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_late_disconnect_from_failed_address_attempt_does_not_drop_fallback_connection() -> None:
    class LateCallbackBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.failed_address_callback = None

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            return True

        def open_device_address(self, _address: str, callback):
            self.failed_address_callback = callback
            return None

    binding = LateCallbackBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    await transport.start_scan(lambda *_args: None)

    await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", ""))
    binding.failed_address_callback(None, False)
    await asyncio.sleep(0)

    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_device_id_connection_failure_falls_back_to_address_during_live_scan() -> None:
    class DeviceIdFailureBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.address_attempts = []
            self.unregister_calls = 0

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            self.unregister_calls += 1
            return True

        def open_device(self, device_id: str, _callback):
            self.connected_id = device_id
            raise TransportError("WCHBLEOpenDevice 返回空句柄")

        def open_device_address(self, address: str, callback):
            self.address_attempts.append(address)
            self.connection_callback = callback
            return self.handle

    binding = DeviceIdFailureBinding()
    transport = WchDllTransport(binding=binding)
    await transport.start_scan(lambda *_args: None)
    stale_id = "BluetoothLE#stale-dc:32:62:1a:fd:22"

    await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", stale_id))

    assert binding.connected_id == stale_id
    assert binding.address_attempts == ["DC:32:62:1A:FD:22"]
    assert binding.unregister_calls == 1
    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_device_id_connection_failure_rescans_and_retries_latest_device_id() -> None:
    class RefreshedIdBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.open_attempts = []

        def open_device(self, device_id: str, callback):
            self.open_attempts.append(device_id)
            if device_id.startswith("BluetoothLE#stale-"):
                raise TransportError("WCHBLEOpenDevice 返回空句柄")
            self.connection_callback = callback
            return self.handle

    binding = RefreshedIdBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    stale_id = "BluetoothLE#stale-dc:32:62:1a:fd:22"

    await transport.connect(WchDevice("OTA", "dc-32-62-1a-fd-22", stale_id))

    assert binding.enumerate_calls == 1
    assert binding.open_attempts == [stale_id, binding.records[0].device_id]
    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_early_disconnect_from_stale_device_id_falls_back_to_fresh_device_id() -> None:
    class EarlyDisconnectThenConnectBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.open_attempts = []

        def open_device(self, device_id: str, callback):
            self.open_attempts.append(device_id)
            if device_id.startswith("BluetoothLE#stale-"):
                callback(self.handle, False)
            else:
                self.connection_callback = callback
            return self.handle

    binding = EarlyDisconnectThenConnectBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    stale_id = "BluetoothLE#stale-dc:32:62:1a:fd:22"

    await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", stale_id))

    assert binding.open_attempts == [stale_id, binding.records[0].device_id]
    assert binding.closed == [binding.handle]
    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_connect_retries_gatt_discovery_until_device_is_ready() -> None:
    class SlowGattBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.discovery_calls = 0

        def has_ota_characteristic(self, handle) -> bool:
            assert handle is self.handle
            self.discovery_calls += 1
            return self.discovery_calls >= 3

    binding = SlowGattBinding()
    transport = WchDllTransport(binding=binding)
    transport.gatt_discovery_retry_delay = 0

    await transport.connect(
        WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)
    )

    assert binding.discovery_calls == 3
    assert transport.is_connected
    await transport.disconnect()


@pytest.mark.asyncio
async def test_failed_live_scan_unregistration_blocks_device_id_fallback() -> None:
    class StopFailureDuringFallbackBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.open_attempts = []

        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            return False

        def open_device_address(self, _address: str, _callback):
            return None

        def open_device(self, device_id: str, callback):
            self.open_attempts.append(device_id)
            return super().open_device(device_id, callback)

    binding = StopFailureDuringFallbackBinding()
    transport = WchDllTransport(binding=binding, scan_duration_ms=1)
    await transport.start_scan(lambda *_args: None)

    with pytest.raises(TransportError, match="注销实时广播扫描失败"):
        await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", ""))

    assert binding.open_attempts == []
    assert transport._scan_active
    assert transport._native_scan_active


@pytest.mark.asyncio
async def test_connect_closes_new_handle_when_stopping_live_scan_fails() -> None:
    class StopFailureBinding(FakeBinding):
        def register_advertising_notify(self, _callback) -> bool:
            return True

        def unregister_advertising_notify(self) -> bool:
            return False

        def open_device_address(self, _address: str, callback):
            self.connection_callback = callback
            return self.handle

    binding = StopFailureBinding()
    transport = WchDllTransport(binding=binding)
    await transport.start_scan(lambda *_args: None)

    with pytest.raises(TransportError, match="注销实时广播扫描失败"):
        await transport.connect(WchDevice("OTA", "DC:32:62:1A:FD:22", ""))

    assert binding.closed == [binding.handle]
    assert not transport.is_connected


@pytest.mark.asyncio
async def test_disconnect_callback_before_open_returns_rejects_connection() -> None:
    class EarlyDisconnectBinding(FakeBinding):
        def open_device(self, device_id: str, callback):
            self.connected_id = device_id
            callback(self.handle, False)
            return self.handle

    binding = EarlyDisconnectBinding()
    transport = WchDllTransport(binding=binding)
    device = WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)

    with pytest.raises(TransportError, match="连接建立期间设备已断开"):
        await transport.connect(device)

    assert not transport.is_connected
    assert binding.closed == [binding.handle, binding.handle]


def test_binding_accepts_device_id_suffix_after_device_mac() -> None:
    class SuffixedDeviceIdDll(FakeRawDll):
        def WCHBLEEnumDevice(self, _duration, _filter, records, count) -> None:
            name = b"SENSOR"
            device_id = (
                b"BluetoothLE#BluetoothLEb8:1e:a4:e6:64:96-"
                b"dc:32:62:1a:fd:22_suffix"
            )
            records[0].Name[: len(name)] = name
            records[0].DevID[: len(device_id)] = device_id
            records[0].Rssi = -60
            count._obj.value = 1

    records = WchDllBinding(library=SuffixedDeviceIdDll()).enumerate_devices(10)

    assert len(records) == 1
    assert records[0].address == "DC:32:62:1A:FD:22"


def test_binding_provides_capacity_for_crowded_scan_results() -> None:
    class CapacityDll(FakeRawDll):
        def __init__(self) -> None:
            super().__init__()
            self.capacity = 0

        def WCHBLEEnumDevice(self, _duration, _filter, _records, count) -> None:
            self.capacity = count._obj.value
            count._obj.value = 0

    raw = CapacityDll()

    WchDllBinding(library=raw).enumerate_devices(10)

    assert raw.capacity == 256


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


def test_binding_decodes_utf8_dll_error_message() -> None:
    raw = FakeRawDll()
    raw.write_status = 12
    raw.error_message = "操作已被用户取消。".encode("utf-8")
    binding = WchDllBinding(library=raw)

    with pytest.raises(TransportError, match="操作已被用户取消"):
        binding.write_ota(raw.handle, b"\x82", False)


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
    assert (raw.handle, 0xFEE1) not in binding._notify_callbacks


def test_binding_retires_notify_callback_after_successful_unregister() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    binding.register_read_notify(raw.handle, 0xFEE1)
    callback = raw.notification_registrations[-1][2]

    binding.unregister_read_notify(raw.handle, 0xFEE1)

    assert callback in binding._retired_notify_callbacks


def test_late_notify_from_reused_handle_does_not_pollute_new_connection() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    binding.register_read_notify(raw.handle, 0xFEE1)
    old_callback = raw.notification_registrations[-1][2]
    binding.unregister_read_notify(raw.handle, 0xFEE1)
    binding.register_read_notify(raw.handle, 0xFEE1)
    current_callback = raw.notification_registrations[-1][2]

    stale = ctypes.create_string_buffer(b"\x05\x00")
    current = ctypes.create_string_buffer(b"\x00\x00")
    old_callback(None, stale, 2)
    current_callback(None, current, 2)

    assert binding.read_notify(raw.handle, 0xFEE1) == b"\x00\x00"
    assert binding.read_notify(raw.handle, 0xFEE1) == b""


def test_notification_buffers_are_isolated_by_connection_handle() -> None:
    raw = FakeRawDll()
    binding = WchDllBinding(library=raw)
    first_handle = 0x1111
    second_handle = 0x2222

    binding.register_read_notify(first_handle, 0xFEE1)
    first_callback = raw.notification_registrations[-1][2]
    binding.register_read_notify(second_handle, 0xFEE1)
    second_callback = raw.notification_registrations[-1][2]
    stale = (ctypes.c_ubyte * 2)(0x05, 0x00)
    current = (ctypes.c_ubyte * 2)(0x00, 0x00)
    first_callback(None, stale, 2)
    second_callback(None, current, 2)

    assert binding.read_notify(second_handle, 0xFEE1) == b"\x00\x00"


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
async def test_initialization_trace_contains_runtime_diagnostics() -> None:
    traces = []
    transport = WchDllTransport(binding=FakeBinding(), trace_callback=traces.append)

    await transport.start_scan(lambda _device, _advertisement: None)
    await transport.stop_scan()

    assert any("进程=" in trace and "连接超时=" in trace for trace in traces)


@pytest.mark.asyncio
async def test_repeated_empty_scans_emit_windows_environment_hint() -> None:
    binding = FakeBinding()
    binding.records = []
    traces = []
    transport = WchDllTransport(binding=binding, trace_callback=traces.append)

    for _ in range(3):
        await transport._scan_once(lambda _device, _advertisement: None, scan_duration_ms=1)

    assert any(
        "Bluetooth Support Service" in trace and "驱动" in trace for trace in traces
    )


def test_live_advertising_resets_empty_compatibility_scan_counter() -> None:
    transport = WchDllTransport(binding=FakeBinding())
    transport._scan_active = True
    transport._scan_callback = lambda _device, _advertisement: None
    transport._empty_scan_cycles = 2

    transport._dispatch_advertising_record(
        Record("OTA", "", "DC:32:62:1A:FD:22", -45)
    )

    assert transport._empty_scan_cycles == 0


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


@pytest.mark.asyncio
async def test_connect_failure_unregisters_notify_before_closing_handle() -> None:
    class MtuFailureBinding(FakeBinding):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_events = []

        def get_mtu(self, handle) -> int:
            raise TransportError("读取 MTU 失败")

        def unregister_read_notify(self, handle, characteristic_uuid: int) -> None:
            self.cleanup_events.append(("unregister", characteristic_uuid))
            super().unregister_read_notify(handle, characteristic_uuid)

        def close_device(self, handle) -> None:
            self.cleanup_events.append(("close", handle))
            super().close_device(handle)

    binding = MtuFailureBinding()
    transport = WchDllTransport(binding=binding)

    with pytest.raises(TransportError, match="读取 MTU 失败"):
        await transport.connect(
            WchDevice("OTA", "DC:32:62:1A:FD:22", binding.records[0].device_id)
        )

    assert binding.cleanup_events[:2] == [
        ("unregister", 0xFEE1),
        ("unregister", 0xFEE2),
    ]
    assert binding.cleanup_events[-1] == ("close", binding.handle)


def test_transport_uses_conservative_program_and_verify_pacing_without_sync_reads() -> None:
    transport = WchDllTransport(binding=FakeBinding())

    assert transport.program_packet_delay == 0.012
    assert transport.verify_packet_delay == 0.012
    assert not hasattr(transport, "verify_write_with_response")
    assert not hasattr(transport, "verify_status_per_packet")


@pytest.mark.asyncio
async def test_shutdown_releases_executor_and_rejects_future_operations():
    class RecordingExecutor:
        def __init__(self):
            self.calls = []

        def shutdown(self, *, wait, cancel_futures):
            self.calls.append((wait, cancel_futures))

    transport = WchDllTransport(binding=FakeBinding())
    executor = RecordingExecutor()
    transport._executor = executor

    await transport.shutdown()

    assert executor.calls == [(False, True)]
    with pytest.raises(TransportError, match="已经关闭"):
        await transport.start_scan(lambda _device, _advertisement: None)
