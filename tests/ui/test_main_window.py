import asyncio
from dataclasses import dataclass
import inspect
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView

from wch_ota.application.ota_events import OtaEvent, UpgradeStage, UpgradeStatus
from wch_ota.domain.firmware import FirmwareImage
from wch_ota.domain.models import ChipType, CurrentImageInfo, ImageType
from wch_ota.ui.main_window import MainWindow


@dataclass
class Device:
    name: str
    address: str


@dataclass
class Advertisement:
    local_name: str | None
    rssi: int


class FakeTransport:
    def __init__(self):
        self.is_connected = False
        self.connected_device = None
        self.callback = None
        self.stopped = 0
        self.disconnected = 0
        self.connect_calls = 0
        self.connect_started = asyncio.Event()
        self.connect_gate = None

    async def start_scan(self, callback):
        self.callback = callback

    async def stop_scan(self):
        self.stopped += 1

    async def connect(self, device):
        self.connect_calls += 1
        self.connect_started.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.connected_device = device
        self.is_connected = True

    async def disconnect(self):
        self.disconnected += 1
        self.is_connected = False


class FakeController:
    def __init__(self):
        self._event_callback = None
        self.cancelled = False
        self.upgrades = []
        self.info = CurrentImageInfo(ChipType.CH583, ImageType.A, 0x1000, 256)

    async def get_current_image_info(self):
        return self.info

    async def upgrade(self, firmware, info):
        self.upgrades.append((firmware, info))

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def window(qtbot):
    transport = FakeTransport()
    controller = FakeController()
    widget = MainWindow(transport=transport, controller=controller)
    qtbot.addWidget(widget)
    return widget, transport, controller


def test_window_contains_complete_controls_and_initial_state(window):
    widget, _, _ = window

    assert widget.device_view.model() is widget.device_proxy
    assert widget.scan_button.text() == "开始扫描"
    assert not hasattr(widget, "stop_scan_button")
    assert widget.connect_button.text() == "连接"
    assert widget.disconnect_button.text() == "断开"
    assert widget.connect_button.isHidden()
    assert widget.disconnect_button.isHidden()
    assert widget.info_button.text() == "获取信息"
    assert widget.firmware_path.isReadOnly()
    assert widget.start_button.text() == "开始升级"
    assert widget.cancel_button.text() == "取消"
    assert not widget.start_button.isEnabled()
    assert not widget.cancel_button.isEnabled()
    assert not widget.detailed_log_checkbox.isChecked()
    assert widget.clear_log_button.text() == "清空日志"
    assert widget.size().width() == 1200
    assert widget.size().height() == 800
    assert widget.device_view.verticalHeader().isHidden()
    header = widget.device_view.horizontalHeader()
    assert header.sectionResizeMode(0) == QHeaderView.Stretch
    assert header.sectionResizeMode(1) == QHeaderView.Interactive
    assert header.sectionResizeMode(2) == QHeaderView.Interactive
    assert header.sectionResizeMode(3) == QHeaderView.Interactive
    assert not header.stretchLastSection()
    original_width = widget.device_view.columnWidth(1)
    widget.device_view.setColumnWidth(1, original_width + 20)
    assert widget.device_view.columnWidth(1) == original_width + 20


def test_reference_layout_compacts_controls_and_gives_log_more_space(window):
    widget, _, _ = window

    assert widget.right_splitter.orientation() == Qt.Vertical
    assert widget.right_splitter.count() == 2
    assert widget.log_view.minimumHeight() >= 260
    assert widget.firmware_panel.sizePolicy().verticalPolicy().name == "Maximum"
    assert widget.firmware_path.minimumHeight() == widget.erase_address.minimumHeight()
    assert widget.browse_button.maximumWidth() <= 64
    assert widget.progress_bar.minimumWidth() == 320
    assert widget.progress_bar.maximumWidth() == 16777215
    progress_index = widget.progress_layout.indexOf(widget.progress_bar)
    assert widget.progress_layout.indexOf(widget.progress_text) == progress_index + 1
    assert widget.progress_text.minimumWidth() == 0
    assert widget.progress_text.alignment() & Qt.AlignLeft
    assert widget.progress_bar.minimumHeight() == 6
    assert widget.progress_bar.maximumHeight() == 6
    assert not widget.progress_bar.isTextVisible()
    assert widget.device_group.title() == "设备列表"
    assert widget.device_group.minimumWidth() >= 500
    assert widget.device_view.columnWidth(1) >= 150
    assert widget.device_view.columnWidth(3) <= 105
    assert widget.device_group.isAncestorOf(widget.scan_button)
    assert widget.device_group.isAncestorOf(widget.device_filter)
    assert widget.info_box.isAncestorOf(widget.info_button)
    assert widget.log_panel.isAncestorOf(widget.export_button)
    assert widget.log_panel.isAncestorOf(widget.clear_log_button)
    assert not widget.statusBar().isSizeGripEnabled()


