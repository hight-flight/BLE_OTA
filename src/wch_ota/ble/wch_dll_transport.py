"""基于沁恒 WCHBLEDLL.dll 的 Windows BLE OTA 传输后端。"""

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
import re
import threading
from time import perf_counter
from typing import Any, Callable

from .transport import DisconnectCallback, ScanCallback, TransportError

_MAX_PATH = 260
_MAX_SCAN_DEVICES = 256
_MAC_ADDRESS = re.compile(r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})")
_ADVERTISING_SNAPSHOT_SIZE = 0x10F18
_ADVERTISING_SECTION_COUNT_OFFSET = 56
_ADVERTISING_SECTION_OFFSET = 60
_ADVERTISING_SECTION_STRIDE = 0x10C
_ADVERTISING_RSSI_OFFSET = 0x10F10
_ConnectionCallback = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_ubyte)
_READ_CALLBACK = ctypes.WINFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong)
_AdvertisingCallback = ctypes.WINFUNCTYPE(None, ctypes.c_void_p)
TraceCallback = Callable[[str], None]


class _BLENameDevID(ctypes.Structure):
    _fields_ = [
        ("Name", ctypes.c_ubyte * _MAX_PATH),
        ("DevID", ctypes.c_ubyte * _MAX_PATH),
        ("Rssi", ctypes.c_int),
    ]


class _BLEStatus(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint32),
        ("os_error", ctypes.c_uint32),
        ("att_error", ctypes.c_uint8),
        ("message", ctypes.c_char * 256),
    ]


@dataclass(frozen=True)
class WchScanRecord:
    name: str
    device_id: str
    address: str
    rssi: int


