"""可取消 OTA 状态机的应用层契约。"""

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from wch_ota.application.ota_controller import OtaController, OtaError
from wch_ota.application.ota_events import UpgradeStage, UpgradeStatus
from wch_ota.ble.transport import TransportError
from wch_ota.domain.firmware import FirmwareImage
from wch_ota.domain.models import ChipType, CurrentImageInfo, ImageType
from wch_ota.domain.protocol import (
    build_compact_erase_command,
    build_end_command,
    build_erase_command,
    build_info_command,
    build_program_command,
    build_verify_command,
)


async def no_sleep(_delay: float) -> None:
    return None


class FakeTransport:
    def __init__(
        self,
        *,
        responses: list[bytes] | None = None,
        connected: bool = True,
        mtu: int = 23,
        supports_write_with_response: bool = False,
    ) -> None:
        self.is_connected = connected
        self.effective_mtu = mtu
        self.max_write_without_response_size = mtu - 3
        self.supports_write_with_response = supports_write_with_response
        self.ota_characteristic_properties = (
            ("read", "write", "write-without-response")
            if supports_write_with_response
            else ("read", "write-without-response")
        )
        self.responses = list(responses or [])
        self.writes: list[bytes] = []
        self.write_responses: list[bool] = []
        self.read_calls = 0
        self.write_hook: Callable[[bytes], Awaitable[None]] | None = None
        self.write_error: Exception | None = None

    async def write_ota(self, payload: bytes, *, response: bool = False) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append(payload)
        self.write_responses.append(response)
        if self.write_hook is not None:
            await self.write_hook(payload)

    async def read_ota(self, use_cached: bool = False) -> bytes:
        del use_cached
        self.read_calls += 1
        return self.responses.pop(0) if self.responses else b""


def image_info(*, block_size: int = 16) -> CurrentImageInfo:
    return CurrentImageInfo(ChipType.CH583, ImageType.A, 0, block_size)


def info_response(*, block_size: int = 16) -> bytes:
    response = bytearray(20)
    response[0] = 1
    response[5:7] = block_size.to_bytes(2, "little")
    response[7:9] = b"\x83\x00"
    return bytes(response)


@pytest.mark.asyncio
async def test_get_current_image_info_writes_info_and_parses_response() -> None:
    transport = FakeTransport(responses=[info_response(block_size=32)])
    controller = OtaController(transport, sleep=no_sleep)

    result = await controller.get_current_image_info()

    assert result == image_info(block_size=32)
    assert transport.writes == [build_info_command()]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "message"),
    [
        (FakeTransport(connected=False), "连接"),
        (FakeTransport(responses=[b"invalid"]), "信息"),
    ],
)
async def test_get_current_image_info_rejects_disconnected_or_invalid_response(
    transport: FakeTransport, message: str
) -> None:
    with pytest.raises(OtaError, match=message):
        await OtaController(transport, sleep=no_sleep).get_current_image_info()


@pytest.mark.asyncio
async def test_upgrade_sends_exact_commands_and_emits_ordered_successful_stages() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"], mtu=23)
    events = []
    firmware = FirmwareImage(0, bytes(range(20)))
    controller = OtaController(transport, events.append, sleep=no_sleep)

    await controller.upgrade(firmware, image_info(block_size=16))

    assert transport.writes == [
        build_info_command(),
        build_erase_command(0, 2, ChipType.CH583, target_image=ImageType.A),
        build_program_command(0, firmware.data, 0, 23, ChipType.CH583),
        build_program_command(16, firmware.data, 16, 23, ChipType.CH583),
        build_verify_command(0, firmware.data, 0, 23, ChipType.CH583),
        build_verify_command(16, firmware.data, 16, 23, ChipType.CH583),
        build_end_command(),
    ]
    assert [packet[2:4] for packet in transport.writes if packet[0] == 0x80] == [
        b"\x00\x00",
        b"\x01\x00",
    ]
    assert [packet[2:4] for packet in transport.writes if packet[0] == 0x82] == [
        b"\x00\x00",
        b"\x01\x00",
    ]
    stages = []
    for event in events:
        if not stages or stages[-1] is not event.stage:
            stages.append(event.stage)
    assert stages == [
        UpgradeStage.ERASE,
        UpgradeStage.PROGRAM,
        UpgradeStage.VERIFY,
        UpgradeStage.END,
    ]
    assert events[-1].status is UpgradeStatus.SUCCESS
    assert events[-1].progress == len(firmware.data)
    assert events[-1].total == len(firmware.data)


