import logging
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

logger = logging.getLogger(__name__)

COMBO_STYLE = """
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

FRAME_BUTTON_STYLE = """
QPushButton {
    background: #1c1c1e;
    color: #636366;
    border: 1px solid #2c2c2e;
    border-radius: 10px;
    padding: 8px 14px;
    font-size: 15px;
    font-weight: 500;
    min-width: 80px;
    min-height: 42px;
}
QPushButton:hover {
    border-color: #48484a;
    background: #242426;
    color: #f5f5f5;
}
QPushButton:checked {
    background: #dc2626;
    color: #ffffff;
    border-color: #dc2626;
    font-weight: 700;
}
QPushButton:checked:hover {
    background: #b91c1c;
    border-color: #b91c1c;
}
"""

LABEL_STYLE = (
    "color: #636366;" "font-size: 10px;" "font-weight: 700;" "letter-spacing: 2px;"
)

SEPARATOR_STYLE = (
    "background: #2c2c2e;" "min-width: 1px;" "max-width: 1px;" "margin: 6px 0;"
)


def _make_separator():
    sep = QFrame()
    sep.setFrameShape(QFrame.VLine)
    sep.setStyleSheet(SEPARATOR_STYLE)
    return sep


class _ColumnBase(QWidget):
    """Shared border-on-focus behaviour for all control columns."""

    def __init__(self):
        super().__init__()
        self.setObjectName("ExposureColumn")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self._apply_border("transparent")

    def _apply_border(self, color, width=2):
        self.setStyleSheet(
            f"QWidget#ExposureColumn {{"
            f"  border: {width}px solid {color};"
            f"  border-radius: 10px;"
            f"  background: transparent;"
            f"}}"
        )

    def set_focused(self, focused, editing=False):
        if editing:
            self._apply_border("#ef4444", width=3)
        elif focused:
            self._apply_border("#dc2626", width=2)
        else:
            self._apply_border("transparent")


class ExposureColumn(_ColumnBase):
    def __init__(self, label_text, placeholder_text):
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QLabel(label_text)
        header.setStyleSheet(LABEL_STYLE)
        layout.addWidget(header)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(42)
        self.combo.setPlaceholderText(placeholder_text)
        self.combo.setStyleSheet(COMBO_STYLE)
        layout.addWidget(self.combo)


class FrameColumn(_ColumnBase):
    toggled = Signal(bool)

    def __init__(self):
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        header = QLabel("FRAME")
        header.setStyleSheet(LABEL_STYLE)
        layout.addWidget(header)

        self.button = QPushButton("OFF")
        self.button.setCheckable(True)
        self.button.setStyleSheet(FRAME_BUTTON_STYLE)
        self.button.toggled.connect(self._on_toggled)
        layout.addWidget(self.button)

    def _on_toggled(self, checked):
        self.button.setText("ON" if checked else "OFF")
        self.toggled.emit(checked)

    def toggle(self):
        self.button.toggle()


class ExposureBarWidget(QWidget):
    changed = Signal(str)  # field name: 'ae_mode' | 'iso' | 'shutter' | 'aperture'
    frame_toggled = Signal(bool)

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
        self._columns = []
        self._setup_ui()

    def _setup_ui(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 14, 20, 14)
        layout.setSpacing(0)

        ae_col = ExposureColumn("MODE", "P")
        iso_col = ExposureColumn("ISO", "AUTO")
        shutter_col = ExposureColumn("SHUTTER", "AUTO")
        aperture_col = ExposureColumn("APERTURE", "AUTO")
        self.frame_col = FrameColumn()

        self._columns = [ae_col, iso_col, shutter_col, aperture_col, self.frame_col]

        self.ae_mode_combo = ae_col.combo
        self.iso_combo = iso_col.combo
        self.shutter_combo = shutter_col.combo
        self.aperture_combo = aperture_col.combo
        self.frame_button = self.frame_col.button

        self.ae_mode_combo.activated.connect(lambda _: self.changed.emit("ae_mode"))
        self.iso_combo.activated.connect(lambda _: self.changed.emit("iso"))
        self.shutter_combo.activated.connect(lambda _: self.changed.emit("shutter"))
        self.aperture_combo.activated.connect(lambda _: self.changed.emit("aperture"))
        self.frame_col.toggled.connect(self.frame_toggled)

        for i, col in enumerate(self._columns):
            layout.addWidget(col)
            if i < len(self._columns) - 1:
                layout.addSpacing(16)
                layout.addWidget(_make_separator())
                layout.addSpacing(16)

        layout.addStretch(1)

    def toggle_frame(self):
        self.frame_col.toggle()

    def set_controls_enabled(self, enabled):
        self.ae_mode_combo.setEnabled(enabled)
        self.iso_combo.setEnabled(enabled)
        self.shutter_combo.setEnabled(enabled)
        self.aperture_combo.setEnabled(enabled)

    def set_focused_control(self, index):
        """Highlight the control at index with a red border (-1 = clear all)."""
        logger.debug("set_focused_control index=%s", index)
        for i, col in enumerate(self._columns):
            col.set_focused(i == index)

    def set_editing(self, index, editing):
        """Show an editing (brighter) border on the control being modified."""
        logger.debug("set_editing index=%s editing=%s", index, editing)
        for i, col in enumerate(self._columns):
            col.set_focused(i == index, editing=(i == index and editing))