def test_device_filter_matches_name_or_address(window, qtbot):
    widget, _, _ = window
    widget._on_device_detected(Device("Feeder_A", "11:22:33"), Advertisement(None, -55))
    widget._on_device_detected(Device("Sensor_B", "AA:BB:CC"), Advertisement(None, -60))

    widget.device_filter.setText("feeder")
    assert widget.device_proxy.rowCount() == 1
    assert widget.device_proxy.index(0, 0).data() == "Feeder_A"

    widget.device_filter.setText("aa:bb")
    assert widget.device_proxy.rowCount() == 1
    assert widget.device_proxy.index(0, 1).data() == "AA:BB:CC"


def test_each_device_row_contains_connect_and_disconnect_buttons(window):
    widget, _, _ = window
    widget._on_device_detected(Device("Feeder_A", "11:22:33"), Advertisement(None, -55))

    assert widget.device_model.columnCount() == 4
    action_widget = widget.device_view.indexWidget(widget.device_proxy.index(0, 3))
    assert action_widget is not None
    buttons = action_widget.findChildren(type(widget.scan_button))
    assert [button.text() for button in buttons] == ["连接", "断开"]
    assert all(button.maximumWidth() <= 48 for button in buttons)
    assert all(button.property("actionButton") for button in buttons)
    assert widget.device_group.isAncestorOf(action_widget)


@pytest.mark.asyncio
async def test_scan_button_toggles_start_and_stop(window):
    widget, transport, _ = window

    await widget.start_scan()
    assert widget.scan_button.text() == "停止扫描"

    await widget.stop_scan()
    assert widget.scan_button.text() == "开始扫描"
    assert transport.stopped == 1


@pytest.mark.asyncio
async def test_snapshot_scan_returns_button_to_idle(qtbot):
    transport = FakeTransport()
    transport.scan_is_continuous = False
    widget = MainWindow(transport=transport, controller=FakeController())
    qtbot.addWidget(widget)

    await widget.start_scan()

    assert widget.scan_button.text() == "开始扫描"
    assert not widget._scanning


def test_clear_log_button_clears_log_display(window, qtbot):
    widget, _, _ = window
    widget._append_log("需要清空的日志")

    qtbot.mouseClick(widget.clear_log_button, Qt.LeftButton)

    assert widget.log_view.toPlainText() == ""


def test_window_uses_blue_white_theme_and_primary_button_roles(window):
    widget, _, _ = window

    style = widget.styleSheet().lower()
    assert "#0078d4" in style
    assert "font-size: 13px" in style
    assert "min-height: 24px" in style
    assert "border-radius: 5px" in style
    assert "border-radius: 8px" in style
    assert "qheaderview::section" in style
    assert "qprogressbar::chunk" in style
    assert widget.scan_button.property("buttonRole") == "primary"
    assert widget.connect_button.property("buttonRole") == "primary"
    assert widget.start_button.property("buttonRole") == "primary"


def test_transport_diagnostics_are_hidden_by_default_and_can_be_enabled(window):
    widget, _, _ = window

    widget.handle_transport_trace("WCH DLL TX：测试诊断")

    assert "测试诊断" not in widget.log_view.toPlainText()

    widget.detailed_log_checkbox.setChecked(True)
    widget.handle_transport_trace("WCH DLL RX：详细诊断")

    assert "详细诊断" in widget.log_view.toPlainText()