@pytest.mark.asyncio
async def test_erase_retries_empty_reads_like_android_before_programming() -> None:
    transport = FakeTransport(
        responses=[b"", b"", b"", b"\x00", b"\x00"],
        mtu=23,
    )
    firmware = FirmwareImage(0, bytes(range(16)))

    await OtaController(transport, sleep=no_sleep).upgrade(firmware, image_info())

    assert transport.read_calls == 5
    assert [packet[0] for packet in transport.writes] == [
        0x84,
        0x81,
        0x80,
        0x82,
        0x83,
    ]


@pytest.mark.asyncio
async def test_erase_waits_past_one_second_before_first_windows_read() -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    transport = FakeTransport(responses=[b"", b"", b"\x00", b"\x00"])

    await OtaController(transport, sleep=record_sleep).upgrade(
        FirmwareImage(0, bytes(16)), image_info()
    )

    # 基线 INFO 探测（0.2s）→ 擦除 settle（1.5s）→ 空读重试（0.5s）→ 校验等待（1s）
    assert delays[:4] == [0.2, 1.5, 0.5, 1]


@pytest.mark.asyncio
async def test_wch_dll_erase_waits_like_android_then_polls_every_twenty_ms() -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    transport = FakeTransport(responses=[b"", b"", b"\x00", b"\x00"])
    transport.erase_settle_delay = 1.0
    transport.erase_read_attempts = 50
    transport.erase_read_retry_delay = 0.02

    await OtaController(transport, sleep=record_sleep).upgrade(
        FirmwareImage(0, bytes(16)), image_info()
    )

    assert delays[:3] == [0.2, 1.0, 0.02]
    assert transport.read_calls == 4


@pytest.mark.asyncio
async def test_wch_dll_uses_android_erase_as_first_command() -> None:
    transport = FakeTransport(responses=[b"", b"\x00", b"\x00"])
    transport.prefer_compact_erase = False

    await OtaController(transport, sleep=no_sleep).upgrade(
        FirmwareImage(0x1000, bytes(16)), image_info()
    )

    assert transport.writes[1] == build_erase_command(
        0x1000, 1, ChipType.CH583, target_image=ImageType.A
    )
    assert transport.writes[0] == build_info_command()


@pytest.mark.asyncio
async def test_erase_matches_android_no_response_write_even_when_ack_is_supported() -> None:
    transport = FakeTransport(
        responses=[b"", b"\x00", b"\x00"],
        supports_write_with_response=True,
    )

    await OtaController(transport, sleep=no_sleep).upgrade(
        FirmwareImage(0, bytes(16)), image_info()
    )

    assert transport.write_responses == [False, False, False, False, False]


@pytest.mark.asyncio
async def test_empty_no_response_erase_retries_same_android_frame_with_response() -> None:
    events = []
    command = build_erase_command(
        0x1000, 1, ChipType.CH583, target_image=ImageType.A
    )
    transport = FakeTransport(
        responses=(
            [b""] * 12
            + [info_response(block_size=16)]
            + [b"\x00", b"\x00"]
        ),
        mtu=247,
        supports_write_with_response=True,
    )

    await OtaController(transport, events.append, sleep=no_sleep).upgrade(
        FirmwareImage(0x1000, bytes(16)), image_info(block_size=16)
    )

    assert transport.writes[:4] == [
        build_info_command(),
        command,
        build_info_command(),
        command,
    ]
    assert transport.write_responses[:4] == [False, False, False, True]
    assert any("有响应写入" in event.message for event in events)