class WchDllBinding:
    """ctypes 封装，集中处理 WCH DLL 的结构体与返回码。"""

    def __init__(self, dll_path: str | Path | None = None, *, library: Any = None) -> None:
        self._connection_callbacks: dict[int, Any] = {}
        # 原生 DLL 可能在打开失败或关闭返回后继续投递迟到事件，因此连接回调
        # 与 binding 同寿命，不能释放仍可能被原生线程调用的函数指针。
        self._retired_connection_callbacks: list[Any] = []
        self._notify_buffers: dict[tuple[int, int], deque[bytes]] = {}
        self._notify_callbacks: dict[tuple[int, int], Any] = {}
        self._notify_modes: dict[tuple[int, int], str] = {}
        self._notify_lock = threading.Lock()
        self._advertising_callback: Any | None = None
        if library is not None:
            self._dll = library
        else:
            path = Path(dll_path) if dll_path is not None else _default_dll_path()
            if not path.is_file():
                raise TransportError(f"找不到 WCH BLE 库：{path}")
            try:
                self._dll = ctypes.WinDLL(str(path))
            except (AttributeError, OSError) as error:
                raise TransportError(f"加载 WCH BLE 库失败：{error}") from error
            self._configure_ctypes()

    def initialize(self) -> None:
        self._dll.WCHBLEInit()
        if not self._dll.WCHBLEIsBluetoothOpened():
            raise TransportError("Windows 蓝牙未开启")
        if not self._dll.WCHBLEIsLowEnergySupported():
            raise TransportError("当前蓝牙适配器不支持 BLE")

    @property
    def supports_live_scan(self) -> bool:
        return all(
            callable(getattr(self._dll, name, None))
            for name in (
                "WCHBLERegisterAdvertisingNotify",
                "WCHBLEOpenDeviceAddress",
            )
        )

    def enumerate_devices(self, scan_ms: int) -> list[WchScanRecord]:
        records = (_BLENameDevID * _MAX_SCAN_DEVICES)()
        count = ctypes.c_ulong(_MAX_SCAN_DEVICES)
        self._dll.WCHBLEEnumDevice(
            ctypes.c_ulong(scan_ms), b"", records, ctypes.byref(count)
        )
        result: list[WchScanRecord] = []
        for raw in records[: count.value]:
            name = _decode_native_string(raw.Name)
            device_id = _decode_native_string(raw.DevID)
            addresses = _MAC_ADDRESS.findall(device_id)
            if not addresses:
                continue
            result.append(
                WchScanRecord(
                    name=name,
                    device_id=device_id,
                    # Windows Device ID 可能同时包含适配器 MAC、设备 MAC 和后缀；
                    # 设备地址是其中最后一个 MAC，不要求它位于字符串末尾。
                    address=addresses[-1].upper(),
                    rssi=int(raw.Rssi),
                )
            )
        return result

    def register_advertising_notify(
        self, callback: Callable[[WchScanRecord], None]
    ) -> bool:
        """持续接收 Windows 广播，与 WCH BLE 调试工具的扫描方式一致。"""
        register = getattr(self._dll, "WCHBLERegisterAdvertisingNotify", None)
        if not callable(register):
            return False

        @_AdvertisingCallback
        def native_callback(snapshot_pointer: Any) -> None:
            if not snapshot_pointer:
                return
            try:
                snapshot = ctypes.string_at(
                    snapshot_pointer, _ADVERTISING_SNAPSHOT_SIZE
                )
                callback(_parse_advertising_snapshot(snapshot))
            except Exception:
                # 不能让 Python 异常越过 ctypes/native 回调边界。
                return

        self._advertising_callback = native_callback
        try:
            success = bool(register(native_callback))
        except Exception:
            self._advertising_callback = None
            raise
        if not success:
            self._advertising_callback = None
        return success

    def unregister_advertising_notify(self) -> bool:
        register = getattr(self._dll, "WCHBLERegisterAdvertisingNotify", None)
        if not callable(register):
            self._advertising_callback = None
            return False
        success = bool(register(None))
        if success:
            self._advertising_callback = None
        return success

    def open_device(self, device_id: str, callback: Any) -> Any:
        @_ConnectionCallback
        def native_callback(native_handle: Any, status: int) -> None:
            callback(native_handle, bool(status))

        handle = self._dll.WCHBLEOpenDevice(
            device_id.encode("mbcs"), False, native_callback
        )
        if not handle:
            self._retired_connection_callbacks.append(native_callback)
            raise TransportError("WCHBLEOpenDevice 返回空句柄")
        self._connection_callbacks[_handle_key(handle)] = native_callback
        return handle

    def open_device_address(self, address: str, callback: Any) -> Any:
        """按广播 MAC 连接，供 AdvertisingNotify 扫描结果使用。"""
        open_by_address = getattr(self._dll, "WCHBLEOpenDeviceAddress", None)
        if not callable(open_by_address):
            raise TransportError("当前 WCH BLE 库不支持按地址连接")
        if _MAC_ADDRESS.fullmatch(address) is None:
            raise TransportError(f"无效的蓝牙地址：{address}")
        # 新版 WCH DLL 的 PCHAR 参数并不是带冒号的显示字符串，而是回调结构中
        # 原样取得的 6 字节 MAC；ctypes 会为 bytes 参数附加字符串终止零。
        raw_address = bytes.fromhex(address.replace(":", ""))

        @_ConnectionCallback
        def native_callback(native_handle: Any, status: int) -> None:
            callback(native_handle, bool(status))

        handle = open_by_address(raw_address, False, native_callback)
        if not handle:
            self._retired_connection_callbacks.append(native_callback)
            raise TransportError("WCHBLEOpenDeviceAddress 返回空句柄")
        self._connection_callbacks[_handle_key(handle)] = native_callback
        return handle

    def close_device(self, handle: Any) -> None:
        self._dll.WCHBLECloseDevice(handle)
        callback = self._connection_callbacks.pop(_handle_key(handle), None)
        if callback is not None:
            self._retired_connection_callbacks.append(callback)

    def list_characteristics(self, handle: Any) -> list[int]:
        uuids = (ctypes.c_ushort * 64)()
        count = ctypes.c_ushort(64)
        status = self._dll.WCHBLEGetCharacteristicByUUID(
            handle, 0xFEE0, uuids, ctypes.byref(count)
        )
        self._require_success(status, "读取 FEE0 特征列表")
        return [int(uuids[index]) for index in range(count.value)]

    def has_ota_characteristic(self, handle: Any) -> bool:
        return 0xFEE1 in self.list_characteristics(handle)

    def register_read_notify(self, handle: Any, characteristic_uuid: int) -> int:
        """订阅特征通知并缓存回送数据。返回 DLL 状态码（0 为成功）。

        WCH BLE OTA 的擦除/编程等响应以通知形式下发到 FEE1（部分设备另含 FEE2）。
        必须传入真正的回调指针；早期实现误把结果缓冲区当回调传入，导致订阅无效、
        设备回送的通知被 DLL 直接丢弃，主机永远读不到擦除完成响应。
        """
        key = (_handle_key(handle), characteristic_uuid)
        c_callback = _READ_CALLBACK(
            lambda param_inf, buf, length: self._on_notify(key, buf, length)
        )
        with self._notify_lock:
            self._notify_buffers[key] = deque()
            self._notify_callbacks[key] = c_callback
            self._notify_modes[key] = "notify"
        status = self._dll.WCHBLERegisterReadNotify(
            handle,
            0xFEE0,
            characteristic_uuid,
            c_callback,
            None,
        )
        # 不在此处抛错：订阅失败（例如 DLL 未上报 notify 属性）时应回退到
        # GATT 特征值读取；由调用方记录状态码以辅助诊断。
        return int(status)

    def register_read_indicate(self, handle: Any, characteristic_uuid: int) -> int:
        """订阅特征指示（indicate）并缓存回送数据。返回 DLL 状态码（0 为成功）。

        部分设备 OTA 特征以 indicate 而非 notify 上报响应；DLL 按属性位拒绝
        notify 订阅时，可改用 indicate 订阅作为回退。
        """
        key = (_handle_key(handle), characteristic_uuid)
        c_callback = _READ_CALLBACK(
            lambda param_inf, buf, length: self._on_notify(key, buf, length)
        )
        with self._notify_lock:
            self._notify_buffers[key] = deque()
            self._notify_callbacks[key] = c_callback
            self._notify_modes[key] = "indicate"
        status = self._dll.WCHBLERegisterReadIndicate(
            handle,
            0xFEE0,
            characteristic_uuid,
            c_callback,
            None,
        )
        return int(status)

    def read_notify(self, handle: Any, characteristic_uuid: int) -> bytes:
        """取出并清空某特征缓存的通知数据。"""
        key = (_handle_key(handle), characteristic_uuid)
        with self._notify_lock:
            frames = self._notify_buffers.get(key)
            return frames.popleft() if frames else b""

    def _on_notify(self, key: tuple[int, int], buf: Any, length: int) -> None:
        if not buf or not length:
            return
        try:
            data = ctypes.string_at(buf, length)
        except Exception:
            return
        with self._notify_lock:
            frames = self._notify_buffers.get(key)
            if frames is not None:
                frames.append(data)

    def unregister_read_notify(self, handle: Any, characteristic_uuid: int) -> None:
        key = (_handle_key(handle), characteristic_uuid)
        with self._notify_lock:
            mode = self._notify_modes.get(key, "notify")
        unregister = (
            self._dll.WCHBLERegisterReadIndicate
            if mode == "indicate"
            else self._dll.WCHBLERegisterReadNotify
        )
        status = unregister(handle, 0xFEE0, characteristic_uuid, None, None)
        if int(status) != 0:
            return
        # 必须在 DLL 注销成功后再释放 CFUNCTYPE，避免原生线程调用悬空指针。
        with self._notify_lock:
            self._notify_callbacks.pop(key, None)
            self._notify_modes.pop(key, None)
            self._notify_buffers.pop(key, None)

    def get_mtu(self, handle: Any) -> int:
        mtu = ctypes.c_ushort()
        status = self._dll.WCHBLEGetMtu(handle, ctypes.byref(mtu))
        self._require_success(status, "读取 MTU")
        if mtu.value < 23:
            raise TransportError(f"WCH DLL 返回无效 MTU：{mtu.value}")
        return int(mtu.value)

    def get_ota_characteristic_properties(self, handle: Any) -> tuple[str, ...]:
        action = ctypes.c_ulong()
        attribute_handle = ctypes.c_ushort()
        status = self._dll.WCHBLEGetCharacteristicAction(
            handle,
            0xFEE0,
            0xFEE1,
            ctypes.byref(action),
            ctypes.byref(attribute_handle),
        )
        self._require_success(status, "读取 FEE1 属性")
        bit_names = (
            (0x02, "read"),
            (0x08, "write"),
            (0x04, "write-without-response"),
            (0x10, "notify"),
            (0x20, "indicate"),
        )
        return tuple(name for bit, name in bit_names if action.value & bit)

    def write_ota(self, handle: Any, payload: bytes, response: bool) -> None:
        buffer = ctypes.create_string_buffer(payload)
        status = self._dll.WCHBLEWriteCharacteristic(
            handle,
            0xFEE0,
            0xFEE1,
            bool(response),
            buffer,
            ctypes.c_uint(len(payload)),
        )
        self._require_success(status, "写入 FEE1")

    def read_characteristic(self, handle: Any, characteristic_uuid: int) -> bytes:
        buffer = ctypes.create_string_buffer(512)
        length = ctypes.c_uint(512)
        status = self._dll.WCHBLEReadCharacteristic(
            handle, 0xFEE0, characteristic_uuid, buffer, ctypes.byref(length)
        )
        self._require_success(status, f"读取 0x{characteristic_uuid:04X}")
        return bytes(buffer.raw[: length.value])

    def read_ota(self, handle: Any) -> bytes:
        return self.read_characteristic(handle, 0xFEE1)

    def _require_success(self, status: int, operation: str) -> None:
        if int(status) == 0:
            return
        summary = f"{operation}失败，WCH DLL 状态码 0x{int(status):02X}"
        get_last_error = getattr(self._dll, "WCHBLEGetLastError", None)
        if get_last_error is None:
            raise TransportError(summary)
        try:
            detail = get_last_error()
            raw_message = bytes(detail.message).split(b"\0", 1)[0]
            try:
                message = raw_message.decode("utf-8")
            except UnicodeDecodeError:
                message = raw_message.decode("mbcs", errors="replace")
            summary += (
                f"；SDK={int(detail.code)}；OS=0x{int(detail.os_error):08X}；"
                f"ATT=0x{int(detail.att_error):02X}"
            )
            if message:
                summary += f"；{message}"
        except Exception:
            pass
        raise TransportError(summary)

    def _configure_ctypes(self) -> None:
        self._dll.WCHBLEInit.restype = None
        self._dll.WCHBLEIsBluetoothOpened.restype = ctypes.c_bool
        self._dll.WCHBLEIsLowEnergySupported.restype = ctypes.c_bool
        self._dll.WCHBLEEnumDevice.argtypes = [
            ctypes.c_ulong,
            ctypes.c_char_p,
            ctypes.POINTER(_BLENameDevID),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        self._dll.WCHBLEOpenDevice.argtypes = [
            ctypes.c_char_p,
            wintypes.BOOL,
            _ConnectionCallback,
        ]
        self._dll.WCHBLEOpenDevice.restype = ctypes.c_void_p
        if hasattr(self._dll, "WCHBLERegisterAdvertisingNotify"):
            self._dll.WCHBLERegisterAdvertisingNotify.argtypes = [ctypes.c_void_p]
            self._dll.WCHBLERegisterAdvertisingNotify.restype = wintypes.BOOL
        if hasattr(self._dll, "WCHBLEOpenDeviceAddress"):
            self._dll.WCHBLEOpenDeviceAddress.argtypes = [
                ctypes.c_char_p,
                wintypes.BOOL,
                _ConnectionCallback,
            ]
            self._dll.WCHBLEOpenDeviceAddress.restype = ctypes.c_void_p
        self._dll.WCHBLECloseDevice.argtypes = [ctypes.c_void_p]
        self._dll.WCHBLECloseDevice.restype = None
        self._dll.WCHBLEGetCharacteristicByUUID.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.POINTER(ctypes.c_ushort),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        self._dll.WCHBLEGetCharacteristicByUUID.restype = ctypes.c_ubyte
        self._dll.WCHBLEGetMtu.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ushort),
        ]
        self._dll.WCHBLEGetMtu.restype = ctypes.c_ubyte
        self._dll.WCHBLEGetCharacteristicAction.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ushort,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ushort),
        ]
        self._dll.WCHBLEGetCharacteristicAction.restype = ctypes.c_ubyte
        self._dll.WCHBLEWriteCharacteristic.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ushort,
            wintypes.BOOL,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        self._dll.WCHBLEWriteCharacteristic.restype = ctypes.c_ubyte
        self._dll.WCHBLEReadCharacteristic.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ushort,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint),
        ]
        self._dll.WCHBLEReadCharacteristic.restype = ctypes.c_ubyte
        self._dll.WCHBLERegisterReadNotify.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ushort,
            _READ_CALLBACK,
            ctypes.c_void_p,
        ]
        self._dll.WCHBLERegisterReadNotify.restype = ctypes.c_ubyte
        self._dll.WCHBLERegisterReadIndicate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ushort,
            ctypes.c_ushort,
            _READ_CALLBACK,
            ctypes.c_void_p,
        ]
        self._dll.WCHBLERegisterReadIndicate.restype = ctypes.c_ubyte
        self._dll.WCHBLEGetLastError.argtypes = []
        self._dll.WCHBLEGetLastError.restype = _BLEStatus


