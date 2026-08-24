"""固件选择控件。"""

from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import (
    QButtonGroup, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QPushButton,
    QSizePolicy, QWidget,
)

from wch_ota.domain.models import ImageType


class FirmwarePanel(QGroupBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("固件", parent)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.setProperty("compact", True)
        self.browse_button.setMaximumWidth(64)
        path_layout = QHBoxLayout()
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.setSpacing(6)
        path_layout.addWidget(self.path_edit, 1)
        path_layout.addWidget(self.browse_button)
        path_widget = QWidget()
        path_widget.setLayout(path_layout)

        self.image_a_button = QPushButton("IMAGEA")
        self.image_b_button = QPushButton("IMAGEB")
        self.image_iap_button = QPushButton("IMAGE_IAP")
        self.image_group = QButtonGroup(self)
        self.image_group.setExclusive(True)
        image_layout = QHBoxLayout()
        image_layout.setContentsMargins(0, 0, 0, 0)
        image_layout.setSpacing(6)
        for button in (
            self.image_a_button,
            self.image_b_button,
            self.image_iap_button,
        ):
            button.setCheckable(True)
            button.setProperty("compact", True)
            button.setProperty("targetImageButton", True)
            self.image_group.addButton(button)
            image_layout.addWidget(button)
        self.image_a_button.setChecked(True)
        image_widget = QWidget()
        image_widget.setLayout(image_layout)

        self.erase_address = QLineEdit("0x00000000")
        self.erase_address.setValidator(QRegularExpressionValidator(QRegularExpression(r"0[xX][0-9A-Fa-f]{1,8}")))
        layout = QFormLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(5)
        layout.addRow("目标类型", image_widget)
        layout.addRow("固件文件", path_widget)
        layout.addRow("BIN 擦除地址", self.erase_address)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)

    def target_image(self) -> ImageType:
        if self.image_iap_button.isChecked():
            return ImageType.IAP
        if self.image_b_button.isChecked():
            return ImageType.B
        return ImageType.A

    def set_target_image(self, image: ImageType) -> None:
        {
            ImageType.A: self.image_a_button,
            ImageType.B: self.image_b_button,
            ImageType.IAP: self.image_iap_button,
        }[image].setChecked(True)