@pytest.mark.asyncio
async def test_scan_callback_has_two_argument_python_signature(window):
    widget, transport, _ = window

    await widget.start_scan()

    signature = inspect.signature(transport.callback)
    signature.bind(Device("OTA", "11:22"), Advertisement(None, -55))


@pytest.mark.asyncio
async def test_selection_connects_using_original_ble_device(window):
    widget, transport, _ = window
    device = Device("OTA", "11:22")
    widget._on_device_detected(device, Advertisement(None, -55))
    widget.device_view.selectRow(0)

    await widget.connect_selected()

    assert transport.connected_device is device
    assert widget.disconnect_button.isEnabled()
    assert widget.info_button.isEnabled()
    assert not widget.scan_button.isEnabled()
    assert not widget.device_filter.isEnabled()
    assert "正在连接：11:22" in widget.log_view.toPlainText()


@pytest.mark.asyncio
async def test_connect_keeps_scanner_active_until_connection_succeeds(qtbot):
    order = []

    class OrderedTransport(FakeTransport):
        async def connect(self, device):
            order.append("connect")
            await super().connect(device)

        async def stop_scan(self):
            order.append("stop_scan")
            await super().stop_scan()

    transport = OrderedTransport()
    widget = MainWindow(transport=transport, controller=FakeController())
    qtbot.addWidget(widget)
    widget._on_device_detected(Device("OTA", "11:22"), Advertisement(None, -55))
    widget.device_view.selectRow(0)

    await widget.connect_selected()

    assert order[:2] == ["connect", "stop_scan"]


def test_empty_exception_log_includes_exception_type(window):
    widget, _, _ = window

    widget._report_error("连接失败", TimeoutError())

    assert "连接失败：操作超时（TimeoutError）" in widget.log_view.toPlainText()


def test_device_info_displays_target_image_and_chip_specific_size_meaning(window):
    widget, _, _ = window

    widget._apply_device_info(
        CurrentImageInfo(ChipType.CH583, ImageType.B, 221184, 4096)
    )
    assert widget.target_image_label.text() == "A"
    assert widget.image_value_title.text() == "镜像最大大小"
    assert widget.offset_label.text() == "221184 字节（0x00036000）"

    widget._apply_device_info(
        CurrentImageInfo(ChipType.CH579, ImageType.A, 0x1200, 256)
    )
    assert widget.target_image_label.text() == "B"
    assert widget.image_value_title.text() == "镜像偏移"
    assert widget.offset_label.text() == "0x00001200"


@pytest.mark.asyncio
async def test_concurrent_connect_runs_once_and_keeps_success_state(window):
    widget, transport, _ = window
    device = Device("OTA", "11:22")
    widget._on_device_detected(device, Advertisement(None, -55))
    widget.device_view.selectRow(0)
    transport.connect_gate = asyncio.Event()

    first = asyncio.create_task(widget.connect_selected())
    await transport.connect_started.wait()
    assert not widget.scan_button.isEnabled()
    assert not widget.connect_button.isEnabled()
    assert not widget.device_view.isEnabled()
    second = asyncio.create_task(widget.connect_selected())
    await asyncio.sleep(0)
    transport.connect_gate.set()
    await asyncio.gather(first, second)

    assert transport.connect_calls == 1
    assert widget._connected
    assert widget.disconnect_button.isEnabled()

    await widget.connect_selected()
    assert transport.connect_calls == 1
    assert widget._connected


@pytest.mark.asyncio
async def test_bin_address_and_hex_automatic_address_are_passed_to_upgrade(
    window, tmp_path: Path
):
    widget, transport, controller = window
    transport.is_connected = True
    widget._set_connected(True)
    widget._apply_device_info(controller.info)
    binary = tmp_path / "app.bin"
    binary.write_bytes(bytes(0x4000) + b"\x01\x02")
    widget.set_firmware_path(binary)
    widget.erase_address.setText("0x4000")

    await widget.start_upgrade()

    assert controller.upgrades[-1][0].start_address == 0x4000
    assert controller.upgrades[-1][0].data == b"\x01\x02"
    hex_file = Path(__file__).parents[1] / "fixtures" / "firmware_contiguous.hex"
    widget.set_firmware_path(hex_file)
    assert not widget.erase_address.isEnabled()
    await widget.start_upgrade()
    assert controller.upgrades[-1][0].start_address == 0x10010


