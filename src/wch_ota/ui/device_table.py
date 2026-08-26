"""BLE 扫描结果表格模型。"""

import re
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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[tuple[Any, Any]] = []
        self._addresses: dict[str, int] = {}
        self._names: dict[str, str] = {}
        self._pending_names: dict[str, tuple[str, int]] = {}

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
        candidates = [
            name.strip()
            for name in (
                getattr(advertisement, "local_name", None),
                getattr(device, "name", None),
                self._raw_local_name(advertisement),
            )
            if isinstance(name, str) and name.strip()
        ]
        if candidates:
            current_name = max(candidates, key=len)
            previous_name = self._names.get(address, "")
            # 同一扫描周期可能先后收到 Shortened/Complete Local Name，保留信息更完整者。
            if len(current_name) >= len(previous_name):
                self._names[address] = current_name
                self._pending_names.pop(address, None)
            elif current_name != previous_name:
                # 真实改名也可能比旧名称短。连续收到相同短名称后再接受，避免
                # 单个 Shortened Local Name 包覆盖完整名称。
                pending_name, count = self._pending_names.get(address, ("", 0))
                count = count + 1 if pending_name == current_name else 1
                if count >= 3:
                    self._names[address] = current_name
                    self._pending_names.pop(address, None)
                else:
                    self._pending_names[address] = (current_name, count)
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

    @staticmethod
    def _raw_local_name(advertisement: Any) -> str | None:
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

        names: list[str] = []
        for packet in raw_packets:
            native_advertisement = getattr(packet, "advertisement", None)
            if native_advertisement is None:
                continue
            native_name = getattr(native_advertisement, "local_name", None)
            if isinstance(native_name, str) and native_name.strip():
                names.append(native_name.strip())
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
                        names.append(decoded)
        return max(names, key=len) if names else None

    def device_at(self, row: int) -> Any | None:
        return self._rows[row][0] if 0 <= row < len(self._rows) else None

    def clear(self) -> None:
        if self._rows:
            self.beginResetModel()
            self._rows.clear()
            self._addresses.clear()
            self._names.clear()
            self._pending_names.clear()
            self.endResetModel()
