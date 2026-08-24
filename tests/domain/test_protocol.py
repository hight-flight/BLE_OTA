"""WCH OTA command framing and response parsing contracts."""

from collections.abc import Callable

import pytest

from wch_ota.domain.models import ChipType, ImageType
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


def test_info_and_end_commands_are_fixed_twenty_byte_frames() -> None:
    assert build_info_command() == bytes([0x84, 18] + [0] * 18)
    assert build_end_command() == bytes([0x83, 18] + [0] * 18)


@pytest.mark.parametrize(
    ("chip", "address", "expected_address"),
    [
        (ChipType.CH579, 0x1234, bytes([0x8D, 0x04])),
        (ChipType.CH583, 0x1234, bytes([0x23, 0x01])),
        (ChipType.CH573, 0x1234, bytes([0x23, 0x01])),
        (ChipType.CH592, 0x1234, bytes([0x23, 0x01])),
        (ChipType.CH32V208, 0x1234, bytes([0x23, 0x01])),
        (ChipType.CH32F208, 0x1234, bytes([0x23, 0x01])),
    ],
)
def test_erase_command_encodes_chip_scaled_address_little_endian(
    chip: ChipType, address: int, expected_address: bytes
) -> None:
    command = build_erase_command(
        address, 0x5678, chip, target_image=ImageType.B
    )

    assert command == bytes([0x81, 0, *expected_address, 0x78, 0x56] + [0] * 14)


def test_erase_command_encodes_target_image_a_in_seventh_byte() -> None:
    command = build_erase_command(
        0x1000, 50, ChipType.CH583, target_image=ImageType.A
    )

    assert command == bytes.fromhex(
        "81 00 00 01 32 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00"
    )


def test_erase_command_encodes_target_image_iap_in_seventh_byte() -> None:
    command = build_erase_command(
        0, 111, ChipType.CH583, target_image=ImageType.IAP
    )

    assert command == bytes.fromhex(
        "81 00 00 00 6F 00 02 00 00 00 00 00 00 00 00 00 00 00 00 00"
    )


def test_compact_erase_command_matches_wch_pc_reference_tool() -> None:
    assert build_compact_erase_command(0x1000, 50, ChipType.CH583) == bytes.fromhex(
        "81 04 00 01 32 00"
    )


@pytest.mark.parametrize(
    ("builder", "opcode"),
    [(build_program_command, 0x80), (build_verify_command, 0x82)],
)
def test_data_commands_use_mtu_payload_and_zero_pad_final_packet(
    builder: Callable[[int, bytes, int, int, ChipType], bytes], opcode: int
) -> None:
    command = builder(0x1234, b"\xAA\xBB\xCC", 0, 23, ChipType.CH579)

    assert len(command) == 20
    assert command[:4] == bytes([opcode, 16, 0x8D, 0x04])
    assert command[4:7] == b"\xAA\xBB\xCC"
    assert command[7:] == bytes(13)


@pytest.mark.parametrize(
    ("builder", "opcode"),
    [(build_program_command, 0x80), (build_verify_command, 0x82)],
)
def test_data_commands_honor_offset_and_mtu(
    builder: Callable[[int, bytes, int, int, ChipType], bytes], opcode: int
) -> None:
    command = builder(0x1000, bytes(range(32)), 16, 23, ChipType.CH583)

    assert len(command) == 20
    assert command[:4] == bytes([opcode, 16, 0, 1])
    assert command[4:] == bytes(range(16, 32))


@pytest.mark.parametrize("mtu", [6, 7, 8, 22, 263])
@pytest.mark.parametrize("builder", [build_program_command, build_verify_command])
def test_data_commands_reject_mtu_without_an_aligned_payload_or_unencodable_length(
    builder: Callable[[int, bytes, int, int, ChipType], bytes], mtu: int
) -> None:
    with pytest.raises(ValueError, match="mtu"):
        builder(0, b"\x01", 0, mtu, ChipType.CH583)