@pytest.mark.asyncio
async def test_empty_erase_response_probes_info_channel_before_failing() -> None:
    transport = FakeTransport(
        responses=(
            [b""] * 12
            + [info_response(block_size=4096)]
            + [b""] * 11
        ),
        mtu=247,
    )
    controller = OtaController(transport, sleep=no_sleep)

    with pytest.raises(OtaError, match="INFO 探测有效") as caught:
        await controller.upgrade(
            FirmwareImage(0x1000, bytes(16)), image_info(block_size=4096)
        )

    assert transport.writes == [
        build_info_command(),
        build_erase_command(
            0x1000, 1, ChipType.CH583, target_image=ImageType.A
        ),
        build_info_command(),
        build_compact_erase_command(0x1000, 1, ChipType.CH583),
    ]
    assert "Image A" in str(caught.value)
    assert "块大小 4096" in str(caught.value)
    assert "MTU=247" in str(caught.value)
    assert "兼容帧TX=81 04 00 01 01 00" in str(caught.value)


@pytest.mark.asyncio
async def test_compact_erase_fallback_continues_upgrade_after_valid_info_probe() -> None:
    events = []
    transport = FakeTransport(
        responses=(
            [b""] * 12
            + [info_response(block_size=16)]
            + [b"\x00", b"\x00"]
        ),
        mtu=247,
    )

    await OtaController(transport, events.append, sleep=no_sleep).upgrade(
        FirmwareImage(0x1000, bytes(16)), image_info(block_size=16)
    )

    assert transport.writes[:4] == [
        build_info_command(),
        build_erase_command(
            0x1000, 1, ChipType.CH583, target_image=ImageType.A
        ),
        build_info_command(),
        build_compact_erase_command(0x1000, 1, ChipType.CH583),
    ]
    assert [packet[0] for packet in transport.writes[4:]] == [0x80, 0x82, 0x83]
    assert any("6 字节兼容帧" in event.message for event in events)


@pytest.mark.asyncio
async def test_erase_does_not_treat_changed_info_as_success() -> None:
    """INFO 尾部字段会自然变化，不能代替擦除阶段 ACK。"""
    events = []
    transport = FakeTransport(
        responses=(
            [info_response(block_size=16)]
            + [b""] * 11
            + [info_response(block_size=4096)]
            + [b""] * 11
        ),
        mtu=247,
    )

    with pytest.raises(OtaError, match="擦除失败"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0x1000, bytes(16)), image_info(block_size=16)
        )

    assert transport.writes[:3] == [
        build_info_command(),
        build_erase_command(
            0x1000, 1, ChipType.CH583, target_image=ImageType.A
        ),
        build_info_command(),
    ]
    assert all(packet[0] not in {0x80, 0x82, 0x83} for packet in transport.writes[3:])
    assert events[-1].status is UpgradeStatus.FAILED


@pytest.mark.asyncio
async def test_erase_confirmed_complete_when_probe_info_matches_baseline_only_after_retry() -> None:
    """擦除后 INFO 与基线相同（擦除未执行）时仍走有响应写重试。"""
    events = []
    transport = FakeTransport(
        responses=(
            [info_response(block_size=16)]
            + [b""] * 11
            + [info_response(block_size=16)]
            + [b"\x00", b"\x00"]
        ),
        mtu=247,
        supports_write_with_response=True,
    )

    await OtaController(transport, events.append, sleep=no_sleep).upgrade(
        FirmwareImage(0x1000, bytes(16)), image_info(block_size=16)
    )

    assert transport.writes[:4] == [
        build_info_command(),
        build_erase_command(
            0x1000, 1, ChipType.CH583, target_image=ImageType.A
        ),
        build_info_command(),
        build_erase_command(
            0x1000, 1, ChipType.CH583, target_image=ImageType.A
        ),
    ]
    assert any("有响应写入" in event.message for event in events)
    assert events[-1].status is UpgradeStatus.SUCCESS


@pytest.mark.asyncio
async def test_verify_does_not_treat_info_probe_as_success() -> None:
    """INFO 可读只说明链路存活，不能代替校验阶段 ACK。"""
    events = []
    transport = FakeTransport(
        responses=[b"", b"\x00"] + [b""] * 51 + [info_response(block_size=16)],
        mtu=23,
    )

    with pytest.raises(OtaError, match="校验失败"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(16)), image_info()
        )

    assert transport.writes[-1] != build_end_command()
    assert events[-1].status is UpgradeStatus.FAILED


