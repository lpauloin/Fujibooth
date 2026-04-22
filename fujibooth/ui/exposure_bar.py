from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
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
    color: #636366;
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
    "color: #636366;" "font-size: 10px;" "font-weight: 700;" "letter-spacing: 2px;"
)

_SEPARATOR_STYLE = (
    "background: #2c2c2e;" "min-width: 1px;" "max-width: 1px;" "margin: 6px 0;"
)


class _ExposureColumn(QWidget):
    def __init__(self, label_text, placeholder_text):
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
    changed = Signal(str)  # field name: 'ae_mode' | 'iso' | 'shutter' | 'aperture'

    def __init__(self):
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

    def _setup_ui(self):
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

        self.ae_mode_combo.activated.connect(lambda _: self.changed.emit("ae_mode"))
        self.iso_combo.activated.connect(lambda _: self.changed.emit("iso"))
        self.shutter_combo.activated.connect(lambda _: self.changed.emit("shutter"))
        self.aperture_combo.activated.connect(lambda _: self.changed.emit("aperture"))

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

    def set_controls_enabled(self, enabled: bool):
        self.ae_mode_combo.setEnabled(enabled)
        self.iso_combo.setEnabled(enabled)
        self.shutter_combo.setEnabled(enabled)
        self.aperture_combo.setEnabled(enabled)
