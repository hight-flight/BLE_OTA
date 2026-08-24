"""WCH BLE OTA 主窗口。"""

import asyncio
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import re
from typing import Any

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QFormLayout, QGroupBox, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QSplitter, QTableView, QVBoxLayout, QWidget,
)
from qasync import asyncSlot

from wch_ota.application.ota_events import OtaEvent, UpgradeStatus
from wch_ota.domain.firmware import parse_firmware
from wch_ota.domain.models import ChipType, ImageType

from .device_table import DeviceTableModel
from .firmware_dialog import FirmwarePanel
from .log_export import export_log
from .theme import BLUE_WHITE_STYLESHEET


_TARGET_IMAGE = {
    ImageType.A: ImageType.B,
    ImageType.B: ImageType.A,
}


class MainWindow(QMainWindow):
    device_detected = Signal(object, object)
    ota_event_received = Signal(object)
    transport_disconnected = Signal(object)
    transport_trace_received = Signal(str)

    def __init__(self, *, transport: Any, controller: Any) -> None:
        super().__init__()
        self.transport = transport
        self.controller = controller
        self.current_info = None
        self._connected = bool(transport.is_connected)
        self._connected_address = ""
        self._scanning = False
        self._upgrading = False
        self._connection_busy = False
        self._connection_lock = asyncio.Lock()
        self._disconnecting = False
        self._firmware_valid = False
        self._firmware_cache_key: tuple[Any, ...] | None = None
        self._firmware_cache = None
        self._shutdown_task: asyncio.Task | None = None
        self._upgrade_task: asyncio.Task | None = None
        self._shutdown_complete = False
        self.setWindowTitle("WCH BLE OTA 工具")
        self.resize(1200, 800)
        self._build_ui()
        self.setStyleSheet(BLUE_WHITE_STYLESHEET)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage("WCH BLE OTA · 就绪")
        self.device_detected.connect(self._on_device_detected)
        self.ota_event_received.connect(self._on_ota_event)
        self.transport_disconnected.connect(self._on_transport_disconnected)
        self.transport_trace_received.connect(self._on_transport_trace)
        self.controller._event_callback = self.ota_event_received.emit
        self._refresh_controls()
        self.hide()

    def handle_transport_trace(self, message: str) -> None:
        """将任意工作线程产生的传输诊断安全转交给 Qt 主线程。"""
        self.transport_trace_received.emit(message)

    @Slot(str)
    def _on_transport_trace(self, message: str) -> None:
        if self.detailed_log_checkbox.isChecked():
            self._append_log(message)

    def _build_ui(self) -> None:
        self.scan_button = QPushButton("开始扫描")
        self.export_button = QPushButton("导出日志")
        self.scan_button.setProperty("buttonRole", "primary")

        self.device_model = DeviceTableModel(self)
        self.device_proxy = QSortFilterProxyModel(self)
        self.device_proxy.setSourceModel(self.device_model)
        self.device_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.device_proxy.setFilterKeyColumn(-1)
        self.device_view = QTableView()
        self.device_view.setModel(self.device_proxy)
        self.device_view.setSelectionBehavior(QTableView.SelectRows)
        self.device_view.setSelectionMode(QTableView.SingleSelection)
        self.device_view.setAlternatingRowColors(True)
        self.device_view.setWordWrap(False)
        self.device_view.verticalHeader().hide()
        self.device_view.verticalHeader().setDefaultSectionSize(38)
        device_header = self.device_view.horizontalHeader()
        device_header.setSectionResizeMode(QHeaderView.Interactive)
        device_header.setSectionResizeMode(0, QHeaderView.Stretch)
        device_header.setMinimumSectionSize(45)
        device_header.setStretchLastSection(False)
        self.device_view.setColumnWidth(0, 135)
        self.device_view.setColumnWidth(1, 155)
        self.device_view.setColumnWidth(2, 45)
        self.device_view.setColumnWidth(3, 105)
        self.device_group = QGroupBox("设备列表")
        self.device_group.setMinimumWidth(500)
        self.device_filter = QLineEdit()
        self.device_filter.setPlaceholderText("按名称/地址过滤")
        self.device_filter.setClearButtonEnabled(True)
        self.device_filter.setProperty("filterField", True)
        device_toolbar = QHBoxLayout()
        device_toolbar.setContentsMargins(0, 0, 0, 0)
        device_toolbar.setSpacing(6)
        device_toolbar.addWidget(self.device_filter, 1)
        device_toolbar.addWidget(self.scan_button)
        device_layout = QVBoxLayout(self.device_group)
        device_layout.setContentsMargins(8, 6, 8, 8)
        device_layout.setSpacing(6)
        device_layout.addLayout(device_toolbar)
        device_layout.addWidget(self.device_view, 1)

        self.connect_button = QPushButton("连接", self)
        self.disconnect_button = QPushButton("断开", self)
        self.connect_button.hide()
        self.disconnect_button.hide()
        self.info_button = QPushButton("获取信息")
        self.connect_button.setProperty("buttonRole", "primary")

        self.chip_label = QLabel("-")
        self.image_label = QLabel("-")
        self.target_image_label = QLabel("-")
        self.image_value_title = QLabel("镜像最大大小")
        self.offset_label = QLabel("-")
        self.block_label = QLabel("-")
        self.info_box = QGroupBox("设备镜像信息")
        info_layout = QVBoxLayout(self.info_box)
        info_layout.setSpacing(4)
        info_form = QFormLayout()
        info_form.addRow("芯片", self.chip_label)
        info_form.addRow("当前镜像", self.image_label)
        info_form.addRow("目标镜像", self.target_image_label)
        info_form.addRow(self.image_value_title, self.offset_label)
        info_form.addRow("块大小", self.block_label)
        info_actions = QHBoxLayout()
        info_actions.addStretch(1)
        info_actions.addWidget(self.info_button)
        info_layout.addLayout(info_form)
        info_layout.addLayout(info_actions)

        self.firmware_panel = FirmwarePanel()
        self.firmware_path = self.firmware_panel.path_edit
        self.browse_button = self.firmware_panel.browse_button
        self.erase_address = self.firmware_panel.erase_address
        self.start_button = QPushButton("开始升级")
        self.cancel_button = QPushButton("取消")
        self.start_button.setProperty("buttonRole", "primary")
        upgrade_buttons = QHBoxLayout()
        upgrade_buttons.addWidget(self.start_button)
        upgrade_buttons.addWidget(self.cancel_button)
        self.stage_label = QLabel("等待")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setMinimumWidth(320)
        self.progress_bar.setFixedHeight(6)
        self.progress_bar.setTextVisible(False)
        self.progress_text = QLabel("0 / 0 字节")
        self.progress_text.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.progress_layout = QHBoxLayout()
        self.progress_layout.setContentsMargins(0, 0, 0, 0)
        self.progress_layout.setSpacing(6)
        self.progress_layout.addWidget(self.stage_label)
        self.progress_layout.addWidget(self.progress_bar, 1)
        self.progress_layout.addWidget(self.progress_text)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(260)
        self.detailed_log_checkbox = QCheckBox("详细日志")
        self.clear_log_button = QPushButton("清空日志")
        log_header = QHBoxLayout()
        log_header.addWidget(QLabel("日志窗口"))
        log_header.addStretch(1)
        log_header.addWidget(self.detailed_log_checkbox)
        log_actions = QHBoxLayout()
        log_actions.addStretch(1)
        log_actions.addWidget(self.export_button)
        log_actions.addWidget(self.clear_log_button)

        right_controls = QWidget()
        right_controls_layout = QVBoxLayout(right_controls)
        right_controls_layout.setContentsMargins(0, 0, 0, 0)
        right_controls_layout.setSpacing(6)
        right_controls_layout.addWidget(self.info_box)
        right_controls_layout.addWidget(self.firmware_panel)
        right_controls_layout.addLayout(upgrade_buttons)
        right_controls_layout.addLayout(self.progress_layout)

        self.log_panel = QWidget()
        log_layout = QVBoxLayout(self.log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addLayout(log_header)
        log_layout.addWidget(self.log_view, 1)
        log_layout.addLayout(log_actions)

        self.right_splitter = QSplitter(Qt.Vertical)
        self.right_splitter.addWidget(right_controls)
        self.right_splitter.addWidget(self.log_panel)
        self.right_splitter.setChildrenCollapsible(False)
        self.right_splitter.setSizes([330, 390])
        self.right_splitter.setStretchFactor(0, 1)
        self.right_splitter.setStretchFactor(1, 2)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.addWidget(self.device_group)
        self.main_splitter.addWidget(self.right_splitter)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setSizes([520, 680])
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(self.main_splitter, 1)
        self.setCentralWidget(central)

        self.scan_button.clicked.connect(self._scan_slot)
        self.connect_button.clicked.connect(self._connect_slot)
        self.disconnect_button.clicked.connect(self._disconnect_slot)
        self.info_button.clicked.connect(self._info_slot)
        self.browse_button.clicked.connect(self.browse_firmware)
        self.erase_address.editingFinished.connect(self._validate_firmware)
        self.start_button.clicked.connect(self._upgrade_slot)
        self.cancel_button.clicked.connect(self.cancel_upgrade)
        self.export_button.clicked.connect(self.export_logs)
        self.clear_log_button.clicked.connect(self.log_view.clear)
        self.device_filter.textChanged.connect(self._apply_device_filter)
        self.device_view.selectionModel().selectionChanged.connect(self._refresh_controls)

    @asyncSlot()
    async def _scan_slot(self) -> None:
        if self._scanning:
            await self.stop_scan()
        else:
            await self.start_scan()

    @asyncSlot()
    async def _connect_slot(self) -> None:
        await self.connect_selected()

    @asyncSlot()
    async def _disconnect_slot(self) -> None:
        await self.disconnect()

    @asyncSlot()
    async def _info_slot(self) -> None:
        await self.get_device_info()

    @asyncSlot()
    async def _upgrade_slot(self) -> None:
        await self.start_upgrade()

    async def start_scan(self) -> None:
        self._set_scanning(True)
        try:
            self.device_model.clear()
            await self.transport.start_scan(self._forward_scan_result)
            self._append_log("开始扫描 BLE 设备")
        except Exception as error:
            self._set_scanning(False)
            self._report_error("扫描失败", error)
        finally:
            if not getattr(self.transport, "scan_is_continuous", True):
                self._set_scanning(False)

    def _forward_scan_result(self, device: Any, advertisement: Any) -> None:
        """用可检查签名的 Python 回调跨越 Bleak 与 Qt Signal 边界。"""
        self.device_detected.emit(device, advertisement)

    async def stop_scan(self) -> None:
        try:
            await self.transport.stop_scan()
            self._set_scanning(False)
            self._append_log("已停止扫描")
        except Exception as error:
            self._report_error("停止扫描失败", error)

    @Slot(object, object)
    def _on_device_detected(self, device: Any, advertisement: Any) -> None:
        self.device_model.update_device(device, advertisement)
        self._sync_device_action_widgets()
        self._refresh_controls()

    @Slot(str)
    def _apply_device_filter(self, text: str) -> None:
        self.device_proxy.setFilterFixedString(text)
        self._sync_device_action_widgets()

    def _sync_device_action_widgets(self) -> None:
        for row in range(self.device_proxy.rowCount()):
            index = self.device_proxy.index(row, 3)
            if self.device_view.indexWidget(index) is not None:
                continue
            address = str(self.device_proxy.index(row, 1).data() or "")
            action_widget = QWidget()
            action_widget.setProperty("actionCell", True)
            action_layout = QHBoxLayout(action_widget)
            action_layout.setContentsMargins(2, 2, 2, 2)
            action_layout.setSpacing(4)
            connect_button = QPushButton("连接")
            disconnect_button = QPushButton("断开")
            for button in (connect_button, disconnect_button):
                button.setProperty("buttonRole", "primary")
                button.setProperty("compact", True)
                button.setProperty("actionButton", True)
                button.setMaximumWidth(48)
                action_layout.addWidget(button)
            connect_button.clicked.connect(
                lambda _checked=False, value=address: self._activate_device_action(value, True)
            )
            disconnect_button.clicked.connect(
                lambda _checked=False, value=address: self._activate_device_action(value, False)
            )
            self.device_view.setIndexWidget(index, action_widget)
        self._refresh_device_action_widgets()

    def _activate_device_action(self, address: str, connect: bool) -> None:
        for row in range(self.device_proxy.rowCount()):
            if str(self.device_proxy.index(row, 1).data() or "") == address:
                self.device_view.selectRow(row)
                break
        (self.connect_button if connect else self.disconnect_button).click()

    def _refresh_device_action_widgets(self) -> None:
        idle = not self._upgrading and not self._connection_busy
        for row in range(self.device_proxy.rowCount()):
            index = self.device_proxy.index(row, 3)
            action_widget = self.device_view.indexWidget(index)
            if action_widget is None:
                continue
            address = str(self.device_proxy.index(row, 1).data() or "")
            buttons = action_widget.findChildren(QPushButton)
            if len(buttons) != 2:
                continue
            buttons[0].setEnabled(idle and not self._connected)
            buttons[1].setEnabled(
                idle and self._connected and address == self._connected_address
            )

    async def connect_selected(self) -> None:
        if self._connection_lock.locked():
            self._append_log("连接操作正在进行中")
            return
        if self._connected or self.transport.is_connected:
            self._set_connected(True)
            self._append_log("设备已经连接")
            return
        indexes = self.device_view.selectionModel().selectedRows()
        if not indexes:
            self._append_log("请先选择设备")
            return
        source_index = self.device_proxy.mapToSource(indexes[0])
        device = self.device_model.device_at(source_index.row())
        async with self._connection_lock:
            self._connection_busy = True
            self._refresh_controls()
            try:
                self._append_log(f"正在连接：{getattr(device, 'address', '')}")
                await self.transport.connect(device)
                self._connected_address = str(getattr(device, "address", ""))
                self._set_connected(True)
                try:
                    await self.transport.stop_scan()
                    self._set_scanning(False)
                except Exception as error:
                    self._report_error("连接成功，但停止扫描失败", error)
                self._append_log(f"已连接：{getattr(device, 'address', '')}")
                properties = ", ".join(
                    getattr(self.transport, "ota_characteristic_properties", ())
                )
                write_mode = (
                    "支持有响应写入，OTA 按 Android 使用无响应写入"
                    if getattr(self.transport, "supports_write_with_response", False)
                    else "仅支持无响应写入"
                )
                mtu = getattr(self.transport, "effective_mtu", "未知")
                backend = getattr(self.transport, "backend_name", "自定义")
                self._append_log(
                    f"蓝牙后端={backend}；OTA 特征 FEE1："
                    f"{properties or '属性未知'}；{write_mode}；"
                    f"MTU={mtu}"
                )
                await self.get_device_info()
            except Exception as error:
                self._set_connected(False)
                self._report_error("连接失败", error)
            finally:
                self._connection_busy = False
                self._refresh_controls()

    async def disconnect(self) -> None:
        self._disconnecting = True
        try:
            await self.transport.disconnect()
            self._set_connected(False)
            self._append_log("已断开设备")
        except Exception as error:
            self._report_error("断开失败", error)
        finally:
            self._disconnecting = False

    def handle_transport_disconnected(self, client: Any) -> None:
        """可从 Bleak 回调线程安全调用的公开断开入口。"""
        self.transport_disconnected.emit(client)

    @Slot(object)
    def _on_transport_disconnected(self, _client: Any) -> None:
        if self._disconnecting or self._shutdown_complete:
            return
        if self._upgrading:
            self.controller.cancel()
        self._set_upgrading(False)
        self._set_connected(False)
        self._append_log("设备意外断开")

    async def get_device_info(self) -> None:
        try:
            info = await self.controller.get_current_image_info()
            self._apply_device_info(info)
            self._append_log("已读取设备镜像信息")
        except Exception as error:
            self._invalidate_device_info()
            self._report_error("获取信息失败", error)

    def _invalidate_device_info(self) -> None:
        self.current_info = None
        self.chip_label.setText("-")
        self.image_label.setText("-")
        self.target_image_label.setText("-")
        self.offset_label.setText("-")
        self.block_label.setText("-")
        self._refresh_controls()

    def _apply_device_info(self, info: Any) -> None:
        self.current_info = info
        self.chip_label.setText(info.chip.value)
        self.image_label.setText(info.image.value)
        target_image = _TARGET_IMAGE.get(info.image)
        self.target_image_label.setText(target_image.value if target_image else "-")
        if info.chip is ChipType.CH579:
            self.image_value_title.setText("镜像偏移")
            self.offset_label.setText(f"0x{info.offset:08X}")
        else:
            self.image_value_title.setText("镜像最大大小")
            self.offset_label.setText(f"{info.offset} 字节（0x{info.offset:08X}）")
        self.block_label.setText(f"{info.block_size} 字节")
        self._refresh_controls()

    @Slot()
    def browse_firmware(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择固件", "", "固件 (*.bin *.hex);;BIN (*.bin);;Intel HEX (*.hex)")
        if path:
            self.set_firmware_path(path)

    def set_firmware_path(self, file_path: str | Path) -> bool:
        path = Path(file_path)
        if path.suffix.lower() not in {".bin", ".hex"}:
            self._append_log("仅支持 BIN 或 HEX 固件文件")
            return False
        self.firmware_path.setText(str(path))
        self._firmware_cache_key = None
        self._validate_firmware()
        return True

    @Slot()
    def _validate_firmware(self) -> None:
        self._firmware_valid = self._get_validated_firmware() is not None
        self._refresh_controls()

    def _get_validated_firmware(self):
        path = Path(self.firmware_path.text())
        suffix = path.suffix.lower()
        erase_address = None
        if suffix == ".bin":
            address = self.erase_address.text()
            if re.fullmatch(r"0[xX][0-9A-Fa-f]{1,8}", address) is None:
                self._firmware_cache_key = None
                self._firmware_cache = None
                return None
            erase_address = int(address, 16)
            if self.current_info is not None and self.current_info.chip is ChipType.CH579:
                erase_address = 0
        elif suffix != ".hex":
            return None
        try:
            stat = path.stat()
        except OSError:
            self._firmware_cache_key = None
            self._firmware_cache = None
            return None
        key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size, erase_address)
        if key == self._firmware_cache_key:
            return self._firmware_cache
        try:
            firmware = parse_firmware(path, erase_address=erase_address)
        except Exception:
            firmware = None
        self._firmware_cache_key = key
        self._firmware_cache = firmware
        return firmware

    async def start_upgrade(self) -> None:
        if self.current_info is None:
            self._append_log("请先获取设备信息")
            return
        if self._upgrade_task is not None and not self._upgrade_task.done():
            self._append_log("升级操作正在进行中")
            return
        path = Path(self.firmware_path.text())
        owner = False
        try:
            firmware = self._get_validated_firmware()
            self._firmware_valid = firmware is not None
            self._refresh_controls()
            if firmware is None:
                raise ValueError("固件文件或 BIN 擦除地址无效")
            current_task = asyncio.current_task()
            self._upgrade_task = current_task
            owner = True
            self._set_upgrading(True)
            target_image = _TARGET_IMAGE.get(self.current_info.image)
            if target_image is None:
                raise ValueError("设备当前 Image 类型无效，无法确定升级目标")
            self._append_log(
                f"开始升级：{path.name}，{len(firmware.data)} 字节；"
                f"目标 Image {target_image.value}"
            )
            target_info = replace(self.current_info, image=target_image)
            await self.controller.upgrade(firmware, target_info)
        except Exception as error:
            self._report_error("升级失败", error)
        finally:
            if owner:
                self._set_upgrading(False)
                self._upgrade_task = None

    @Slot()
    def cancel_upgrade(self) -> None:
        self.controller.cancel()
        self._append_log("正在请求取消升级")

    @Slot(object)
    def _on_ota_event(self, event: OtaEvent) -> None:
        self.stage_label.setText(f"{event.stage.value} · {event.status.value}")
        self.progress_bar.setRange(0, max(1, event.total))
        self.progress_bar.setValue(min(event.progress, max(1, event.total)))
        self.progress_text.setText(f"{event.progress} / {event.total} 字节")
        if event.message:
            self._append_log(event.message)
        self._set_upgrading(event.status is UpgradeStatus.RUNNING)

    def _set_connected(self, connected: bool) -> None:
        self._connected = connected
        if not connected:
            self._invalidate_device_info()
            self._connected_address = ""
        self._refresh_controls()

    def _set_scanning(self, scanning: bool) -> None:
        self._scanning = scanning
        self.scan_button.setText("停止扫描" if scanning else "开始扫描")
        self._refresh_controls()

    def _set_upgrading(self, upgrading: bool) -> None:
        self._upgrading = upgrading
        self._refresh_controls()

    def _refresh_controls(self, *_args) -> None:
        selected = bool(self.device_view.selectionModel().selectedRows())
        idle = not self._upgrading and not self._connection_busy
        self.device_view.setEnabled(idle)
        self.scan_button.setEnabled(idle and not self._connected)
        self.device_filter.setEnabled(idle and not self._connected)
        self.connect_button.setEnabled(idle and not self._connected and selected)
        self.disconnect_button.setEnabled(idle and self._connected)
        self.info_button.setEnabled(idle and self._connected)
        self._refresh_device_action_widgets()
        self.browse_button.setEnabled(idle)
        self.erase_address.setEnabled(
            idle
            and self.firmware_path.text().lower().endswith(".bin")
            and (
                self.current_info is None
                or self.current_info.chip is not ChipType.CH579
            )
        )
        self.start_button.setEnabled(idle and self._connected and self._firmware_valid and self.current_info is not None)
        self.cancel_button.setEnabled(self._upgrading)

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

    def _report_error(self, title: str, error: Exception) -> None:
        detail = str(error).strip()
        if not detail:
            detail = (
                "操作超时（TimeoutError）"
                if isinstance(error, TimeoutError)
                else type(error).__name__
            )
        self._append_log(f"{title}：{detail}")

    @Slot()
    def export_logs(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "导出日志", "ota.log", "日志 (*.log *.txt)")
        if not path:
            return
        try:
            export_log(path, self.log_view.toPlainText())
            self._append_log(f"日志已导出：{path}")
        except OSError as error:
            QMessageBox.warning(self, "导出失败", str(error))

    async def shutdown(self) -> None:
        try:
            self.controller.cancel()
        except Exception as error:
            self._append_log(f"取消升级失败：{error}")

        upgrade_task = self._upgrade_task
        if (
            upgrade_task is not None
            and upgrade_task is not asyncio.current_task()
            and not upgrade_task.done()
        ):
            try:
                await asyncio.shield(upgrade_task)
            except asyncio.CancelledError:
                if not upgrade_task.cancelled():
                    raise
            except Exception as error:
                self._append_log(f"等待升级结束失败：{error}")

        for operation in (self.transport.stop_scan, self.transport.disconnect):
            try:
                await operation()
            except Exception as error:
                self._append_log(f"关闭清理失败：{error}")

    async def _shutdown_and_close(self) -> None:
        try:
            await asyncio.wait_for(self.shutdown(), 3.0)
        except TimeoutError:
            self._append_log("关闭清理超时，窗口将强制关闭")
        except Exception as error:
            self._append_log(f"关闭清理失败：{error}")
        finally:
            self._shutdown_complete = True
            self.close()

    def closeEvent(self, event) -> None:
        if self._shutdown_complete:
            event.accept()
            return
        event.ignore()
        if self._shutdown_task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._append_log("关闭清理无法启动：异步事件循环未运行")
            self._shutdown_complete = True
            self.close()
            return
        self._shutdown_task = loop.create_task(self._shutdown_and_close())