@pytest.mark.asyncio
async def test_upgrade_rejects_firmware_larger_than_target_image() -> None:
    transport = FakeTransport()
    events = []
    info = CurrentImageInfo(ChipType.CH583, ImageType.A, 16, 16)

    with pytest.raises(OtaError, match="超过目标 Image 最大大小"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0x1000, bytes(17)), info
        )

    assert transport.writes == []
    assert events[-1].status is UpgradeStatus.FAILED


def test_iap_preflight_uses_manual_address_without_ab_capacity_limit() -> None:
    controller = OtaController(FakeTransport(), sleep=no_sleep)
    firmware = FirmwareImage(0x1000, bytes(17))
    info = CurrentImageInfo(ChipType.CH583, ImageType.IAP, 16, 16)

    start_address = controller._target_address(firmware, info)

    assert start_address == 0x1000
    assert controller._preflight(firmware, info, start_address) == 2


def test_ch579_rejects_iap_target() -> None:
    controller = OtaController(FakeTransport(), sleep=no_sleep)
    info = CurrentImageInfo(ChipType.CH579, ImageType.IAP, 0x1200, 256)

    with pytest.raises(OtaError, match="CH579.*IAP"):
        controller._target_address(FirmwareImage(0, bytes(16)), info)


@pytest.mark.asyncio
async def test_iap_erase_failure_never_falls_back_to_frame_without_image_flag() -> None:
    transport = FakeTransport(
        responses=[b""] * 12 + [info_response(block_size=16)] + [b""] * 11,
        mtu=247,
    )
    info = CurrentImageInfo(ChipType.CH583, ImageType.IAP, 16, 16)

    with pytest.raises(OtaError, match="擦除失败"):
        await OtaController(transport, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(17)), info
        )

    assert transport.writes == [
        build_info_command(),
        build_erase_command(0, 2, ChipType.CH583, target_image=ImageType.IAP),
        build_info_command(),
    ]


@pytest.mark.asyncio
async def test_upgrade_rejects_start_address_not_aligned_to_erase_block() -> None:
    transport = FakeTransport()
    events = []

    with pytest.raises(OtaError, match="擦除块大小 4096 字节对齐"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0x1010, bytes(16)), image_info(block_size=4096)
        )

    assert transport.writes == []
    assert events[-1].status is UpgradeStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "image_offset", "expected_address"),
    [
        (ImageType.A, 0x1200, 0x1200),
        (ImageType.B, 0x1200, 0),
    ],
)
async def test_ch579_uses_image_aware_target_address(
    image: ImageType, image_offset: int, expected_address: int
) -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"], mtu=23)
    firmware = FirmwareImage(0x2000, bytes(range(16)))
    info = CurrentImageInfo(ChipType.CH579, image, image_offset, 16)

    await OtaController(transport, sleep=no_sleep).upgrade(firmware, info)

    assert transport.writes[1] == build_erase_command(
        expected_address, 1, ChipType.CH579, target_image=image
    )
    assert transport.writes[2] == build_program_command(
        expected_address, firmware.data, 0, 23, ChipType.CH579
    )
    assert transport.writes[3] == build_verify_command(
        expected_address, firmware.data, 0, 23, ChipType.CH579
    )


@pytest.mark.asyncio
async def test_program_and_verify_progress_use_real_bytes_and_reach_total() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"], mtu=23)
    events = []
    firmware = FirmwareImage(0, bytes(range(35)))

    await OtaController(transport, events.append, sleep=no_sleep).upgrade(
        firmware, image_info()
    )

    for stage in (UpgradeStage.PROGRAM, UpgradeStage.VERIFY):
        progress = [event.progress for event in events if event.stage is stage]
        assert progress == sorted(progress)
        assert progress[-1] == len(firmware.data)
        assert all(event.total == len(firmware.data) for event in events if event.stage is stage)


@pytest.mark.asyncio
async def test_effective_mtu_controls_packet_sizes() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"], mtu=247)
    firmware = FirmwareImage(0, bytes(range(256)) + bytes(range(44)))

    await OtaController(transport, sleep=no_sleep).upgrade(firmware, image_info())

    program = [packet for packet in transport.writes if packet[0] == 0x80]
    verify = [packet for packet in transport.writes if packet[0] == 0x82]
    assert [len(packet) for packet in program] == [244, 244]
    assert [len(packet) for packet in verify] == [244, 244]