def _decode_native_string(value: Any) -> str:
    raw = bytes(value).split(b"\0", 1)[0]
    return raw.decode("mbcs", errors="replace")


def _decode_advertising_name(value: bytes) -> str:
    for encoding in ("utf-8", "mbcs"):
        try:
            return value.decode(encoding).rstrip("\0")
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace").rstrip("\0")


def _parse_advertising_snapshot(snapshot: bytes | bytearray) -> WchScanRecord:
    """解析新版 WCH DLL 广播回调的固定布局。

    该布局由官方 BLEDebug V1.1 的调用方式及同版 DLL 实际回调验证：
    MAC 位于 0，AD Data Section 数位于 56，每节为 type/length/256-byte data，
    RSSI 位于 0x10F10。完整名称(0x09)优先于缩短名称(0x08)。
    """
    raw = bytes(snapshot)
    if len(raw) < _ADVERTISING_SNAPSHOT_SIZE:
        raise ValueError("WCH 广播回调数据长度不足")
    mac = raw[:6]
    if not any(mac):
        raise ValueError("WCH 广播回调未包含有效 MAC")
    address = ":".join(f"{part:02X}" for part in mac)
    section_count = min(
        int.from_bytes(
            raw[
                _ADVERTISING_SECTION_COUNT_OFFSET : _ADVERTISING_SECTION_COUNT_OFFSET
                + 4
            ],
            "little",
        ),
        256,
    )
    names: dict[int, str] = {}
    for index in range(section_count):
        offset = _ADVERTISING_SECTION_OFFSET + index * _ADVERTISING_SECTION_STRIDE
        if offset + 8 > _ADVERTISING_RSSI_OFFSET:
            break
        data_type = int.from_bytes(raw[offset : offset + 4], "little")
        data_length = min(
            int.from_bytes(raw[offset + 4 : offset + 8], "little"), 256
        )
        if data_type not in (0x08, 0x09) or not data_length:
            continue
        names[data_type] = _decode_advertising_name(
            raw[offset + 8 : offset + 8 + data_length]
        )
    rssi = int.from_bytes(
        raw[_ADVERTISING_RSSI_OFFSET : _ADVERTISING_RSSI_OFFSET + 4],
        "little",
        signed=True,
    )
    return WchScanRecord(
        name=names.get(0x09) or names.get(0x08) or "",
        device_id="",
        address=address,
        rssi=rssi,
    )


