"""BLE 扫描结果表格模型。"""

import re
from time import monotonic
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt


def normalize_device_address(address: Any) -> str:
    """统一 Windows/WCH 可能返回的 MAC 大小写及分隔符。"""
    value = str(address or "").strip().upper()
    compact = re.sub(r"[:-]", "", value)
    if re.fullmatch(r"[0-9A-F]{12}", compact):
        return ":".join(compact[index : index + 2] for index in range(0, 12, 2))
    return value


class DeviceTableModel(QAbstractTableModel):
    HEADERS = ("设备名", "地址", "RSSI", "操作")

    def __init__(self, parent=None, *, clock=monotonic) -> None:
        super().__init__(parent)
        self._clock = clock
        self._rows: list[tuple[Any, Any]] = []
        self._addresses: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._name_priorities: dict[str, int] = {}
        self._last_seen: dict[str, float] = {}

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        device, advertisement = self._rows[index.row()]
        if index.column() == 0:
            address = normalize_device_address(getattr(device, "address", ""))
            return self._names.get(address, "未知设备")
        if index.column() == 1:
            return normalize_device_address(getattr(device, "address", ""))
        if index.column() == 2:
            return getattr(advertisement, "rssi", getattr(device, "rssi", None))
        return None

    def update_device(self, device: Any, advertisement: Any) -> None:
        address = normalize_device_address(getattr(device, "address", ""))
        candidates: list[tuple[int, str]] = []
        advertising_name = getattr(advertisement, "local_name", None)
        if isinstance(advertising_name, str) and advertising_name.strip():
            candidates.append(
                (
                    int(getattr(advertisement, "name_priority", 30)),
                    advertising_name.strip(),
                )
            )
        device_name = getattr(device, "name", None)
        if isinstance(device_name, str) and device_name.strip():
            candidates.append((10, device_name.strip()))
        raw_candidate = self._raw_local_name(advertisement)
        if raw_candidate is not None:
            candidates.append(raw_candidate)
        if candidates:
            current_priority, current_name = max(
                candidates, key=lambda candidate: (candidate[0], len(candidate[1]))
            )
            previous_name = self._names.get(address, "")
            previous_priority = self._name_priorities.get(address, -1)
            # 广播 Complete Local Name 比 Windows 缓存名可信；同来源才按长度选优。
            if current_priority > previous_priority or (
                current_priority == previous_priority
                and len(current_name) >= len(previous_name)
            ):
                self._names[address] = current_name
                self._name_priorities[address] = current_priority
        self._last_seen[address] = self._clock()
        if address in self._addresses:
            row = self._addresses[address]
            previous_device, _ = self._rows[row]
            # WCH 实时广播只有 MAC，没有 Windows Device ID。保留兼容扫描
            # 已解析出的 Device ID，避免后续连接退化为不稳定的纯地址连接。
            if (
                not getattr(device, "device_id", "")
                and getattr(previous_device, "device_id", "")
            ):
                device = previous_device
            self._rows[row] = (device, advertisement)
            self.dataChanged.emit(self.index(row, 0), self.index(row, 3))
            return
        row = len(self._rows)
        self.beginInsertRows(QModelIndex(), row, row)
        self._rows.append((device, advertisement))
        self._addresses[address] = row
        self.endInsertRows()

    def prune_stale(self, *, max_age_seconds: float) -> int:
        """移除超过指定时间未收到广播的设备。"""
        now = self._clock()
        stale = {
            address
            for address, seen_at in self._last_seen.items()
            if now - seen_at > max_age_seconds
        }
        if not stale:
            return 0
        self.beginResetModel()
        self._rows = [
            row
            for row in self._rows
            if normalize_device_address(getattr(row[0], "address", "")) not in stale
        ]
        self._addresses = {
            normalize_device_address(getattr(device, "address", "")): index
            for index, (device, _advertisement) in enumerate(self._rows)
        }
        for address in stale:
            self._names.pop(address, None)
            self._name_priorities.pop(address, None)
            self._last_seen.pop(address, None)
        self.endResetModel()
        return len(stale)

    @staticmethod
    def _raw_local_name(advertisement: Any) -> tuple[int, str] | None:
        """从 Windows 原始 ADV/Scan Response 中补取 0x08/0x09 设备名。"""
        platform_data = getattr(advertisement, "platform_data", ())
        if len(platform_data) < 2:
            return None
        raw_pair = platform_data[1]
        try:
            raw_packets = tuple(raw_pair)
        except TypeError:
            raw_packets = (raw_pair,)

        try:
            from bleak.backends.winrt.scanner import AdvertisementDataType

            name_types = (
                AdvertisementDataType.COMPLETE_LOCAL_NAME,
                AdvertisementDataType.SHORTENED_LOCAL_NAME,
            )
        except (ImportError, AttributeError):
            name_types = (9, 8)

        names: list[tuple[int, str]] = []
        for packet in raw_packets:
            native_advertisement = getattr(packet, "advertisement", None)
            if native_advertisement is None:
                continue
            native_name = getattr(native_advertisement, "local_name", None)
            if isinstance(native_name, str) and native_name.strip():
                names.append((30, native_name.strip()))
            get_sections = getattr(native_advertisement, "get_sections_by_type", None)
            if not callable(get_sections):
                continue
            for name_type in name_types:
                for section in get_sections(name_type):
                    try:
                        decoded = bytes(section.data).decode("utf-8").rstrip("\x00").strip()
                    except (AttributeError, TypeError, UnicodeDecodeError):
                        continue
                    if decoded:
                        names.append((40 if int(name_type) == 9 else 20, decoded))
        return max(names, key=lambda candidate: (candidate[0], len(candidate[1]))) if names else None

    def device_at(self, row: int) -> Any | None:
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def clear(self) -> None:
        if self._rows:
            self.beginResetModel()
            self._rows.clear()
            self._addresses.clear()
            self._names.clear()
            self._name_priorities.clear()
            self._last_seen.clear()
            self.endResetModel()
