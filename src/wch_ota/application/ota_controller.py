"""驱动 WCH OTA 协议的可取消应用层状态机。"""

import asyncio
from collections.abc import Awaitable, Callable

from wch_ota.ble.transport import Transport, TransportError
from wch_ota.domain.firmware import FirmwareImage
from wch_ota.domain.models import ChipType, CurrentImageInfo, ImageType
from wch_ota.domain.protocol import (
    build_compact_erase_command,
    build_end_command,
    build_erase_command,
    build_info_command,
    build_program_command,
    build_verify_command,
    is_erase_success,
    is_verify_success,
    parse_image_info_response,
)

from .ota_events import OtaEvent, UpgradeStage, UpgradeStatus

EventCallback = Callable[[OtaEvent], None]
Sleep = Callable[[float], Awaitable[None]]

_SUPPORTED_CHIPS = frozenset(
    {
        ChipType.CH583,
        ChipType.CH32V208,
        ChipType.CH32F208,
        ChipType.CH579,
        ChipType.CH573,
        ChipType.CH592,
    }
)
_READ_NULL_ATTEMPTS = 51
_READ_NULL_RETRY_DELAY = 0.2
_INFO_PROBE_ATTEMPTS = 6
_ERASE_SETTLE_DELAY = 1.5
_ERASE_READ_ATTEMPTS = 11
_ERASE_READ_RETRY_DELAY = 0.5


class OtaError(RuntimeError):
    """可展示给用户的 OTA 状态机错误。"""


