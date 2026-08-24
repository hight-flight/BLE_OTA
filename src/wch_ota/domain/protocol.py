"""Pure WCH ISP OTA frame construction and response parsing."""

from .models import ChipType, CurrentImageInfo, ImageType

_IAP_FRAME_LENGTH = 20
_PROGRAM = 0x80
_ERASE = 0x81
_VERIFY = 0x82
_END = 0x83
_INFO = 0x84

_CHIP_IDS: dict[tuple[int, int], ChipType] = {
    (0x83, 0x00): ChipType.CH583,
    (0x08, 0x02): ChipType.CH32V208,
    (0x08, 0xF2): ChipType.CH32F208,
    (0x79, 0x00): ChipType.CH579,
    (0x73, 0x00): ChipType.CH573,
    (0x92, 0x00): ChipType.CH592,
}


def build_info_command() -> bytes:
    """Build the fixed-size command requesting current image information."""
    return bytes([_INFO, _IAP_FRAME_LENGTH - 2]) + bytes(_IAP_FRAME_LENGTH - 2)


def build_erase_command(
    address: int,
    block: int,
    chip: ChipType,
    *,
    target_image: ImageType,
) -> bytes:
    """构造包含目标镜像类型的 20 字节擦除命令。"""
    scaled_address = _scaled_address(address, chip)
    if not 0 <= block <= 0xFFFF:
        raise ValueError("block must fit in an unsigned 16-bit value")
    try:
        image_value = {ImageType.B: 0, ImageType.A: 1}[target_image]
    except KeyError as error:
        raise ValueError("target image must be Image A or Image B") from error
    return bytes([
        _ERASE,
        0,
        scaled_address & 0xFF,
        (scaled_address >> 8) & 0xFF,
        block & 0xFF,
        (block >> 8) & 0xFF,
        image_value,
    ]) + bytes(_IAP_FRAME_LENGTH - 7)


def build_compact_erase_command(address: int, block: int, chip: ChipType) -> bytes:
    """构造 WCH PC 参考工具使用的 6 字节擦除兼容帧。"""
    scaled_address = _scaled_address(address, chip)
    if not 0 <= block <= 0xFFFF:
        raise ValueError("block must fit in an unsigned 16-bit value")
    return bytes([
        _ERASE,
        4,
        scaled_address & 0xFF,
        (scaled_address >> 8) & 0xFF,
        block & 0xFF,
        (block >> 8) & 0xFF,
    ])


def build_program_command(
    address: int, data: bytes, offset: int, mtu: int, chip: ChipType
) -> bytes:
    """Build one PROGRAM frame, zero-padding its final payload as Android does."""
    return _build_data_command(_PROGRAM, address, data, offset, mtu, chip)


def build_verify_command(
    address: int, data: bytes, offset: int, mtu: int, chip: ChipType
) -> bytes:
    """Build one VERIFY frame, zero-padding its final payload as Android does."""
    return _build_data_command(_VERIFY, address, data, offset, mtu, chip)


def build_end_command() -> bytes:
    """Build the fixed-size command ending an OTA session."""
    return bytes([_END, _IAP_FRAME_LENGTH - 2]) + bytes(_IAP_FRAME_LENGTH - 2)


def parse_image_info_response(response: bytes | None) -> CurrentImageInfo | None:
    """Parse an exact 20-byte INFO response, or return ``None`` when invalid."""
    if response is None or len(response) != _IAP_FRAME_LENGTH:
        return None

    image = {0x01: ImageType.A, 0x02: ImageType.B}.get(response[0], ImageType.UNKNOWN)
    return CurrentImageInfo(
        chip=_CHIP_IDS.get((response[7], response[8]), ChipType.UNKNOWN),
        image=image,
        offset=int.from_bytes(response[1:5], "little"),
        block_size=response[5] + 256 * response[6],
    )


def is_erase_success(response: bytes | None) -> bool:
    """Return whether an ERASE response has a successful zero status."""
    return _has_success_status(response)


def is_verify_success(response: bytes | None) -> bool:
    """Return whether a VERIFY response has a successful zero status."""
    return _has_success_status(response)


def _build_data_command(
    opcode: int, address: int, data: bytes, offset: int, mtu: int, chip: ChipType
) -> bytes:
    if not 8 <= mtu <= 262:
        raise ValueError("mtu must be between 8 and 262")

    frame_max_length = mtu - 3
    payload_length = ((frame_max_length - 4) // 16) * 16
    if payload_length == 0:
        raise ValueError("mtu must allow at least one 16-byte aligned payload")
    if not 0 <= offset <= len(data):
        raise ValueError("offset must be within data")

    scaled_address = _scaled_address(address, chip)
    payload = data[offset : offset + payload_length]
    return bytes([
        opcode,
        payload_length,
        scaled_address & 0xFF,
        (scaled_address >> 8) & 0xFF,
    ]) + payload + bytes(payload_length - len(payload))


def _scaled_address(address: int, chip: ChipType) -> int:
    if address < 0:
        raise ValueError("address must not be negative")
    address_base = 4 if chip is ChipType.CH579 else 16
    scaled_address = address // address_base
    if scaled_address > 0xFFFF:
        raise ValueError("scaled address must fit in an unsigned 16-bit value")
    return scaled_address


def _has_success_status(response: bytes | None) -> bool:
    return bool(response) and response[0] == 0