@pytest.mark.asyncio
async def test_current_image_b_upgrades_explicit_target_image_a(window, tmp_path: Path):
    widget, transport, controller = window
    transport.is_connected = True
    controller.info = CurrentImageInfo(ChipType.CH583, ImageType.B, 0x36000, 4096)
    widget._set_connected(True)
    widget._apply_device_info(controller.info)
    firmware = tmp_path / "image-a.hex"
    firmware.write_text(":020000040000FA\n:0410000001020304E2\n:00000001FF\n")
    widget.set_firmware_path(firmware)

    await widget.start_upgrade()

    _, target_info = controller.upgrades[-1]
    assert target_info.image is ImageType.A
    assert widget.current_info.image is ImageType.B
    assert "目标 Image A" in widget.log_view.toPlainText()


@pytest.mark.asyncio
async def test_ch579_bin_ignores_manual_address_and_keeps_complete_file(window, tmp_path):
    widget, transport, controller = window
    transport.is_connected = True
    controller.info = CurrentImageInfo(ChipType.CH579, ImageType.A, 0x1200, 256)
    widget._set_connected(True)
    widget._apply_device_info(controller.info)
    binary = tmp_path / "ch579.bin"
    binary.write_bytes(b"\x01\x02\x03")
    widget.set_firmware_path(binary)
    widget.erase_address.setText("0x4000")

    await widget.start_upgrade()

    assert controller.upgrades[-1][0] == FirmwareImage(0, b"\x01\x02\x03")


def test_ota_event_updates_progress_log_and_restores_controls(window, tmp_path):
    widget, transport, _ = window
    transport.is_connected = True
    widget._set_connected(True)
    firmware = tmp_path / "firmware.bin"
    firmware.write_bytes(b"\x01")
    widget.set_firmware_path(firmware)
    widget._set_upgrading(True)

    widget._on_ota_event(
        OtaEvent(UpgradeStage.PROGRAM, UpgradeStatus.RUNNING, 25, 100, "写入中")
    )

    assert widget.stage_label.text() == "编程 · 运行中"
    assert widget.progress_bar.maximum() == 100
    assert widget.progress_bar.value() == 25
    assert widget.progress_text.text() == "25 / 100 字节"
    assert "写入中" in widget.log_view.toPlainText()
    assert not widget.scan_button.isEnabled()
    assert widget.cancel_button.isEnabled()

    widget._on_ota_event(
        OtaEvent(UpgradeStage.END, UpgradeStatus.SUCCESS, 100, 100, "升级完成")
    )
    assert not widget.scan_button.isEnabled()
    assert not widget.cancel_button.isEnabled()


@pytest.mark.asyncio
async def test_failed_info_read_invalidates_previous_device_info(window):
    widget, transport, controller = window
    transport.is_connected = True
    widget._set_connected(True)
    widget._apply_device_info(controller.info)

    async def fail_info():
        raise RuntimeError("设备没有响应")

    controller.get_current_image_info = fail_info
    await widget.get_device_info()

    assert widget.current_info is None
    assert widget.chip_label.text() == "-"
    assert widget.image_label.text() == "-"
    assert widget.target_image_label.text() == "-"
    assert not widget.start_button.isEnabled()


@pytest.mark.asyncio
async def test_shutdown_stops_scan_and_disconnects(window):
    widget, transport, _ = window

    await widget.shutdown()

    assert transport.stopped == 1
    assert transport.disconnected == 1


def test_start_requires_parseable_existing_firmware(window, tmp_path):
    widget, transport, controller = window
    transport.is_connected = True
    widget._set_connected(True)
    widget._apply_device_info(controller.info)

    widget.set_firmware_path(tmp_path / "missing.bin")
    assert not widget.start_button.isEnabled()

    bad_hex = tmp_path / "bad.hex"
    bad_hex.write_text("not intel hex", encoding="ascii")
    widget.set_firmware_path(bad_hex)
    assert not widget.start_button.isEnabled()

    binary = tmp_path / "valid.bin"
    binary.write_bytes(b"\x01")
    widget.set_firmware_path(binary)
    assert widget.start_button.isEnabled()
    widget.erase_address.setText("1234")
    widget.erase_address.editingFinished.emit()
    assert not widget.start_button.isEnabled()


