"""Firmware image parsing contracts."""

from pathlib import Path
from types import SimpleNamespace
import stat

import pytest

import wch_ota.domain.firmware as firmware_module
from wch_ota.domain.errors import FirmwareError
from wch_ota.domain.firmware import (
    MAX_FIRMWARE_SPAN,
    MAX_HEX_FILE_SIZE,
    FirmwareImage,
    parse_firmware,
)


FIXTURES = Path(__file__).parent.parent / "fixtures"


def write_hex(tmp_path: Path, name: str, *records: str) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(records) + "\n", encoding="ascii")
    return path


def test_parse_contiguous_hex_uses_lowest_absolute_data_address() -> None:
    image = parse_firmware(FIXTURES / "firmware_contiguous.hex")

    assert image == FirmwareImage(start_address=0x10010, data=b"\x01\x02\x03\x04")


def test_parse_sparse_hex_zero_fills_the_address_gap() -> None:
    image = parse_firmware(FIXTURES / "firmware_sparse.hex")

    assert image.start_address == 0x10
    assert image.data == b"\xAA\xBB" + bytes(14) + b"\xCC"


def test_parse_hex_rejects_sparse_span_over_sixty_four_mebibytes(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "too-sparse.hex",
        ":0100000001FE",
        ":020000040400F6",
        ":0100000002FD",
        ":00000001FF",
    )

    with pytest.raises(FirmwareError, match=str(MAX_FIRMWARE_SPAN)):
        parse_firmware(path)


def test_parse_hex_rejects_file_larger_than_sixteen_mebibytes_before_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "oversized.hex"
    path.write_text(":00000001FF\n", encoding="ascii")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self: SimpleNamespace(st_size=MAX_HEX_FILE_SIZE + 1, st_mode=stat.S_IFREG),
    )
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs: pytest.fail("HEX was opened"))

    with pytest.raises(FirmwareError, match=str(MAX_HEX_FILE_SIZE)):
        parse_firmware(path)


def test_scan_hex_rejects_overlapping_records_whose_total_data_exceeds_limit() -> None:
    record_data = bytes(255)
    record_count = MAX_FIRMWARE_SPAN // len(record_data) + 1

    def records():
        for _ in range(record_count):
            yield 255, 0, 0x00, record_data

    with pytest.raises(FirmwareError, match=str(MAX_FIRMWARE_SPAN)):
        firmware_module._scan_hex_records(records())


def test_parse_hex_supports_extended_linear_address_records(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "linear.hex",
        ":020000040002F8",
        ":01000300AB51",
        ":00000001FF",
    )

    assert parse_firmware(path) == FirmwareImage(0x20003, b"\xAB")


def test_parse_hex_supports_extended_segment_address_records(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "segment.hex",
        ":020000021234B6",
        ":01000500CD2D",
        ":00000001FF",
    )

    assert parse_firmware(path) == FirmwareImage(0x12345, b"\xCD")


def test_parse_hex_overwrites_data_at_the_same_absolute_address(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "overlap.hex",
        ":020000000102FB",
        ":01000100FFff",
        ":00000001FF",
    )

    assert parse_firmware(path) == FirmwareImage(0, b"\x01\xFF")


def test_parse_hex_rejects_data_record_which_overflows_32_bit_address(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "overflow.hex",
        ":02000004FFFFFC",
        ":02FFFF000102FD",
        ":00000001FF",
    )

    with pytest.raises(FirmwareError):
        parse_firmware(path)


@pytest.mark.parametrize(
    "records",
    [
        (":0100000001FF", ":00000001FF"),
        (":01000000GGFF", ":00000001FF"),
        (":0100000001FE  ", ":00000001FF"),
        (":0100000001F", ":00000001FF"),
        ("0100000001FE", ":00000001FF"),
        (":0200000001FC", ":00000001FF"),
    ],
)
def test_parse_hex_rejects_malformed_records(tmp_path: Path, records: tuple[str, ...]) -> None:
    with pytest.raises(FirmwareError):
        parse_firmware(write_hex(tmp_path, "invalid.hex", *records))


@pytest.mark.parametrize(
    "records",
    [
        (":0100000001FE",),
        (":00000001FF",),
        (":0100000001FE", ":00000001FF", ":0100010002FC"),
        (":00000006FA", ":00000001FF"),
    ],
)
def test_parse_hex_requires_data_then_eof_and_rejects_unsupported_types(
    tmp_path: Path, records: tuple[str, ...]
) -> None:
    with pytest.raises(FirmwareError):
        parse_firmware(write_hex(tmp_path, "invalid.hex", *records))


def test_parse_hex_ignores_start_address_records(tmp_path: Path) -> None:
    path = write_hex(
        tmp_path,
        "starts.hex",
        ":0400000300000000F9",
        ":0400000500000000F7",
        ":0100000001FE",
        ":00000001FF",
    )

    assert parse_firmware(path) == FirmwareImage(0, b"\x01")


@pytest.mark.parametrize(
    "record",
    [":00000003FD", ":0400010300000000F8", ":00000005FB", ":0400010500000000F6"],
)
def test_parse_hex_rejects_malformed_start_address_records(
    tmp_path: Path, record: str
) -> None:
    path = write_hex(tmp_path, "invalid-start.hex", record, ":0100000001FE", ":00000001FF")

    with pytest.raises(FirmwareError):
        parse_firmware(path)


def test_parse_uppercase_hex_extension(tmp_path: Path) -> None:
    path = write_hex(tmp_path, "image.HEX", ":0100000001FE", ":00000001FF")

    assert parse_firmware(path) == FirmwareImage(0, b"\x01")


def test_parse_bin_skips_full_image_prefix_before_erase_address(tmp_path: Path) -> None:
    path = tmp_path / "image.BIN"
    path.write_bytes(b"\x10\x20\x30\x40\x50")

    assert parse_firmware(path, erase_address=3) == FirmwareImage(3, b"\x40\x50")


def test_parse_bin_rejects_data_that_overflows_32_bit_address(tmp_path: Path) -> None:
    path = tmp_path / "overflow.bin"
    path.write_bytes(b"\x01\x02")

    with pytest.raises(FirmwareError):
        parse_firmware(path, erase_address=0xFFFFFFFF)


@pytest.mark.parametrize("erase_address", [None, -1, 0x1_0000_0000, True, 1.5, "0"])
def test_parse_bin_requires_a_non_negative_32_bit_integer_erase_address(
    tmp_path: Path, erase_address: object
) -> None:
    path = tmp_path / "image.bin"
    path.write_bytes(b"\x01")

    with pytest.raises(FirmwareError):
        parse_firmware(path, erase_address=erase_address)


def test_parse_bin_rejects_size_over_firmware_span_without_reading_large_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "oversized.bin"
    path.write_bytes(b"x")
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self: SimpleNamespace(st_size=MAX_FIRMWARE_SPAN + 1, st_mode=stat.S_IFREG),
    )
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("oversized BIN was read"))

    with pytest.raises(FirmwareError, match=str(MAX_FIRMWARE_SPAN)):
        parse_firmware(path, erase_address=0)


def test_parse_bin_rejects_empty_or_missing_file(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")

    for path in (empty, tmp_path / "missing.bin"):
        with pytest.raises(FirmwareError):
            parse_firmware(path, erase_address=0)


def test_parse_firmware_rejects_unsupported_file_extension(tmp_path: Path) -> None:
    path = tmp_path / "image.txt"
    path.write_bytes(b"data")

    with pytest.raises(FirmwareError):
        parse_firmware(path, erase_address=0)