@pytest.mark.asyncio
async def test_cancel_before_upgrade_emits_cancelled_and_sends_nothing() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00"])
    events = []
    controller = OtaController(transport, events.append, sleep=no_sleep)
    controller.cancel()

    await controller.upgrade(FirmwareImage(0, bytes(32)), image_info())

    assert transport.writes == []
    assert events[-1].status is UpgradeStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_after_program_packet_stops_before_next_packet() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00"])
    events = []
    controller = OtaController(transport, events.append, sleep=no_sleep)

    async def cancel_after_first_program(payload: bytes) -> None:
        if payload[0] == 0x80:
            controller.cancel()

    transport.write_hook = cancel_after_first_program
    await controller.upgrade(FirmwareImage(0, bytes(40)), image_info())

    assert [packet[0] for packet in transport.writes] == [0x84, 0x81, 0x80]
    assert events[-1].status is UpgradeStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_during_erase_wait_stops_before_reads_and_fallback_commands() -> None:
    transport = FakeTransport(responses=[info_response()])
    events = []
    controller = None

    async def cancel_during_erase_wait(delay: float) -> None:
        if delay >= 1.0:
            controller.cancel()

    controller = OtaController(
        transport, events.append, sleep=cancel_during_erase_wait
    )

    await controller.upgrade(FirmwareImage(0, bytes(16)), image_info())

    assert transport.writes == [
        build_info_command(),
        build_erase_command(0, 1, ChipType.CH583, target_image=ImageType.A),
    ]
    assert transport.read_calls == 1
    assert events[-1].status is UpgradeStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_during_pre_erase_info_wait_never_sends_erase_command() -> None:
    transport = FakeTransport(responses=[info_response()])
    events = []
    controller = None

    async def cancel_during_info_wait(delay: float) -> None:
        if delay == 0.2:
            controller.cancel()

    controller = OtaController(
        transport, events.append, sleep=cancel_during_info_wait
    )

    await controller.upgrade(FirmwareImage(0, bytes(16)), image_info())

    assert transport.writes == [build_info_command()]
    assert transport.read_calls == 0
    assert events[-1].status is UpgradeStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "stage_name"),
    [([b"\x00", b"\x01"], "擦除"), ([b"\x00", b"\x00", b"\x01"], "校验")],
)
async def test_failed_stage_emits_failed_and_raises(
    responses: list[bytes], stage_name: str
) -> None:
    transport = FakeTransport(responses=responses)
    events = []

    with pytest.raises(OtaError, match=stage_name):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(16)), image_info()
        )

    assert events[-1].status is UpgradeStatus.FAILED
    assert transport.is_connected


@pytest.mark.asyncio
async def test_transport_error_is_wrapped_with_cause_and_failed_event() -> None:
    transport = FakeTransport()
    transport.write_error = TransportError("无线链路中断")
    events = []

    with pytest.raises(OtaError, match="无线链路中断") as caught:
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0, b"x"), image_info()
        )

    assert isinstance(caught.value.__cause__, TransportError)
    assert events[-1].status is UpgradeStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data_length", "block_size"),
    [(1, 0), (65536, 1)],
    ids=["zero-block-size", "too-many-blocks"],
)
async def test_upgrade_rejects_invalid_block_count(
    data_length: int, block_size: int
) -> None:
    transport = FakeTransport()
    events = []

    with pytest.raises(OtaError, match="块"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(data_length)), image_info(block_size=block_size)
        )

    assert transport.writes == []
    assert events[-1].status is UpgradeStatus.FAILED


@pytest.mark.asyncio
async def test_upgrade_rejects_concurrent_reentry_and_recovers_running_flag() -> None:
    entered_sleep = asyncio.Event()
    release_sleep = asyncio.Event()

    async def gated_sleep(_delay: float) -> None:
        entered_sleep.set()
        await release_sleep.wait()

    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"])
    controller = OtaController(transport, sleep=gated_sleep)
    first = asyncio.create_task(
        controller.upgrade(FirmwareImage(0, bytes(16)), image_info())
    )
    await entered_sleep.wait()

    with pytest.raises(OtaError, match="进行中"):
        await controller.upgrade(FirmwareImage(0, bytes(16)), image_info())

    release_sleep.set()
    await first
    assert not controller.running


