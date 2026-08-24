"""桌面端蓝白主题。"""

BLUE_WHITE_STYLESHEET = """
QMainWindow,
QWidget {
    background-color: #FFFFFF;
    color: #202124;
    font-family: "Microsoft YaHei";
    font-size: 13px;
}

QWidget[actionCell="true"] {
    background-color: transparent;
}

QGroupBox {
    background-color: #FFFFFF;
    border: 1px solid #B8C9DC;
    border-radius: 8px;
    margin-top: 24px;
    padding: 10px 8px 8px 8px;
    font-weight: 600;
}

QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 0;
    right: 0;
    padding: 4px 10px;
    color: #17365D;
    background-color: #DCE9F8;
    border-radius: 4px;
    font-size: 13px;
}

QPushButton {
    min-height: 24px;
    padding: 1px 12px;
    border: 1px solid #9AA7B4;
    border-radius: 5px;
    background-color: #F4F6F8;
    color: #263238;
}

QPushButton:hover {
    background-color: #E7EDF3;
    border-color: #6F8192;
}

QPushButton:pressed {
    background-color: #D8E1EA;
}

QPushButton[buttonRole="primary"] {
    color: #FFFFFF;
    background-color: #0078D4;
    border-color: #0070C6;
    font-weight: 600;
}

QPushButton[buttonRole="primary"]:hover {
    background-color: #1689DB;
}

QPushButton[buttonRole="primary"]:pressed {
    background-color: #0067B8;
}

QPushButton:disabled {
    color: #949DA6;
    background-color: #E9EDF1;
    border-color: #D2D8DE;
}

QPushButton[compact="true"] {
    padding-left: 7px;
    padding-right: 7px;
}

QPushButton[actionButton="true"] {
    min-height: 20px;
    padding: 0 5px;
    font-size: 12px;
}

QLineEdit,
QPlainTextEdit,
QTableView {
    background-color: #FFFFFF;
    border: 1px solid #B8C9DC;
    selection-background-color: #D6EBFA;
    selection-color: #202124;
    border-radius: 6px;
}

QLineEdit {
    min-height: 24px;
    padding: 1px 6px;
}

QPlainTextEdit {
    font-family: Consolas, "Microsoft YaHei";
    font-size: 13px;
}

QHeaderView::section {
    min-height: 27px;
    padding: 2px 7px;
    background-color: #DCE9F8;
    color: #17365D;
    border: none;
    border-right: 1px solid #B8C9DC;
    border-bottom: 1px solid #B8C9DC;
    font-weight: 600;
    font-size: 13px;
}

QTableView {
    gridline-color: #E2E8EF;
    alternate-background-color: #F7FAFD;
}

QTableView::item {
    min-height: 28px;
    padding: 3px;
}

QTableView::item:selected {
    background-color: #D6EBFA;
    color: #17365D;
}

QProgressBar {
    min-height: 4px;
    max-height: 4px;
    border: 1px solid #AAB9C8;
    border-radius: 3px;
    background-color: #EDF2F7;
}

QProgressBar::chunk {
    background-color: #0078D4;
    border-radius: 2px;
}

QSplitter::handle {
    background-color: #C5D1DE;
    width: 1px;
    height: 4px;
}

QCheckBox {
    spacing: 6px;
    color: #455A64;
}

QStatusBar {
    min-height: 22px;
    background-color: #0078D4;
    color: #FFFFFF;
    border: none;
}

QStatusBar QLabel {
    background-color: transparent;
    color: #FFFFFF;
}
"""
