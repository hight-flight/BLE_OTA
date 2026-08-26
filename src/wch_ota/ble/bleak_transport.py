"""Bleak-backed implementation of the BLE OTA transport."""

import asyncio
from collections.abc import Callable
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from wch_ota.ble.transport import (
    DisconnectCallback,
    FEE0_UUID,
    FEE1_UUID,
    ScanCallback,
    TransportError,
)


class BleakTransport:
    """Owns the sustained scan and GATT connection used for OTA traffic."""

    backend_name = "Bleak/WinRT"
    scan_is_continuous = True

    def __init__(
        self,
        *,
        scanner_factory: Callable[..., Any] = BleakScanner,
        client_factory: Callable[..., Any] = BleakClient,
        disconnected_callback: DisconnectCallback | None = None,
    ) -> None:
        self._scanner_factory = scanner_factory
        self._client_factory = client_factory
        self._disconnected_callback = disconnected_callback
        self._scanner: Any | None = None
        self._client: Any | None = None
        self._ota_characteristic: Any | None = None
        self._scan_lock = asyncio.Lock()
        self._connection_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and bool(self._client.is_connected)

    @property
    def max_write_without_response_size(self) -> int:
        characteristic = self._require_characteristic()
        return int(characteristic.max_write_without_response_size)

    @property
    def effective_mtu(self) -> int:
        return self.max_write_without_response_size + 3

    @property
    def ota_characteristic_properties(self) -> tuple[str, ...]:
        characteristic = self._require_characteristic()
        return tuple(str(item).lower() for item in characteristic.properties)

    @property
    def supports_write_with_response(self) -> bool:
        return "write" in self.ota_characteristic_properties

    async def start_scan(self, callback: ScanCallback) -> None:
        async with self._scan_lock:
            if self._scanner is not None:
                await self._stop_scan_locked()
            try:
                scanner = self._scanner_factory(detection_callback=callback)
                await scanner.start()
            except BleakError as exc:
                raise TransportError(f"启动蓝牙扫描失败：{exc}") from exc
            self._scanner = scanner

    async def stop_scan(self) -> None:
        async with self._scan_lock:
            await self._stop_scan_locked()

    async def _stop_scan_locked(self) -> None:
        scanner = self._scanner
        if scanner is None:
            return
        try:
            await scanner.stop()
        except BleakError as exc:
            raise TransportError(f"停止蓝牙扫描失败：{exc}") from exc
        if scanner is self._scanner:
            self._scanner = None

    async def connect(self, device: Any) -> None:
        async with self._connection_lock:
            if self.is_connected:
                raise TransportError("蓝牙设备已经连接，请先断开当前连接")
            await self._connect_locked(device)

    async def _connect_locked(self, device: Any) -> None:
        client: Any | None = None
        try:
            client_options: dict[str, Any] = {
                "winrt": {"use_cached_services": False}
            }
            address_type = self._get_winrt_address_type(device)
            if address_type is not None:
                client_options["winrt"]["address_type"] = address_type
            client = self._client_factory(
                device,
                disconnected_callback=self._handle_disconnected,
                **client_options,
            )
            self._client = client
            await client.connect()
        except BleakError as exc:
            cleanup_error = await self._clean_failed_client(client)
            if cleanup_error is not None:
                raise TransportError(
                    f"连接蓝牙设备失败：{exc}；断开失败：{cleanup_error}"
                ) from cleanup_error
            raise TransportError(f"连接蓝牙设备失败：{exc}") from exc

        try:
            service = client.services.get_service(FEE0_UUID)
            if service is None:
                await self._reject_connection_locked("设备缺少 OTA 服务 FEE0")
            characteristic = service.get_characteristic(FEE1_UUID)
            if characteristic is None:
                await self._reject_connection_locked("设备缺少 OTA 特征 FEE1")
        except BleakError as exc:
            cleanup_error = await self._clean_failed_client(client)
            if cleanup_error is not None:
                raise TransportError(
                    f"验证 OTA 服务失败：{exc}；断开失败：{cleanup_error}"
                ) from cleanup_error
            raise TransportError(f"验证 OTA 服务失败：{exc}") from exc
        self._ota_characteristic = characteristic

    @staticmethod
    def _get_winrt_address_type(device: Any) -> str | None:
        """从 Windows 扫描结果中保留 BLE 公有/随机地址类型。"""
        details = getattr(device, "details", None)
        event_args = getattr(details, "adv", None) or getattr(details, "scan", None)
        native_type = getattr(event_args, "bluetooth_address_type", None)
        if native_type is None:
            return None

        name = getattr(native_type, "name", "").lower()
        if name in {"public", "random"}:
            return name
        try:
            value = int(native_type)
        except (TypeError, ValueError):
            return None
        return {0: "public", 1: "random"}.get(value)

    async def read_ota(self, use_cached: bool = False) -> bytes:
        characteristic = self._require_characteristic()
        client = self._require_client()
        try:
            value = await client.read_gatt_char(characteristic, use_cached=use_cached)
        except BleakError as exc:
            raise TransportError(f"读取 OTA 特征失败：{exc}") from exc
        return bytes(value)

    async def discard_ota_responses(self) -> None:
        """Bleak 通过非缓存 GATT 读取获取状态，没有本地通知队列可清理。"""
        return

    async def write_ota(self, payload: bytes, *, response: bool = False) -> None:
        characteristic = self._require_characteristic()
        client = self._require_client()
        if response and "write" not in self.ota_characteristic_properties:
            raise TransportError("OTA 特征不支持有响应写入")
        limit = 512 if response else int(characteristic.max_write_without_response_size)
        if len(payload) > limit:
            mode = "有响应" if response else "无响应"
            raise TransportError(f"OTA 数据长度 {len(payload)} 超过{mode}写入上限 {limit}")
        try:
            await client.write_gatt_char(characteristic, payload, response=response)
        except BleakError as exc:
            raise TransportError(f"写入 OTA 特征失败：{exc}") from exc

    async def disconnect(self) -> None:
        async with self._connection_lock:
            await self._disconnect_locked()

    async def _disconnect_locked(self) -> None:
        client = self._client
        if client is None:
            return
        try:
            if client.is_connected:
                await client.disconnect()
        except BleakError as exc:
            raise TransportError(f"断开蓝牙设备失败：{exc}") from exc
        if client is self._client:
            self._client = None
            self._ota_characteristic = None

    async def _reject_connection_locked(self, message: str) -> None:
        await self._disconnect_locked()
        raise TransportError(message)

    async def _clean_failed_client(self, client: Any | None) -> BleakError | None:
        if client is None:
            return None
        try:
            if client.is_connected:
                await client.disconnect()
        except BleakError as exc:
            self._ota_characteristic = None
            return exc
        if client is self._client:
            self._client = None
            self._ota_characteristic = None
        return None

    def _require_client(self) -> Any:
        if not self.is_connected:
            raise TransportError("蓝牙设备尚未连接")
        return self._client

    def _require_characteristic(self) -> Any:
        if self._ota_characteristic is None or not self.is_connected:
            raise TransportError("OTA 特征尚未就绪")
        return self._ota_characteristic

    def _handle_disconnected(self, client: Any) -> None:
        if client is not self._client:
            return
        self._client = None
        self._ota_characteristic = None
        if self._disconnected_callback is not None:
            self._disconnected_callback(client)