@pytest.mark.asyncio
async def test_observer_exception_does_not_interrupt_upgrade() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"])
    observer_error = RuntimeError("界面事件处理失败")

    def broken_observer(_event: object) -> None:
        raise observer_error

    controller = OtaController(transport, broken_observer, sleep=no_sleep)

    await controller.upgrade(FirmwareImage(0, bytes(16)), image_info())

    assert [packet[0] for packet in transport.writes] == [
        0x84,
        0x81,
        0x80,
        0x82,
        0x83,
    ]
    assert controller.last_observer_error is observer_error


@pytest.mark.asyncio
async def test_get_info_rejects_busy_controller_without_writing_info() -> None:
    entered_sleep = asyncio.Event()
    release_sleep = asyncio.Event()

    async def gated_sleep(_delay: float) -> None:
        entered_sleep.set()
        await release_sleep.wait()

    transport = FakeTransport(responses=[b"\x00", b"\x00", b"\x00"])
    controller = OtaController(transport, sleep=gated_sleep)
    upgrade = asyncio.create_task(
        controller.upgrade(FirmwareImage(0, bytes(16)), image_info())
    )
    await entered_sleep.wait()

    with pytest.raises(OtaError, match="进行中"):
        await controller.get_current_image_info()

    # 进行中的升级仅写入其擦除前基线 INFO 探测，get_current_image_info 未额外写入
    assert transport.writes.count(build_info_command()) == 1
    release_sleep.set()
    await upgrade


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("firmware", "info", "message"),
    [
        (
            FirmwareImage(0, b"x"),
            CurrentImageInfo(ChipType.UNKNOWN, ImageType.A, 0, 16),
            "芯片",
        ),
        (FirmwareImage(1, b"x"), image_info(), "对齐"),
        (FirmwareImage(0, b"x"), CurrentImageInfo(ChipType.CH579, ImageType.A, 2, 16), "对齐"),
        (FirmwareImage(0, b""), image_info(), "为空"),
        (FirmwareImage(0xFFFF0, bytes(17)), image_info(), "范围"),
        (
            FirmwareImage(0, bytes(5)),
            CurrentImageInfo(ChipType.CH579, ImageType.A, 0x3FFFC, 16),
            "范围",
        ),
    ],
    ids=[
        "unsupported-chip",
        "unaligned-16-byte-chip",
        "unaligned-ch579",
        "empty-data",
        "cross-16-byte-chip-boundary",
        "cross-ch579-boundary",
    ],
)
async def test_upgrade_preflight_rejects_invalid_image_before_any_write(
    firmware: FirmwareImage, info: CurrentImageInfo, message: str
) -> None:
    transport = FakeTransport()

    with pytest.raises(OtaError, match=message):
        await OtaController(transport, sleep=no_sleep).upgrade(firmware, info)

    assert transport.writes == []


@pytest.mark.asyncio
async def test_failed_event_preserves_erase_error_detail() -> None:
    transport = FakeTransport(responses=[b"\x00", b"\x05\xAA"])
    events = []

    with pytest.raises(OtaError, match="状态 0x05"):
        await OtaController(transport, events.append, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(16)), image_info()
        )

    assert events[-1].status is UpgradeStatus.FAILED
    assert "地址 0x00000000" in events[-1].message
    assert "块数 1" in events[-1].message
    assert "RX=05 AA" in events[-1].message
    assert "写入=无响应" in events[-1].message
    assert "FEE1=read,write-without-response" in events[-1].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chip",
    [
        ChipType.CH573,
        ChipType.CH583,
        ChipType.CH579,
        ChipType.CH32V208,
        ChipType.CH32F208,
        ChipType.CH592,
    ],
)
async def test_all_supported_chips_reject_unknown_current_image(chip: ChipType) -> None:
    transport = FakeTransport()
    info = CurrentImageInfo(chip, ImageType.UNKNOWN, 0, 16)

    with pytest.raises(OtaError, match="Image"):
        await OtaController(transport, sleep=no_sleep).upgrade(
            FirmwareImage(0, bytes(16)), info
        )

    assert transport.writes == []
