from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_COMBO_STYLE = """
QComboBox {
    background: #1c1c1e;
    color: #f5f5f5;
    border: 1px solid #2c2c2e;
    border-radius: 10px;
    padding: 8px 14px;
    font-size: 15px;
    font-weight: 500;
    min-width: 140px;
}
QComboBox::drop-down {
    border: none;
    width: 28px;
}
QComboBox::down-arrow {
    width: 10px;
    height: 10px;
}
QComboBox:hover {
    border-color: #48484a;
    background: #242426;
}
QComboBox:disabled {
    background: #141416;
    color: #3a3a3c;
    border-color: #1c1c1e;
}
QComboBox QAbstractItemView {
    background: #1c1c1e;
    color: #f5f5f5;
    selection-background-color: #0a84ff;
    border: 1px solid #2c2c2e;
    border-radius: 8px;
    padding: 4px;
    outline: none;
}
"""

_LABEL_STYLE = (
    "color: #636366;"
    "font-size: 10px;"
    "font-weight: 700;"
    "letter-spacing: 2px;"
)

_SEPARATOR_STYLE = (
    "background: #2c2c2e;"
    "min-width: 1px;"
    "max-width: 1px;"
    "margin: 6px 0;"
)

_APPLY_BUTTON_STYLE = """
QPushButton {
    background: #0a84ff;
    color: white;
    border: none;
    border-radius: 12px;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 0 22px;
    min-width: 110px;
    min-height: 42px;
}
QPushButton:hover {
    background: #0070e0;
}
QPushButton:pressed {
    background: #005bbf;
}
QPushButton:disabled {
    background: #1c1c1e;
    color: #3a3a3c;
    border: 1px solid #2c2c2e;
}
"""


class _ExposureColumn(QWidget):
    def __init__(self, label_text: str, placeholder_text: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QLabel(label_text)
        header.setStyleSheet(_LABEL_STYLE)
        layout.addWidget(header)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(42)
        self.combo.setPlaceholderText(placeholder_text)
        self.combo.setStyleSheet(_COMBO_STYLE)
        layout.addWidget(self.combo)


class ExposureBarWidget(QWidget):
    apply_clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setStyleSheet(
            "QWidget#ExposureBar {"
            "  background: #0a0a0b;"
            "  border-radius: 16px;"
            "  border: 1px solid #1c1c1e;"
            "}"
        )
        self.setObjectName("ExposureBar")
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(0)

        ae_col = _ExposureColumn("MODE", "P")
        iso_col = _ExposureColumn("ISO", "AUTO")
        shutter_col = _ExposureColumn("SHUTTER", "AUTO")
        aperture_col = _ExposureColumn("APERTURE", "AUTO")

        self.ae_mode_combo = ae_col.combo
        self.iso_combo = iso_col.combo
        self.shutter_combo = shutter_col.combo
        self.aperture_combo = aperture_col.combo

        self.apply_button = QPushButton("APPLY")
        self.apply_button.setStyleSheet(_APPLY_BUTTON_STYLE)
        self.apply_button.setCursor(Qt.PointingHandCursor)
        self.apply_button.clicked.connect(self.apply_clicked)

        for i, col in enumerate([ae_col, iso_col, shutter_col, aperture_col]):
            layout.addWidget(col)
            if i < 3:
                sep = QFrame()
                sep.setFrameShape(QFrame.VLine)
                sep.setStyleSheet(_SEPARATOR_STYLE)
                layout.addSpacing(16)
                layout.addWidget(sep)
                layout.addSpacing(16)

        layout.addStretch(1)
        layout.addWidget(self.apply_button, alignment=Qt.AlignVCenter)

    def set_controls_enabled(self, enabled: bool) -> None:
        self.ae_mode_combo.setEnabled(enabled)
        self.iso_combo.setEnabled(enabled)
        self.shutter_combo.setEnabled(enabled)
        self.aperture_combo.setEnabled(enabled)
        self.apply_button.setEnabled(enabled)
