"""User-facing domain errors."""


class FirmwareError(Exception):
    """Raised when a firmware file cannot be used for an OTA update."""
