"""Domain model and protocol helpers for WCH BLE OTA."""

from .models import ChipType, CurrentImageInfo, ImageType
from .protocol import (
    build_end_command,
    build_compact_erase_command,
    build_erase_command,
    build_info_command,
    build_program_command,
    build_verify_command,
    is_erase_success,
    is_verify_success,
    parse_image_info_response,
)

__all__ = [
    "ChipType",
    "CurrentImageInfo",
    "ImageType",
    "build_end_command",
    "build_compact_erase_command",
    "build_erase_command",
    "build_info_command",
    "build_program_command",
    "build_verify_command",
    "is_erase_success",
    "is_verify_success",
    "parse_image_info_response",
]
