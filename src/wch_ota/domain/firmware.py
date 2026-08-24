"""Firmware file parsing independent from BLE transports and user interfaces."""

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterable

from .errors import FirmwareError

MAX_FIRMWARE_SPAN = 64 * 1024 * 1024
MAX_HEX_FILE_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FirmwareImage:
    """Firmware bytes positioned at their OTA erase address."""

    start_address: int
    data: bytes


def parse_firmware(
    file_path: str | Path, *, erase_address: int | None = None
) -> FirmwareImage:
    """Parse a BIN or Intel HEX file into an address and contiguous image bytes."""
    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix not in {".bin", ".hex"}:
        raise FirmwareError("仅支持 BIN 或 HEX 固件文件")
    if not path.is_file():
        raise FirmwareError("固件文件不存在")

    if suffix == ".bin":
        return _parse_bin(path, erase_address)
    return _parse_hex(path)


def _parse_bin(path: Path, erase_address: int | None) -> FirmwareImage:
    if type(erase_address) is not int or not 0 <= erase_address <= 0xFFFFFFFF:
        raise FirmwareError("BIN 固件需要 32 位非负整数擦除地址")
    try:
        file_size = path.stat().st_size
    except OSError as error:
        raise FirmwareError("无法读取固件文件") from error
    if file_size == 0:
        raise FirmwareError("BIN 固件文件不能为空")
    if file_size > MAX_FIRMWARE_SPAN:
        raise FirmwareError(
            f"BIN 固件大小 {file_size} 字节超过上限 {MAX_FIRMWARE_SPAN} 字节"
        )
    if erase_address >= file_size:
        raise FirmwareError("BIN 擦除地址超出文件数据范围")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise FirmwareError("无法读取固件文件") from error
    if not data:
        raise FirmwareError("BIN 固件文件不能为空")
    data = data[erase_address:]
    if len(data) > MAX_FIRMWARE_SPAN:
        raise FirmwareError(
            f"BIN 固件大小 {len(data)} 字节超过上限 {MAX_FIRMWARE_SPAN} 字节"
        )
    return FirmwareImage(start_address=erase_address, data=data)


def _parse_hex(path: Path) -> FirmwareImage:
    try:
        file_size = path.stat().st_size
    except OSError as error:
        raise FirmwareError("无法读取固件文件") from error
    if file_size > MAX_HEX_FILE_SIZE:
        raise FirmwareError(
            f"HEX 固件文件大小 {file_size} 字节超过上限 {MAX_HEX_FILE_SIZE} 字节"
        )

    start_address, end_address = _scan_hex_records(_iter_hex_records(path))
    image_span = end_address - start_address + 1
    if image_span > MAX_FIRMWARE_SPAN:
        raise FirmwareError(
            f"HEX 固件跨度 {image_span} 字节超过上限 {MAX_FIRMWARE_SPAN} 字节"
        )

    image = bytearray(image_span)
    _fill_hex_records(_iter_hex_records(path), start_address, end_address, image)
    return FirmwareImage(start_address=start_address, data=bytes(image))


def _iter_hex_records(path: Path) -> Iterable[tuple[int, int, int, bytes]]:
    try:
        with path.open("r", encoding="ascii", newline="") as firmware_file:
            for line_number, raw_line in enumerate(firmware_file, start=1):
                yield _parse_record(raw_line.rstrip("\r\n"), line_number)
    except UnicodeDecodeError as error:
        raise FirmwareError("HEX 固件必须是 ASCII 文本") from error
    except OSError as error:
        raise FirmwareError("无法读取固件文件") from error