class OtaController:
    """串行执行擦除、编程、校验和结束命令。"""

    def __init__(
        self,
        transport: Transport,
        event_callback: EventCallback | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._transport = transport
        self._event_callback = event_callback
        self._sleep = sleep
        self._cancel_requested = asyncio.Event()
        self._operation_lock = asyncio.Lock()
        self._running = False
        self._stage = UpgradeStage.ERASE
        self._progress = 0
        self._total = 0
        self.last_observer_error: Exception | None = None

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        """请求在发送下一条协议命令前停止升级。"""
        self._cancel_requested.set()

    async def get_current_image_info(self) -> CurrentImageInfo:
        """读取并解析当前镜像信息。"""
        await self._acquire_operation()
        try:
            if not self._transport.is_connected:
                raise OtaError("蓝牙设备尚未连接")
            try:
                await self._transport.write_ota(build_info_command())
                response = await self._read_nonempty_response()
            except TransportError as error:
                raise OtaError(str(error)) from error
            info = parse_image_info_response(response)
            if info is None:
                raise OtaError("无法解析当前镜像信息")
            return info
        finally:
            self._operation_lock.release()

    async def upgrade(
        self, firmware: FirmwareImage, info: CurrentImageInfo
    ) -> None:
        """执行一次升级；取消正常返回，失败抛出 :class:`OtaError`。"""
        await self._acquire_operation()
        self._running = True
        self._stage = UpgradeStage.ERASE
        self._progress = 0
        self._total = len(firmware.data)
        try:
            start_address = self._target_address(firmware, info)
            block_count = self._preflight(firmware, info, start_address)
            if await self._erase(firmware, info, start_address, block_count):
                return
            if await self._transfer(
                UpgradeStage.PROGRAM, firmware, info, start_address
            ):
                return
            if await self._transfer(
                UpgradeStage.VERIFY, firmware, info, start_address
            ):
                return
            if await self._end():
                return
        except OtaError as error:
            self._emit(UpgradeStatus.FAILED, message=str(error))
            raise
        except TransportError as error:
            self._emit(UpgradeStatus.FAILED, message=str(error))
            raise OtaError(str(error)) from error
        except Exception as error:
            self._emit(UpgradeStatus.FAILED, message=str(error))
            raise OtaError(str(error)) from error
        finally:
            self._running = False
            self._cancel_requested.clear()
            self._operation_lock.release()

    async def _acquire_operation(self) -> None:
        if self._operation_lock.locked():
            raise OtaError("另一个 OTA 操作正在进行中")
        await self._operation_lock.acquire()

    def _preflight(
        self,
        firmware: FirmwareImage,
        info: CurrentImageInfo,
        start_address: int,
    ) -> int:
        if info.chip not in _SUPPORTED_CHIPS:
            raise OtaError("设备报告了不支持的芯片类型")

        address_base = 4 if info.chip is ChipType.CH579 else 16
        if type(start_address) is not int or start_address < 0:
            raise OtaError("固件起始地址必须是非负整数")
        if start_address % address_base:
            raise OtaError(f"固件起始地址必须按 {address_base} 字节对齐")
        if not firmware.data:
            raise OtaError("固件数据不能为空")
        if (
            info.chip is not ChipType.CH579
            and info.image in {ImageType.A, ImageType.B}
            and info.offset > 0
            and len(firmware.data) > info.offset
        ):
            raise OtaError(
                f"固件大小 {len(firmware.data)} 字节超过目标 Image 最大大小 "
                f"{info.offset} 字节"
            )

        end_address = start_address + len(firmware.data) - 1
        maximum_address = 0xFFFF * address_base + (address_base - 1)
        if end_address > maximum_address:
            raise OtaError("固件地址范围超出芯片协议上限")

        block_count = self._block_count(len(firmware.data), info.block_size)
        if start_address % info.block_size:
            raise OtaError(
                f"固件起始地址必须按擦除块大小 {info.block_size} 字节对齐"
            )
        try:
            build_program_command(
                start_address,
                firmware.data,
                0,
                self._transport.effective_mtu,
                info.chip,
            )
        except ValueError as error:
            raise OtaError(f"固件传输参数无效：{error}") from error
        return block_count

    @staticmethod
    def _target_address(firmware: FirmwareImage, info: CurrentImageInfo) -> int:
        if info.image not in {ImageType.A, ImageType.B, ImageType.IAP}:
            raise OtaError("设备当前 Image 类型无效")
        if info.image is ImageType.IAP and info.chip is ChipType.CH579:
            raise OtaError("CH579 不支持 Image IAP 升级")
        if info.chip is not ChipType.CH579:
            return firmware.start_address
        if info.image is ImageType.A:
            return info.offset
        if info.image is ImageType.B:
            return 0
        raise OtaError("CH579 当前 Image 类型无效")

    @staticmethod
    def _block_count(data_length: int, block_size: int) -> int:
        if block_size <= 0:
            raise OtaError("擦除块大小必须大于 0")
        block_count = (data_length + block_size - 1) // block_size
        if block_count > 0xFFFF:
            raise OtaError("擦除块数量超过 65535")
        return block_count

    async def _erase(
        self,
        firmware: FirmwareImage,
        info: CurrentImageInfo,
        start_address: int,
        block_count: int,
    ) -> bool:
        self._start_stage(UpgradeStage.ERASE)
        if self._stop_if_cancelled():
            return True
        # 擦除前刷新一次 INFO 通道，与 Android 先读取镜像信息再进入升级的顺序一致。
        await self._refresh_info_before_erase()
        android_command = build_erase_command(
            start_address,
            block_count,
            info.chip,
            target_image=info.image,
        )
        compact_command = (
            None
            if info.image is ImageType.IAP
            else build_compact_erase_command(start_address, block_count, info.chip)
        )
        prefer_compact = bool(
            getattr(self._transport, "prefer_compact_erase", False)
        ) and compact_command is not None
        command = compact_command if prefer_compact else android_command
        fallback_command = android_command if prefer_compact else compact_command
        fallback_label = "Android 备用帧" if prefer_compact else "兼容帧"
        # Android Demo 对全部 OTA 命令固定使用 WRITE_TYPE_NO_RESPONSE。
        # Windows 端也保持相同 ATT 写入类型，避免设备固件区分 Write Request/Command。
        erase_write_with_response = False
        response = await self._send_erase_and_read(command)
        compatibility = ""
        probe = ""
        if not response:
            probe_message, probe_valid, _ = await self._probe_info_channel()
            probe = f"；{probe_message}"
            if probe_valid:
                if self._transport.supports_write_with_response:
                    response_retry = await self._send_erase_and_read(
                        command, response=True
                    )
                    response_retry_rx = (
                        response_retry.hex(" ").upper() or "空"
                    )
                    compatibility += (
                        f"；有响应重试TX={command.hex(' ').upper()}；"
                        f"有响应重试RX={response_retry_rx}"
                    )
                    if is_erase_success(response_retry):
                        self._emit(
                            UpgradeStatus.RUNNING,
                            message="无响应擦除未返回状态，已改用有响应写入擦除成功",
                        )
                        return False
                if fallback_command is not None:
                    fallback_response = await self._send_erase_and_read(fallback_command)
                    fallback_rx = fallback_response.hex(" ").upper() or "空"
                    compatibility = (
                        f"；{fallback_label}TX={fallback_command.hex(' ').upper()}；"
                        f"{fallback_label}RX={fallback_rx}"
                    )
                    if is_erase_success(fallback_response):
                        fallback_description = (
                            "Android 20 字节备用帧"
                            if prefer_compact
                            else "6 字节兼容帧"
                        )
                        self._emit(
                            UpgradeStatus.RUNNING,
                            message=f"主擦除帧无响应，已使用{fallback_description}擦除成功",
                        )
                        return False
        if not is_erase_success(response):
            rx = bytes(response or b"").hex(" ").upper() or "空"
            status = f"状态 0x{response[0]:02X}" if response else "无有效状态"
            write_mode = "有响应" if erase_write_with_response else "无响应"
            properties = ",".join(self._transport.ota_characteristic_properties)
            raise OtaError(
                f"擦除失败：设备{status}；芯片 {info.chip.value}；"
                f"Image {info.image.value}；地址 0x{start_address:08X}；"
                f"块数 {block_count}；写入={write_mode}；"
                f"MTU={self._transport.effective_mtu}；"
                f"FEE1={properties or '未知'}；读取={self._erase_read_attempts()}次；"
                f"TX={command.hex(' ').upper()}；RX={rx}{probe}{compatibility}"
            )
        return False

    async def _send_erase_and_read(
        self, command: bytes, *, response: bool = False
    ) -> bytes:
        await self._transport.write_ota(command, response=response)
        # Android 在 sleep(1000) 后经 GATT 调度约 1.03 秒才读到结果。
        # Windows 的定时更贴近整秒，首读过早可能读空并干扰设备擦除任务，
        # 因此留出额外余量，并降低后续轮询频率。
        settle_delay = float(
            getattr(self._transport, "erase_settle_delay", _ERASE_SETTLE_DELAY)
        )
        if settle_delay > 0:
            await self._sleep(settle_delay)
        return await self._read_nonempty_response(
            attempts=self._erase_read_attempts(),
            retry_delay=float(
                getattr(
                    self._transport,
                    "erase_read_retry_delay",
                    _ERASE_READ_RETRY_DELAY,
                )
            ),
        )

    def _erase_read_attempts(self) -> int:
        return int(
            getattr(self._transport, "erase_read_attempts", _ERASE_READ_ATTEMPTS)
        )

    async def _transfer(
        self,
        stage: UpgradeStage,
        firmware: FirmwareImage,
        info: CurrentImageInfo,
        start_address: int,
    ) -> bool:
        self._start_stage(stage)
        builder = (
            build_program_command
            if stage is UpgradeStage.PROGRAM
            else build_verify_command
        )
        while self._progress < self._total:
            if self._stop_if_cancelled():
                return True
            command = builder(
                start_address + self._progress,
                firmware.data,
                self._progress,
                self._transport.effective_mtu,
                info.chip,
            )
            await self._transport.write_ota(command)
            actual_payload = min(command[1], self._total - self._progress)
            self._progress += actual_payload
            self._emit(UpgradeStatus.RUNNING)

        if stage is UpgradeStage.VERIFY:
            await self._sleep(1)
            response = await self._read_nonempty_response()
            if not is_verify_success(response):
                probe_message, _, _ = await self._probe_info_channel()
                raise OtaError(f"校验失败：设备返回错误状态；{probe_message}")
        return False

    async def _read_nonempty_response(
        self,
        *,
        attempts: int = _READ_NULL_ATTEMPTS,
        retry_delay: float = _READ_NULL_RETRY_DELAY,
    ) -> bytes:
        """轮询空 GATT 值，为耗时较长的擦除保留约 10 秒响应窗口。"""
        for attempt in range(attempts):
            response = await self._transport.read_ota()
            if response:
                return bytes(response)
            if attempt + 1 < attempts:
                await self._sleep(retry_delay)
        return b""

    async def _probe_info_channel(self) -> tuple[str, bool, bytes]:
        command = build_info_command()
        try:
            await self._transport.write_ota(command, response=False)
            await self._sleep(_READ_NULL_RETRY_DELAY)
            response = await self._read_nonempty_response(attempts=_INFO_PROBE_ATTEMPTS)
        except TransportError as error:
            return f"INFO 探测失败：{error}", False, b""

        rx = response.hex(" ").upper() or "空"
        info = parse_image_info_response(response)
        if info is None:
            return (
                f"INFO 探测无效；探测TX={command.hex(' ').upper()}；探测RX={rx}",
                False,
                b"",
            )
        return (
            (
                f"INFO 探测有效：芯片 {info.chip.value}，Image {info.image.value}，"
                f"块大小 {info.block_size}；探测TX={command.hex(' ').upper()}；探测RX={rx}"
            ),
            True,
            bytes(response),
        )

    async def _refresh_info_before_erase(self) -> bytes:
        """擦除前刷新 INFO 通道；失败时交由后续擦除 ACK 判定处理。"""
        try:
            await self._transport.write_ota(build_info_command(), response=False)
            await self._sleep(_READ_NULL_RETRY_DELAY)
            response = bytes(await self._transport.read_ota())
        except TransportError:
            return b""
        return response if parse_image_info_response(response) is not None else b""

    async def _end(self) -> bool:
        self._start_stage(UpgradeStage.END)
        if self._stop_if_cancelled():
            return True
        await self._transport.write_ota(build_end_command())
        self._emit(UpgradeStatus.SUCCESS, message="升级完成")
        return False

    def _start_stage(self, stage: UpgradeStage) -> None:
        self._stage = stage
        self._progress = 0
        self._emit(UpgradeStatus.RUNNING)

    def _stop_if_cancelled(self) -> bool:
        if not self._cancel_requested.is_set():
            return False
        self._emit(UpgradeStatus.CANCELLED, message="升级已取消")
        return True

    def _emit(self, status: UpgradeStatus, *, message: str = "") -> None:
        if self._event_callback is None:
            return
        try:
            self._event_callback(
                OtaEvent(
                    stage=self._stage,
                    status=status,
                    progress=self._progress,
                    total=self._total,
                    message=message,
                )
            )
        except Exception as error:
            self.last_observer_error = error