def test_unexpected_disconnect_restores_idle_disconnected_state(window):
    widget, _, _ = window
    widget._set_connected(True)
    widget._set_upgrading(True)

    widget.handle_transport_disconnected(object())

    assert not widget.disconnect_button.isEnabled()
    assert not widget.cancel_button.isEnabled()
    assert "设备意外断开" in widget.log_view.toPlainText()


class CloseEvent:
    def __init__(self):
        self.accepted = False
        self.ignored = False

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.ignored = True


@pytest.mark.asyncio
async def test_close_waits_for_normal_cleanup_and_reuses_one_task(window):
    widget, transport, _ = window
    first = CloseEvent()
    second = CloseEvent()

    widget.closeEvent(first)
    task = widget._shutdown_task
    widget.closeEvent(second)

    assert first.ignored and second.ignored
    assert widget._shutdown_task is task
    await task
    assert widget._shutdown_complete
    assert transport.stopped == 1
    assert transport.disconnected == 1


@pytest.mark.asyncio
async def test_close_times_out_cleanup_after_three_seconds(window, monkeypatch):
    widget, _, _ = window
    captured = {}

    async def timeout(awaitable, seconds):
        captured["seconds"] = seconds
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    event = CloseEvent()
    widget.closeEvent(event)
    await widget._shutdown_task

    assert event.ignored
    assert captured["seconds"] == 3.0
    assert widget._shutdown_complete
    assert "关闭清理超时" in widget.log_view.toPlainText()


@pytest.mark.asyncio
async def test_shutdown_waits_for_upgrade_before_disconnect(qtbot, tmp_path):
    order = []
    upgrade_started = asyncio.Event()
    finish_upgrade = asyncio.Event()

    class OrderedTransport(FakeTransport):
        async def stop_scan(self):
            order.append("stop_scan")

        async def disconnect(self):
            order.append("disconnect")
            self.is_connected = False

    class OrderedController(FakeController):
        async def upgrade(self, firmware, info):
            order.append("upgrade_started")
            upgrade_started.set()
            await finish_upgrade.wait()
            order.append("upgrade_finished")

        def cancel(self):
            order.append("cancel")
            finish_upgrade.set()

    transport = OrderedTransport()
    controller = OrderedController()
    transport.is_connected = True
    widget = MainWindow(transport=transport, controller=controller)
    qtbot.addWidget(widget)
    widget._set_connected(True)
    widget._apply_device_info(controller.info)
    binary = tmp_path / "app.bin"
    binary.write_bytes(b"\x01")
    widget.set_firmware_path(binary)

    upgrade_task = asyncio.create_task(widget.start_upgrade())
    await upgrade_started.wait()
    assert widget._upgrade_task is upgrade_task
    await widget.shutdown()

    assert order == ["upgrade_started", "cancel", "upgrade_finished", "stop_scan", "disconnect"]
    assert upgrade_task.done()
    assert widget._upgrade_task is None


@pytest.mark.asyncio
async def test_concurrent_upgrade_early_return_does_not_clear_owner_state(qtbot, tmp_path):
    started = asyncio.Event()
    finish = asyncio.Event()

    class BlockingController(FakeController):
        async def upgrade(self, firmware, info):
            started.set()
            await finish.wait()

    transport = FakeTransport()
    controller = BlockingController()
    transport.is_connected = True
    widget = MainWindow(transport=transport, controller=controller)
    qtbot.addWidget(widget)
    widget._set_connected(True)
    widget._apply_device_info(controller.info)
    binary = tmp_path / "app.bin"
    binary.write_bytes(b"\x01")
    widget.set_firmware_path(binary)

    owner = asyncio.create_task(widget.start_upgrade())
    await started.wait()
    await widget.start_upgrade()

    assert widget._upgrade_task is owner
    assert widget._upgrading
    assert widget.cancel_button.isEnabled()
    finish.set()
    await owner
    assert widget._upgrade_task is None
    assert not widget._upgrading