def _scan_hex_records(
    records: Iterable[tuple[int, int, int, bytes]],
) -> tuple[int, int]:
    base_address = 0
    eof_seen = False
    data_count = 0
    start_address: int | None = None
    end_address: int | None = None
    record_seen = False
    for count, address, record_type, record_data in records:
        record_seen = True
        if eof_seen:
            raise FirmwareError("HEX 记录出现在 EOF 记录之后")

        if record_type == 0x00:
            absolute_start, absolute_end = _data_bounds(base_address, address, count, record_data)
            if record_data:
                data_count += len(record_data)
                if data_count > MAX_FIRMWARE_SPAN:
                    raise FirmwareError(
                        f"HEX 数据总量 {data_count} 字节超过上限 {MAX_FIRMWARE_SPAN} 字节"
                    )
                start_address = (
                    absolute_start if start_address is None else min(start_address, absolute_start)
                )
                end_address = absolute_end if end_address is None else max(end_address, absolute_end)
        else:
            base_address, eof_seen = _handle_non_data_record(
                count, address, record_type, record_data, base_address
            )

    if not record_seen:
        raise FirmwareError("HEX 固件文件不能为空")
    if not eof_seen:
        raise FirmwareError("HEX 固件缺少 EOF 记录")
    if start_address is None or end_address is None:
        raise FirmwareError("HEX 固件不包含数据记录")
    return start_address, end_address


def _fill_hex_records(
    records: Iterable[tuple[int, int, int, bytes]],
    start_address: int,
    end_address: int,
    image: bytearray,
) -> None:
    base_address = 0
    data_count = 0
    eof_seen = False
    for count, address, record_type, record_data in records:
        if eof_seen:
            raise FirmwareError("HEX 记录出现在 EOF 记录之后")
        if record_type == 0x00:
            absolute_start, absolute_end = _data_bounds(base_address, address, count, record_data)
            if record_data:
                data_count += len(record_data)
                if data_count > MAX_FIRMWARE_SPAN:
                    raise FirmwareError(
                        f"HEX 数据总量 {data_count} 字节超过上限 {MAX_FIRMWARE_SPAN} 字节"
                    )
                if absolute_start < start_address or absolute_end > end_address:
                    raise FirmwareError("HEX 固件在读取期间发生变化")
                image[absolute_start - start_address : absolute_end - start_address + 1] = record_data
        else:
            base_address, eof_seen = _handle_non_data_record(
                count, address, record_type, record_data, base_address
            )
    if not eof_seen:
        raise FirmwareError("HEX 固件缺少 EOF 记录")


def _data_bounds(base_address: int, address: int, count: int, data: bytes) -> tuple[int, int]:
    absolute_start = base_address + address
    absolute_end = absolute_start + count - 1
    if data and (absolute_start < 0 or absolute_end > 0xFFFFFFFF):
        raise FirmwareError("HEX 数据记录地址超出 32 位范围")
    return absolute_start, absolute_end


def _handle_non_data_record(
    count: int, address: int, record_type: int, data: bytes, base_address: int
) -> tuple[int, bool]:
    if record_type == 0x01:
        if count != 0 or address != 0:
            raise FirmwareError("HEX EOF 记录无效")
        return base_address, True
    if record_type == 0x02:
        if count != 2 or address != 0:
            raise FirmwareError("HEX 扩展段地址记录无效")
        return int.from_bytes(data, "big") << 4, False
    if record_type == 0x04:
        if count != 2 or address != 0:
            raise FirmwareError("HEX 扩展线性地址记录无效")
        return int.from_bytes(data, "big") << 16, False
    if record_type in {0x03, 0x05}:
        if count != 4 or address != 0:
            raise FirmwareError("HEX 起始地址记录无效")
        return base_address, False
    raise FirmwareError("HEX 包含不支持的记录类型")


def _parse_record(line: str, line_number: int) -> tuple[int, int, int, bytes]:
    if not line.startswith(":"):
        raise FirmwareError(f"第 {line_number} 行必须以冒号开头")
    encoded = line[1:]
    if len(encoded) < 10 or len(encoded) % 2:
        raise FirmwareError(f"第 {line_number} 行 HEX 记录长度无效")
    if any(character not in "0123456789abcdefABCDEF" for character in encoded):
        raise FirmwareError(f"第 {line_number} 行含有非法十六进制字符")
    try:
        raw = bytes.fromhex(encoded)
    except ValueError as error:
        raise FirmwareError(f"第 {line_number} 行含有非法十六进制字符") from error

    count = raw[0]
    if len(raw) != count + 5:
        raise FirmwareError(f"第 {line_number} 行 HEX 记录长度与字节数不符")
    if sum(raw) & 0xFF:
        raise FirmwareError(f"第 {line_number} 行 HEX 校验和无效")
    return count, (raw[1] << 8) | raw[2], raw[3], raw[4:-1]