@pytest.mark.parametrize("builder", [build_program_command, build_verify_command])
def test_data_commands_accept_smallest_mtu_with_sixteen_byte_aligned_payload(
    builder: Callable[[int, bytes, int, int, ChipType], bytes]
) -> None:
    assert builder(0, bytes(range(16)), 0, 23, ChipType.CH583) == bytes(
        [0x80 if builder is build_program_command else 0x82, 16, 0, 0, *range(16)]
    )


@pytest.mark.parametrize(("mtu", "frame_length", "payload_length"), [(247, 244, 240), (262, 244, 240)])
@pytest.mark.parametrize("builder", [build_program_command, build_verify_command])
def test_data_commands_align_payload_to_sixteen_bytes(
    builder: Callable[[int, bytes, int, int, ChipType], bytes],
    mtu: int,
    frame_length: int,
    payload_length: int,
) -> None:
    command = builder(0, bytes(payload_length), 0, mtu, ChipType.CH583)

    assert len(command) == frame_length
    assert command[1] == payload_length


@pytest.mark.parametrize(
    ("address", "block"),
    [(-1, 0), (0, -1), (0x100000, 0), (0, 0x10000)],
)
def test_erase_command_rejects_negative_or_overflowing_values(address: int, block: int) -> None:
    with pytest.raises(ValueError):
        build_erase_command(
            address, block, ChipType.CH583, target_image=ImageType.B
        )


def test_erase_command_accepts_largest_encodable_address_and_block() -> None:
    command = build_erase_command(
        0xFFFFF, 0xFFFF, ChipType.CH583, target_image=ImageType.B
    )

    assert command[2:6] == bytes([0xFF, 0xFF, 0xFF, 0xFF])


@pytest.mark.parametrize("builder", [build_program_command, build_verify_command])
@pytest.mark.parametrize(
    ("address", "offset"),
    [(-1, 0), (0x100000, 0), (0, -1), (0, 2)],
)
def test_data_commands_reject_invalid_address_or_offset(
    builder: Callable[[int, bytes, int, int, ChipType], bytes], address: int, offset: int
) -> None:
    with pytest.raises(ValueError):
        builder(address, b"\x01", offset, 23, ChipType.CH583)


@pytest.mark.parametrize(
    ("image", "chip_bytes", "expected_image", "expected_chip"),
    [
        (0x01, (0x83, 0x00), ImageType.A, ChipType.CH583),
        (0x02, (0x08, 0x02), ImageType.B, ChipType.CH32V208),
        (0x01, (0x08, 0xF2), ImageType.A, ChipType.CH32F208),
        (0x02, (0x79, 0x00), ImageType.B, ChipType.CH579),
        (0x01, (0x73, 0x00), ImageType.A, ChipType.CH573),
        (0x02, (0x92, 0x00), ImageType.B, ChipType.CH592),
        (0x99, (0xFF, 0xEE), ImageType.UNKNOWN, ChipType.UNKNOWN),
    ],
)
def test_image_info_response_maps_images_chips_and_little_endian_values(
    image: int, chip_bytes: tuple[int, int], expected_image: ImageType, expected_chip: ChipType
) -> None:
    response = bytearray(20)
    response[0] = image
    response[1:5] = (0x12345678).to_bytes(4, "little")
    response[5:7] = (0x3456).to_bytes(2, "little")
    response[7:9] = bytes(chip_bytes)

    info = parse_image_info_response(bytes(response))

    assert info is not None
    assert info.image is expected_image
    assert info.chip is expected_chip
    assert info.offset == 0x12345678
    assert info.block_size == 0x3456


@pytest.mark.parametrize("response", [None, b"", bytes(19), bytes(21)])
def test_invalid_image_info_response_returns_none(response: bytes | None) -> None:
    assert parse_image_info_response(response) is None


@pytest.mark.parametrize(
    ("response", "expected"),
    [(None, False), (b"", False), (b"\x01", False), (b"\x00", True), (b"\x00\xFF", True)],
)
def test_erase_and_verify_success_require_nonempty_zero_status(response: bytes | None, expected: bool) -> None:
    assert is_erase_success(response) is expected
    assert is_verify_success(response) is expected