def _default_dll_path() -> Path:
    return Path(__file__).with_name("WCHBLEDLL_v15.dll")


def _handle_key(handle: Any) -> int:
    value = getattr(handle, "value", handle)
    return int(value)


def _normalize_mac_address(address: Any) -> str:
    value = str(address or "").strip().upper()
    compact = re.sub(r"[:-]", "", value)
    if re.fullmatch(r"[0-9A-F]{12}", compact):
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    return value


def _format_handle(handle: Any) -> str:
    try:
        return f"0x{_handle_key(handle):X}"
    except (TypeError, ValueError):
        return f"测试/自定义句柄@0x{id(handle):X}"


@dataclass(frozen=True)
class WchDevice:
    name: str
    address: str
    device_id: str


@dataclass(frozen=True)
class WchAdvertisement:
    local_name: str | None
    rssi: int


class WchDllTransport:
    """通过 WCH 官方 Windows DLL 访问 BLE 设备。"""

    backend_name = "WCH DLL"
    scan_is_continuous = True
    # WCHBLEOpenDeviceAddress 依赖实时广播扫描建立的内部设备对象；连接句柄
    # 建立后再注销广播回调。Bleak 等后端仍由界面在连接前停止扫描。
    stop_scan_before_connect = False
    # 与已在目标设备验证可用的 Android Demo 一致：完整 20 字节帧，
    # 写入后静默等待再读取。6 字节第三方工具帧仅由控制器作备用。
    # 擦除是慢操作（50 个 4KB 块约 200KB），CH583 需数秒才能完成；
    # 首读过早会读空并可能打断设备擦除，因此拉长 settle 并降低轮询频率，
    # 把总等待窗口从约 3 秒放宽到十几秒，同时避免高频读干扰擦除任务。
    erase_settle_delay = 3.0
    erase_read_attempts = 20
    erase_read_retry_delay = 0.5
    prefer_compact_erase = False
    # WCHBLEWriteCharacteristic 的无响应模式只表示数据已提交，并不保证外设
    # 已消费。6 ms 间隔仍可能让 CH583 的 Flash 编程处理跟不上并静默丢包，
    # 最终由 FLASH_ROM_VERIFY 返回非零状态，因此编程和校验都保守使用 12 ms。
    ota_packet_delay = 0.012
    program_packet_delay = 0.012
    # 校验命令还会同步读取 Flash，处理时间不短于编程命令。WCH DLL 的“有响应”
    # 模式仍会快速返回，不能作为可靠背压，因此保持 Android 的无响应写入方式。
    verify_packet_delay = 0.012
    device_id_resolution_attempts = 3
    gatt_discovery_attempts = 6
    gatt_discovery_retry_delay = 0.25

    def __init__(
        self,
        *,
        binding: Any | None = None,
        dll_path: str | Path | None = None,
        scan_duration_ms: int = 3000,
        disconnected_callback: DisconnectCallback | None = None,
        trace_callback: TraceCallback | None = None,
    ) -> None:
        self._binding = binding or WchDllBinding(dll_path)
        self._scan_duration_ms = scan_duration_ms
        self._initial_scan_duration_ms = min(1000, scan_duration_ms)
        self._disconnected_callback = disconnected_callback
        self._trace_callback = trace_callback
        self._handle: Any | None = None
        self._mtu = 23
        self._ota_properties: tuple[str, ...] = ()
        self._notify_characteristic: int | None = None
        self._operation_lock = asyncio.Lock()
        self._scan_lock = asyncio.Lock()
        self._initialization_lock = asyncio.Lock()
        self._shutdown_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False
        self._empty_read_count = 0
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wch-ble")
        self._scan_active = False
        self._scan_task: asyncio.Task | None = None
        self._scan_callback: ScanCallback | None = None
        self._native_scan_active = False
        self._scan_event_loop: asyncio.AbstractEventLoop | None = None
        self._scan_names: dict[str, str] = {}
        self._connection_event_loop: asyncio.AbstractEventLoop | None = None
        self._connection_generation = 0
        self._pending_connection_state: tuple[int, bool] | None = None

    @property
    def is_connected(self) -> bool:
        return self._handle is not None

    @property
    def effective_mtu(self) -> int:
        return self._mtu

    @property
    def max_write_without_response_size(self) -> int:
        return self._mtu - 3

    @property
    def ota_characteristic_properties(self) -> tuple[str, ...]:
        return self._ota_properties

    @property
    def supports_write_with_response(self) -> bool:
        return "write" in self._ota_properties

    async def start_scan(self, callback: ScanCallback) -> None:
        async with self._scan_lock:
            await self._start_scan_locked(callback)

    async def _start_scan_locked(self, callback: ScanCallback) -> None:
        await self._ensure_initialized()
        self._scan_callback = callback
        if self._scan_active:
            return
        self._scan_active = True
        self._scan_event_loop = asyncio.get_running_loop()
        self._scan_names.clear()
        register_notify = getattr(self._binding, "register_advertising_notify", None)
        supports_live_scan = getattr(self._binding, "supports_live_scan", None)
        if supports_live_scan is None:
            supports_live_scan = callable(register_notify) and callable(
                getattr(self._binding, "open_device_address", None)
            )
        if supports_live_scan and callable(register_notify):
            # 实时广播结构没有 Windows Device ID，部分设备也不在广播包内携带
            # Local Name。先进行一次短枚举，用 Windows 缓存名称和 Device ID
            # 丰富列表，再切换到实时广播持续更新 RSSI 和新设备。
            try:
                await self._scan_once(
                    callback,
                    scan_duration_ms=self._initial_scan_duration_ms,
                )
            except Exception as error:
                self._trace(f"WCH DLL 初始名称解析失败，继续实时扫描：{error}")
            try:
                self._trace("WCH DLL 实时广播扫描开始")
                self._native_scan_active = bool(
                    await self._call_binding(
                        register_notify, self._native_advertising_received
                    )
                )
            except Exception as error:
                self._trace(f"WCH DLL 实时广播扫描不可用，回退兼容扫描：{error}")
                self._native_scan_active = False
            if self._native_scan_active:
                return
            self._trace("WCH DLL 不支持实时广播扫描，使用兼容扫描")
        try:
            await self._scan_once(
                callback,
                scan_duration_ms=self._initial_scan_duration_ms,
            )
        except Exception:
            self._scan_active = False
            raise
        if self._scan_active:
            self._scan_task = asyncio.create_task(self._scan_loop())

    def _native_advertising_received(self, record: WchScanRecord) -> None:
        loop = self._scan_event_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._dispatch_advertising_record, record)

    def _dispatch_advertising_record(self, record: WchScanRecord) -> None:
        if not self._scan_active or self.is_connected:
            return
        callback = self._scan_callback
        if callback is None:
            return
        if record.name:
            self._scan_names[record.address] = record.name
        name = record.name or self._scan_names.get(record.address, "")
        callback(
            WchDevice(name, record.address, record.device_id),
            WchAdvertisement(name or None, record.rssi),
        )

    async def _scan_loop(self) -> None:
        try:
            while self._scan_active and not self.is_connected:
                await asyncio.sleep(0.05)
                if not self._scan_active:
                    break
                callback = self._scan_callback
                if callback is not None:
                    try:
                        await self._scan_once(callback)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        self._trace(f"WCH DLL 持续扫描失败，将继续重试：{error}")
                        await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        finally:
            self._scan_active = False
            if asyncio.current_task() is self._scan_task:
                self._scan_task = None

    async def _scan_once(
        self,
        callback: ScanCallback,
        *,
        scan_duration_ms: int | None = None,
    ) -> None:
        duration_ms = scan_duration_ms or self._scan_duration_ms
        self._trace(f"WCH DLL 扫描开始：时长={duration_ms} ms")
        started = perf_counter()
        records = await self._call_binding(
            self._binding.enumerate_devices, duration_ms
        )
        self._trace(
            f"WCH DLL 扫描完成：设备数={len(records)}；"
            f"耗时={self._elapsed_ms(started):.1f} ms"
        )
        for record in records:
            if record.name:
                self._scan_names[record.address] = record.name
            callback(
                WchDevice(record.name, record.address, record.device_id),
                WchAdvertisement(record.name or None, record.rssi),
            )

    async def stop_scan(self) -> None:
        async with self._scan_lock:
            await self._stop_scan_locked()

    async def _stop_scan_locked(self) -> None:
        if self._native_scan_active:
            unregister_notify = getattr(
                self._binding, "unregister_advertising_notify", None
            )
            if callable(unregister_notify):
                success = bool(await self._call_binding(unregister_notify))
                if not success:
                    raise TransportError("注销实时广播扫描失败，可重试停止扫描")
            self._native_scan_active = False
            self._trace("WCH DLL 实时广播扫描已停止")
        self._scan_active = False
        self._scan_event_loop = None
        task = self._scan_task
        self._scan_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def connect(self, device: WchDevice) -> None:
        await self._ensure_initialized()
        async with self._operation_lock:
            if self.is_connected:
                raise TransportError("蓝牙设备已经连接，请先断开当前连接")
            self._trace(
                f"WCH DLL 连接开始：地址={device.address}；"
                f"名称={device.name or '未知设备'}；设备ID={device.device_id}"
            )
            started = perf_counter()
            self._connection_event_loop = asyncio.get_running_loop()
            def new_connection_callback() -> tuple[int, Any]:
                self._connection_generation += 1
                attempt_generation = self._connection_generation
                self._pending_connection_state = None
                callback = lambda _handle, connected: self._native_connection_changed(
                    attempt_generation, connected
                )
                return attempt_generation, callback

            try:
                handle, generation = await self._open_device_with_fallback(
                    device, new_connection_callback
                )
            except Exception as error:
                raise TransportError(f"WCH DLL 连接设备失败：{error}") from error
            if not handle:
                raise TransportError("WCH DLL 连接设备失败")
            self._handle = handle
            # 处理 WCHBLEOpenDevice 返回前已经送达的断开事件。
            await asyncio.sleep(0)
            if self._pending_connection_state == (generation, False):
                await self._call_binding(self._binding.close_device, handle)
                self._handle = None
                raise TransportError("连接建立期间设备已断开")
            self._pending_connection_state = None
            try:
                # 地址连接必须借助实时广播缓存；句柄一旦建立即可停止扫描，避免
                # 后续服务发现和 OTA 通信继续受到扫描活动影响。停止失败也属于
                # 连接初始化失败，必须关闭刚建立的句柄。
                if self._scan_active:
                    await self.stop_scan()
                self._trace(
                    f"WCH DLL 连接句柄已建立：句柄={_format_handle(handle)}；"
                    f"耗时={self._elapsed_ms(started):.1f} ms"
                )
                characteristic_started = perf_counter()
                has_ota = False
                discovery_error: Exception | None = None
                for attempt in range(1, self.gatt_discovery_attempts + 1):
                    if self._handle is not handle:
                        raise TransportError("连接初始化期间设备已断开")
                    try:
                        has_ota = bool(
                            await self._call_binding(
                                self._binding.has_ota_characteristic, handle
                            )
                        )
                        discovery_error = None
                    except Exception as error:
                        discovery_error = error
                    if has_ota:
                        break
                    if attempt < self.gatt_discovery_attempts:
                        await asyncio.sleep(self.gatt_discovery_retry_delay)
                if not has_ota:
                    if discovery_error is not None:
                        raise TransportError(
                            f"发现 OTA 特征 FEE1 失败：{discovery_error}"
                        ) from discovery_error
                    raise TransportError("设备缺少 OTA 特征 FEE1")
                self._trace(
                    "WCH DLL 特征检查：FEE0/FEE1=存在；"
                    f"耗时={self._elapsed_ms(characteristic_started):.1f} ms"
                )
                self._ota_properties = await self._call_binding(
                    self._binding.get_ota_characteristic_properties, handle
                )
                self._trace(
                    "WCH DLL FEE1 属性："
                    + (",".join(self._ota_properties) or "无")
                )
                characteristics = await self._call_binding(
                    self._binding.list_characteristics, handle
                )
                self._trace(
                    "WCH DLL FEE0 特征列表："
                    + ",".join(f"0x{uuid:04X}" for uuid in characteristics)
                )
                self._notify_characteristic = None
                # OTA 响应（擦除/编程/校验）以通知形式下发到 FEE1。WCH 官方工具与
                # Android 库均只订阅 OTA 特征（FEE1）。WCHBLEDLL 的读通知回调大概率
                # 只保留最后一次注册；若先订阅 FEE1 再订阅 FEE2，FEE2 会覆盖 FEE1
                # 导致收不到 FEE1 的响应。因此这里只订阅 FEE1（与 Android 一致）。
                # 订阅可能返回非 0（例如 DLL 未上报 notify 属性），此时回退到
                # GATT 特征值读取；记录状态码以确认回调是否真正生效。
                if 0xFEE1 in characteristics:
                    notify_result = await self._call_binding(
                        self._binding.register_read_notify, handle, 0xFEE1
                    )
                    if notify_result == 0:
                        self._notify_characteristic = 0xFEE1
                    self._trace(
                        f"WCH DLL 订阅 0xFEE1 通知："
                        f"status={notify_result}（0=成功）"
                    )
                    if self._notify_characteristic is None:
                        # 部分设备 OTA 特征以 indicate 上报响应，DLL 的 notify 订阅
                        # 会按属性位拒绝；改用 indicate 订阅作为回退。
                        indicate_result = await self._call_binding(
                            self._binding.register_read_indicate, handle, 0xFEE1
                        )
                        if indicate_result == 0:
                            self._notify_characteristic = 0xFEE1
                        self._trace(
                            f"WCH DLL 订阅 0xFEE1 指示："
                            f"status={indicate_result}（0=成功）"
                        )
                mtu_started = perf_counter()
                self._mtu = await self._call_binding(self._binding.get_mtu, handle)
                self._trace(
                    f"WCH DLL 连接就绪：MTU={self._mtu}；"
                    f"无响应写入上限={self.max_write_without_response_size}；"
                    f"读取MTU耗时={self._elapsed_ms(mtu_started):.1f} ms"
                )
                if self._handle is not handle:
                    raise TransportError("连接初始化期间设备已断开")
            except Exception:
                await self._call_binding(self._binding.close_device, handle)
                self._handle = None
                self._mtu = 23
                self._ota_properties = ()
                self._notify_characteristic = None
                raise

    async def _open_device_with_fallback(
        self, device: WchDevice, connection_callback_factory: Any
    ) -> tuple[Any, int]:
        address = _normalize_mac_address(device.address)
        errors: list[str] = []
        if device.device_id:
            try:
                handle, generation = await self._open_connection_attempt(
                    self._binding.open_device,
                    (device.device_id,),
                    connection_callback_factory,
                    "WCHBLEOpenDevice 返回空句柄",
                )
                return handle, generation
            except Exception as device_id_error:
                errors.append(f"Device ID 连接失败：{device_id_error}")
                self._trace(
                    "WCH DLL Device ID 连接失败，准备兼容恢复："
                    f"{device_id_error}"
                )

        open_by_address = getattr(self._binding, "open_device_address", None)
        # WCHBLEOpenDeviceAddress 依赖实时广播建立的内部缓存。仅在实时扫描仍
        # 活跃时尝试，避免兼容扫描结果直接按地址打开并稳定返回空句柄。
        if callable(open_by_address) and (
            not device.device_id or self._native_scan_active
        ):
            try:
                handle, generation = await self._open_connection_attempt(
                    open_by_address,
                    (address,),
                    connection_callback_factory,
                    "WCHBLEOpenDeviceAddress 返回空句柄",
                )
                if device.device_id:
                    self._trace("WCH DLL 已从 Device ID 连接回退为 MAC 地址连接")
                return handle, generation
            except Exception as address_error:
                errors.append(f"MAC 地址连接失败：{address_error}")

        # 地址连接不可用或失败时，结束扫描并重新枚举，取得当前电脑、当前
        # 蓝牙适配器生成的最新 Windows Device ID 后再尝试一次。
        if self._scan_active or self._native_scan_active:
            try:
                await self.stop_scan()
            except Exception as stop_error:
                errors.append(f"停止扫描失败：{stop_error}")
                raise TransportError("；".join(errors)) from stop_error
        resolved = None
        for attempt in range(1, self.device_id_resolution_attempts + 1):
            records = await self._call_binding(
                self._binding.enumerate_devices, self._scan_duration_ms
            )
            resolved = next(
                (
                    record
                    for record in records
                    if _normalize_mac_address(record.address) == address
                    and record.device_id
                ),
                None,
            )
            if resolved is not None:
                break
            self._trace(
                "WCH DLL 兼容扫描暂未解析目标 Device ID："
                f"第 {attempt}/{self.device_id_resolution_attempts} 次"
            )
        if resolved is None or not resolved.device_id:
            errors.append("兼容扫描未找到设备的最新 Device ID")
            raise TransportError("；".join(errors))
        try:
            self._trace(
                f"WCH DLL 使用重新扫描取得的 Device ID 重试：{resolved.device_id}"
            )
            handle, generation = await self._open_connection_attempt(
                self._binding.open_device,
                (resolved.device_id,),
                connection_callback_factory,
                "WCHBLEOpenDevice 返回空句柄",
            )
            return handle, generation
        except Exception as refreshed_id_error:
            errors.append(f"最新 Device ID 连接失败：{refreshed_id_error}")
            raise TransportError("；".join(errors)) from refreshed_id_error

    async def _open_connection_attempt(
        self,
        opener: Any,
        arguments: tuple[Any, ...],
        connection_callback_factory: Any,
        empty_handle_message: str,
    ) -> tuple[Any, int]:
        generation, connection_callback = connection_callback_factory()
        handle = await self._call_binding(opener, *arguments, connection_callback)
        if not handle:
            raise TransportError(empty_handle_message)
        # 原生回调可能在 DLL 打开函数返回前到达；让事件循环先处理它，早期
        # 断开视为本次尝试失败，以便继续 MAC 或最新 Device ID 回退。
        await asyncio.sleep(0)
        if self._pending_connection_state == (generation, False):
            await self._call_binding(self._binding.close_device, handle)
            self._pending_connection_state = None
            raise TransportError("连接建立期间设备已断开")
        return handle, generation

    async def write_ota(self, payload: bytes, *, response: bool = False) -> None:
        handle = self._require_handle()
        limit = 512 if response else self.max_write_without_response_size
        if len(payload) > limit:
            mode = "有响应" if response else "无响应"
            raise TransportError(f"OTA 数据长度 {len(payload)} 超过{mode}写入上限 {limit}")
        self._flush_empty_reads("下一次写入前")
        mode = "有响应" if response else "无响应"
        started = perf_counter()
        try:
            await self._call_binding(
                self._binding.write_ota, handle, bytes(payload), response
            )
        except Exception as error:
            self._trace(
                f"WCH DLL TX：模式={mode}；长度={len(payload)}；"
                f"耗时={self._elapsed_ms(started):.1f} ms；状态=失败；"
                f"数据={self._hex(payload)}；错误={error}"
            )
            raise TransportError(f"WCH DLL 写入 OTA 特征失败：{error}") from error
        self._trace(
            f"WCH DLL TX：模式={mode}；长度={len(payload)}；"
            f"耗时={self._elapsed_ms(started):.1f} ms；状态=成功；"
            f"数据={self._hex(payload)}"
        )

    async def read_ota(self, use_cached: bool = False) -> bytes:
        del use_cached
        handle = self._require_handle()
        # OTA 响应以通知形式下发：优先从通知缓冲取出（FEE1 优先，其次 FEE2）。
        for uuid in (0xFEE1, self._notify_characteristic):
            if uuid is None:
                continue
            try:
                notify_payload = await self._call_binding(
                    self._binding.read_notify, handle, uuid
                )
            except TransportError:
                notify_payload = b""
            if notify_payload:
                self._flush_empty_reads("收到通知数据")
                self._trace(
                    f"WCH DLL RX：来源=通知 0x{uuid:04X}；"
                    f"长度={len(notify_payload)}；数据={self._hex(notify_payload)}"
                )
                return notify_payload
        # 回退：部分命令（如 INFO）以可读值回送，直接读取特征值。
        # 优先 FEE1，其次 FEE2（某些设备把响应放在 FEE2 可读值上）。
        for fallback_uuid in (0xFEE1, 0xFEE2):
            payload = await self._read_characteristic(
                handle, fallback_uuid, count_empty=True
            )
            if payload:
                return payload
        return b""

    async def _read_characteristic(
        self, handle: Any, characteristic_uuid: int, *, count_empty: bool
    ) -> bytes:
        started = perf_counter()
        try:
            payload = bytes(
                await self._call_binding(
                    self._binding.read_characteristic, handle, characteristic_uuid
                )
            )
        except Exception as error:
            self._trace(
                f"WCH DLL RX：UUID={characteristic_uuid:04X}；"
                f"耗时={self._elapsed_ms(started):.1f} ms；状态=失败；错误={error}"
            )
            raise TransportError(f"WCH DLL 读取 OTA 特征失败：{error}") from error
        elapsed = self._elapsed_ms(started)
        if payload:
            self._flush_empty_reads("收到数据")
            self._trace(
                f"WCH DLL RX：UUID={characteristic_uuid:04X}；长度={len(payload)}；"
                f"耗时={elapsed:.1f} ms；数据={self._hex(payload)}"
            )
            return payload
        if count_empty:
            self._empty_read_count += 1
            if self._empty_read_count in {1, 10, 25, 50}:
                self._trace(
                    f"WCH DLL RX：空响应累计={self._empty_read_count}；"
                    f"本次耗时={elapsed:.1f} ms"
                )
        return payload

    async def disconnect(self) -> None:
        async with self._operation_lock:
            handle = self._handle
            if handle is None:
                return
            self._connection_generation += 1
            self._pending_connection_state = None
            self._trace(f"WCH DLL 主动断开开始：句柄={_format_handle(handle)}")
            self._handle = None
            self._mtu = 23
            self._ota_properties = ()
            self._notify_characteristic = None
            self._flush_empty_reads("断开前")
            for uuid in (0xFEE1, 0xFEE2):
                try:
                    await self._call_binding(
                        self._binding.unregister_read_notify, handle, uuid
                    )
                except Exception:
                    pass
            started = perf_counter()
            try:
                await self._call_binding(self._binding.close_device, handle)
            except Exception as error:
                raise TransportError(f"WCH DLL 断开设备失败：{error}") from error
            self._trace(f"WCH DLL 主动断开完成：耗时={self._elapsed_ms(started):.1f} ms")

    async def shutdown(self) -> None:
        """停止后台活动并释放本后端拥有的执行器。"""
        async with self._shutdown_lock:
            if self._closed:
                return
            errors: list[str] = []
            try:
                await self.stop_scan()
            except Exception as error:
                errors.append(f"停止扫描失败：{error}")
            try:
                await self.disconnect()
            except Exception as error:
                errors.append(f"断开设备失败：{error}")
            self._closed = True
            self._executor.shutdown(wait=False, cancel_futures=True)
            if errors:
                raise TransportError("；".join(errors))

    def _require_handle(self) -> Any:
        if self._handle is None:
            raise TransportError("蓝牙设备尚未连接")
        return self._handle

    def _native_connection_changed(self, generation: int, connected: bool) -> None:
        self._trace(f"WCH DLL 原生连接事件：状态={'已连接' if connected else '已断开'}")
        loop = self._connection_event_loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(
            self._apply_native_connection_changed, generation, connected
        )

    def _apply_native_connection_changed(
        self, generation: int, connected: bool
    ) -> None:
        if generation != self._connection_generation:
            return
        if self._handle is None:
            self._pending_connection_state = (generation, connected)
            return
        if connected:
            return
        self._handle = None
        self._mtu = 23
        self._ota_properties = ()
        self._notify_characteristic = None
        if self._disconnected_callback is not None:
            self._disconnected_callback(self)

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialization_lock:
            if self._initialized:
                return
            try:
                self._trace("WCH DLL 初始化开始")
                started = perf_counter()
                await self._call_binding(self._binding.initialize)
            except Exception as error:
                if isinstance(error, TransportError):
                    raise
                raise TransportError(f"初始化 WCH BLE 库失败：{error}") from error
            self._initialized = True
            self._trace(f"WCH DLL 初始化完成：耗时={self._elapsed_ms(started):.1f} ms")

    async def _call_binding(self, function: Any, *args: Any) -> Any:
        if self._closed:
            raise TransportError("WCH BLE 后端已经关闭")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, function, *args)

    def _trace(self, message: str) -> None:
        if self._trace_callback is None:
            return
        try:
            self._trace_callback(message)
        except Exception:
            # 诊断观察者不得影响蓝牙协议流程。
            return

    def _flush_empty_reads(self, reason: str) -> None:
        count = self._empty_read_count
        if count and count not in {1, 10, 25, 50}:
            self._trace(f"WCH DLL RX：空响应结束；累计={count}；原因={reason}")
        self._empty_read_count = 0

    @staticmethod
    def _elapsed_ms(started: float) -> float:
        return (perf_counter() - started) * 1000

    @staticmethod
    def _hex(payload: bytes) -> str:
        return " ".join(f"{value:02X}" for value in payload) or "空"
