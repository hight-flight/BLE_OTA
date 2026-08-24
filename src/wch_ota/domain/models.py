"""OTA domain value objects independent from transports and user interfaces."""

from dataclasses import dataclass
from enum import Enum


class ChipType(Enum):
    """Chips recognized by the WCH OTA image-info response."""

    CH583 = "CH583"
    CH32V208 = "CH32V208"
    CH32F208 = "CH32F208"
    CH579 = "CH579"
    CH573 = "CH573"
    CH592 = "CH592"
    UNKNOWN = "UNKNOWN"


class ImageType(Enum):
    """Image slots reported by the bootloader."""

    A = "A"
    B = "B"
    IAP = "IAP"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class CurrentImageInfo:
    """The image currently selected by the OTA bootloader."""

    chip: ChipType
    image: ImageType
    offset: int
    block_size: int
