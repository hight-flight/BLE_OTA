from dataclasses import dataclass
from types import SimpleNamespace

from PySide6.QtCore import Qt

from wch_ota.ui.device_table import DeviceTableModel


@dataclass
class Device:
    name: str
    address: str


@dataclass
class Advertisement:
    local_name: str | None
    rssi: int


def test_device_table_deduplicates_by_address_and_keeps_original_device():
    model = DeviceTableModel()
    first = Device("first", "AA:BB")
    replacement = Device("second", "AA:BB")

    model.update_device(first, Advertisement(None, -80))
    model.update_device(replacement, Advertisement("Friendly", -42))

    assert model.rowCount() == 1
    assert model.data(model.index(0, 0), Qt.DisplayRole) == "Friendly"
    assert model.data(model.index(0, 1), Qt.DisplayRole) == "AA:BB"
    assert model.data(model.index(0, 2), Qt.DisplayRole) == -42
    assert model.device_at(0) is replacement


def test_device_table_normalizes_mac_address_before_deduplication():
    model = DeviceTableModel()

    model.update_device(Device("first", "aa-bb-cc-dd-ee-ff"), Advertisement(None, -70))
    replacement = Device("second", "AA:BB:CC:DD:EE:FF")
    model.update_device(replacement, Advertisement(None, -40))

    assert model.rowCount() == 1
    assert model.data(model.index(0, 1), Qt.DisplayRole) == "AA:BB:CC:DD:EE:FF"
    assert model.device_at(0) is replacement


def test_device_table_preserves_last_known_name_when_later_packet_has_no_name():
    model = DeviceTableModel()

    model.update_device(Device("广播名称", "AA:BB"), Advertisement("广播名称", -50))
    model.update_device(Device("", "AA:BB"), Advertisement(None, -45))

    assert model.data(model.index(0, 0), Qt.DisplayRole) == "广播名称"
    assert model.data(model.index(0, 2), Qt.DisplayRole) == -45


def test_device_table_preserves_device_id_when_live_packet_has_no_device_id():
    model = DeviceTableModel()
    resolved = SimpleNamespace(
        name="设备", address="AA:BB", device_id="BluetoothLE#resolved-aa:bb"
    )
    live = SimpleNamespace(name="设备", address="AA:BB", device_id="")

    model.update_device(resolved, Advertisement(None, -60))
    model.update_device(live, Advertisement(None, -45))

    assert model.device_at(0).device_id == resolved.device_id
    assert model.data(model.index(0, 2), Qt.DisplayRole) == -45


def test_device_table_does_not_replace_complete_name_with_shorter_name():
    model = DeviceTableModel()

    model.update_device(Device("JGS_CHICKEN", "AA:BB"), Advertisement(None, -50))
    model.update_device(Device("CHICKEN", "AA:BB"), Advertisement("CHICKEN", -45))

    assert model.data(model.index(0, 0), Qt.DisplayRole) == "JGS_CHICKEN"


def test_device_table_keeps_complete_name_when_short_name_repeats():
    model = DeviceTableModel()
    address = "AA:BB"
    model.update_device(Device("JGS_CHICKEN", address), Advertisement(None, -50))

    for _ in range(3):
        model.update_device(Device("CHICKEN", address), Advertisement("CHICKEN", -45))

    assert model.data(model.index(0, 0), Qt.DisplayRole) == "JGS_CHICKEN"


def test_device_table_removes_devices_not_seen_within_maximum_age():
    now = [100.0]
    model = DeviceTableModel(clock=lambda: now[0])
    model.update_device(Device("旧设备", "AA:BB"), Advertisement(None, -70))
    now[0] += 16.0
    model.update_device(Device("活动设备", "CC:DD"), Advertisement(None, -40))

    removed = model.prune_stale(max_age_seconds=15.0)

    assert removed == 1
    assert model.rowCount() == 1
    assert model.data(model.index(0, 1), Qt.DisplayRole) == "CC:DD"


def test_device_table_reads_complete_name_from_raw_scan_response():
    class RawAdvertisement:
        local_name = ""

        def get_sections_by_type(self, data_type):
            if int(data_type) == 9:
                return [SimpleNamespace(data=b"JGS_CHICKEN_1.4.04")]
            return []

    raw_scan = SimpleNamespace(advertisement=RawAdvertisement())
    advertisement = Advertisement(None, -45)
    advertisement.platform_data = (object(), (None, raw_scan))
    model = DeviceTableModel()

    model.update_device(Device("", "AA:BB"), advertisement)

    assert model.data(model.index(0, 0), Qt.DisplayRole) == "JGS_CHICKEN_1.4.04"
