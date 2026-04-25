from PySide6.QtCore import Qt, Signal
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

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class FastHorizontalScrollArea(QScrollArea):
    def wheelEvent(self, event):
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

    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.NoFrame)
        self.setObjectName("LiveViewWidget")
        self._pixmap = QPixmap()
        self._freeze_frame = False

        self.setStyleSheet("""
            QFrame#LiveViewWidget {
                background: transparent;
                border: none;
            }
            """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.image_label = ClickableLabel()
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setMinimumHeight(480)
        self.image_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.image_label)

        self.overlay = QLabel(self.image_label)
        self.overlay.setAlignment(Qt.AlignCenter)
        self.overlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.overlay.hide()

        self.status_badge = QLabel(self.image_label)
        self.status_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.status_badge.hide()

        self.remote_badge = QLabel(self.image_label)
        self.remote_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.remote_badge.hide()

        self.image_label.clicked.connect(self.clicked)
        self._apply_frame_style()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.overlay.setGeometry(self.image_label.rect())
        self._update_status_badge_position()
        self._render_pixmap()

    def _apply_frame_style(self):
        if self._freeze_frame:
            border = "3px solid rgba(255, 255, 255, 0.75)"
            bg = "#050505"
        else:
            border = "none"
            bg = "#080808"
        self.image_label.setStyleSheet(f"""
            QLabel {{
                background: {bg};
                border-radius: 18px;
                border: {border};
            }}
            """)

    def set_freeze_frame(self, enabled):
        self._freeze_frame = enabled
        self._apply_frame_style()

    def _update_status_badge_position(self):
        y = 20
        for badge in (self.status_badge, self.remote_badge):
            if badge.isHidden():
                continue
            badge.adjustSize()
            badge.move(20, y)
            badge.raise_()
            y += badge.height() + 8

    def set_pixmap(self, pixmap):
        self._pixmap = pixmap
        self._render_pixmap()

    def _render_pixmap(self):
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

    def show_overlay_text(self, text, font_px=110, background="rgba(0,0,0,0.55)"):
        self.overlay.setText(text)
        self.overlay.setStyleSheet(f"""
            QLabel {{
                background: {background};
                color: white;
                font-size: {font_px}px;
                font-weight: 900;
                border-radius: 18px;
            }}
            """)
        self.overlay.setGeometry(self.image_label.rect())
        self.overlay.show()
        self.overlay.raise_()

    def hide_overlay(self):
        self.overlay.hide()
        self.overlay.clear()

    def set_status(self, text, color):
        text = (text or "").strip()
        if not text:
            self.clear_status()
            return

        self.status_badge.setText(text)
        self.status_badge.setStyleSheet(f"""
            QLabel {{
                background: {color};
                color: white;
                padding: 8px 14px;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 800;
            }}
            """)
        self.status_badge.adjustSize()
        self._update_status_badge_position()
        self.status_badge.show()
        self.status_badge.raise_()

    def clear_status(self):
        self.status_badge.clear()
        self.status_badge.hide()
        self._update_status_badge_position()

    def set_remote_status(self, text, color):
        self.remote_badge.setText((text or "").strip())
        self.remote_badge.setStyleSheet(f"""
            QLabel {{
                background: {color};
                color: white;
                padding: 8px 14px;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 800;
            }}
            """)
        self.remote_badge.show()
        self._update_status_badge_position()

    def clear_remote_status(self):
        self.remote_badge.clear()
        self.remote_badge.hide()
        self._update_status_badge_position()


class ThumbnailButton(QPushButton):
    photo_clicked = Signal(str)

    _BORDER_NORMAL = "border: 2px solid #444444; border-radius: 12px; background: transparent;"
    _BORDER_SELECTED = "border: 3px solid #dc2626; border-radius: 12px; background: transparent;"

    def __init__(self, photo_path, width, height):
        super().__init__()
        self.photo_path = photo_path
        self.setFixedSize(width, height)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton { border: none; border-radius: 12px; background: #151515; padding: 0px; }
        """)

        pixmap = QPixmap(str(photo_path))
        if not pixmap.isNull():
            scaled = pixmap.scaled(
                self.size(),
                Qt.KeepAspectRatioByExpanding,
                Qt.SmoothTransformation,
            )
            self.setIcon(QIcon(scaled))
            self.setIconSize(self.size())

        # Overlay drawn on top of the icon — the only place borders are visible
        self._border = QFrame(self)
        self._border.setGeometry(0, 0, width, height)
        self._border.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self._border.setAttribute(Qt.WA_StyledBackground, True)
        self._border.setStyleSheet(self._BORDER_NORMAL)
        self._border.raise_()

        self.clicked.connect(lambda: self.photo_clicked.emit(str(self.photo_path)))

    def set_remote_selected(self, selected):
        self._border.setStyleSheet(self._BORDER_SELECTED if selected else self._BORDER_NORMAL)


class GalleryWidget(QWidget):
    photo_selected = Signal(str)

    def __init__(self, thumb_w, thumb_h):
        super().__init__()
        self.thumb_w = thumb_w
        self.thumb_h = thumb_h
        self._thumbs = []

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

    def _reset_layout(self):
        self._thumbs = []
        while self.layout.count():
            item = self.layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.layout.addStretch(1)

    def set_photos(self, photos):
        self._reset_layout()
        if not photos:
            return

        last_item = self.layout.takeAt(self.layout.count() - 1)
        if last_item is not None:
            del last_item

        for photo in photos:
            thumb = ThumbnailButton(photo, self.thumb_w, self.thumb_h)
            thumb.photo_clicked.connect(self.photo_selected)
            self.layout.addWidget(thumb)
            self._thumbs.append(thumb)

        self.layout.addStretch(1)

    def set_remote_selection(self, index):
        """Highlight thumbnail at index with a red border and scroll it into view (-1 = clear all)."""
        for i, thumb in enumerate(self._thumbs):
            thumb.set_remote_selected(i == index)
        if 0 <= index < len(self._thumbs):
            self.scroll.ensureWidgetVisible(self._thumbs[index])

    def find_photo_index(self, photo_path):
        """Return the position of photo_path in current thumbs, -1 if not found."""
        target = str(photo_path)
        for i, thumb in enumerate(self._thumbs):
            if str(thumb.photo_path) == target:
                return i
        return -1