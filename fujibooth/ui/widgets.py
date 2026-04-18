from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QIcon, QMouseEvent, QPixmap, QResizeEvent, QWheelEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class ClickableLabel(QLabel):
    clicked = Signal()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class FastHorizontalScrollArea(QScrollArea):
    def wheelEvent(self, event: QWheelEvent) -> None:
        scrollbar = self.horizontalScrollBar()
        delta = event.angleDelta().y() or event.angleDelta().x()

        if delta == 0:
            super().wheelEvent(event)
            return

        direction = -1 if delta > 0 else 1
        step = max(120, scrollbar.pageStep() // 2)
        scrollbar.setValue(scrollbar.value() + direction * step)
        event.accept()


class LiveViewWidget(QFrame):
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()

        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("LiveViewWidget")
        self.setStyleSheet(
            """
            QFrame#LiveViewWidget {
                background: transparent;
                border: none;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.image_label = ClickableLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumHeight(480)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.image_label.setStyleSheet(
            """
            QLabel {
                background: #000000;
                border-radius: 16px;
            }
            """
        )
        self.image_label.clicked.connect(self.clicked)
        layout.addWidget(self.image_label)

        self.overlay = QLabel(self.image_label)
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.overlay.hide()

        self.status_badge = QLabel(self.image_label)
        self.status_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.status_badge.hide()

        self._pixmap = QPixmap()
        self._status_text = ""
        self._status_color = "#666666"

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_overlay_geometry()
        self._update_status_badge_position()
        self._render_pixmap()

    def _update_overlay_geometry(self) -> None:
        self.overlay.setGeometry(self.image_label.rect())

    def _update_status_badge_position(self) -> None:
        if not self.status_badge.isHidden():
            self.status_badge.adjustSize()
            self.status_badge.move(20, 20)

    def set_pixmap(self, pixmap: QPixmap) -> None:
        self._pixmap = pixmap
        self._render_pixmap()

    def _render_pixmap(self) -> None:
        if self._pixmap.isNull():
            self.image_label.clear()
            return

        if self.image_label.width() <= 0 or self.image_label.height() <= 0:
            return

        scaled = self._pixmap.scaled(
            self.image_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def show_overlay_text(
        self,
        text: str,
        font_px: int = 110,
        background: str = "rgba(0,0,0,0.55)",
    ) -> None:
        self.overlay.setText(text)
        self.overlay.setStyleSheet(
            f"""
            QLabel {{
                background: {background};
                color: white;
                font-size: {font_px}px;
                font-weight: 900;
                border-radius: 16px;
            }}
            """
        )
        self._update_overlay_geometry()
        self.overlay.show()
        self.overlay.raise_()

    def hide_overlay(self) -> None:
        self.overlay.hide()
        self.overlay.clear()

    def set_status(self, text: str, color: str) -> None:
        text = (text or "").strip()

        self._status_text = text
        self._status_color = color

        if not text:
            self.clear_status()
            return

        self.status_badge.setText(text)
        self.status_badge.setStyleSheet(
            f"""
            QLabel {{
                background: {color};
                color: white;
                padding: 8px 14px;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 800;
            }}
            """
        )
        self.status_badge.adjustSize()
        self.status_badge.move(20, 20)
        self.status_badge.show()
        self.status_badge.raise_()

    def clear_status(self) -> None:
        self._status_text = ""
        self.status_badge.clear()
        self.status_badge.hide()


class ThumbnailButton(QPushButton):
    photo_clicked = Signal(str)

    def __init__(self, photo_path: Path, width: int, height: int) -> None:
        super().__init__()
        self.photo_path = photo_path

        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            """
            QPushButton {
                border: 2px solid #333333;
                border-radius: 12px;
                background: #151515;
                padding: 0px;
            }
            QPushButton:hover {
                border: 2px solid #666666;
            }
            QPushButton:pressed {
                border: 2px solid #aaaaaa;
            }
            """
        )

        pixmap = QPixmap(str(photo_path))
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self.setIcon(QIcon(scaled))
            self.setIconSize(self.size())

        self.clicked.connect(lambda: self.photo_clicked.emit(str(self.photo_path)))


class GalleryWidget(QWidget):
    photo_selected = Signal(str)

    def __init__(self, thumb_w: int, thumb_h: int) -> None:
        super().__init__()
        self.thumb_w = thumb_w
        self.thumb_h = thumb_h

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        self.scroll = FastHorizontalScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("background: transparent; border: none;")
        root.addWidget(self.scroll)

        self.container = QWidget()
        self.layout = QHBoxLayout(self.container)
        self.layout.setContentsMargins(10, 10, 10, 10)
        self.layout.setSpacing(12)
        self.scroll.setWidget(self.container)

        self._reset_layout()

    def _reset_layout(self) -> None:
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self.layout.addStretch(1)

    def get_horizontal_scrollbar(self):
        return self.scroll.horizontalScrollBar()

    def set_photos(self, photos: list[Path]) -> None:
        self._reset_layout()

        if not photos:
            return

        self.layout.takeAt(self.layout.count() - 1)

        for photo in photos:
            thumb = ThumbnailButton(photo, self.thumb_w, self.thumb_h)
            thumb.photo_clicked.connect(self.photo_selected)
            self.layout.addWidget(thumb)

        self.layout.addStretch(1)