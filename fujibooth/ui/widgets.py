from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QMouseEvent, QPainter, QPixmap, QResizeEvent, QWheelEvent
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

        self.printer_badge = QLabel(self.image_label)
        self.printer_badge.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.printer_badge.hide()

        self._frame_pixmap = QPixmap()
        self._show_frame = False
        self._apply_frame_to_display = False

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
        visible = [
            b
            for b in (self.status_badge, self.remote_badge, self.printer_badge)
            if not b.isHidden()
        ]
        for badge in visible:
            badge.adjustSize()
        max_w = max((b.width() for b in visible), default=0)
        y = 20
        for badge in visible:
            badge.setFixedWidth(max_w)
            badge.move(20, y)
            badge.raise_()
            y += badge.height() + 8

    def set_pixmap(self, pixmap, apply_frame=False):
        self._pixmap = pixmap
        self._apply_frame_to_display = apply_frame
        self._render_pixmap()

    def load_frame(self, path):
        if path:
            self._frame_pixmap = QPixmap(str(path))
        else:
            self._frame_pixmap = QPixmap()
        self._render_pixmap()

    def set_frame_visible(self, enabled):
        self._show_frame = enabled
        self._render_pixmap()

    def _render_pixmap(self):
        if self._pixmap.isNull():
            self.image_label.clear()
            return

        if self.image_label.width() <= 0 or self.image_label.height() <= 0:
            return

        src = self._pixmap

        # Crop live view to match the capture aspect ratio (SDK delivers 4:3,
        # captures are 3:2). Use the frame PNG ratio as the reference.
        if self._apply_frame_to_display and not self._frame_pixmap.isNull():
            target_ratio = self._frame_pixmap.width() / self._frame_pixmap.height()
            src_ratio = src.width() / src.height()
            if abs(src_ratio - target_ratio) > 0.005:
                if src_ratio > target_ratio:
                    new_w = int(src.height() * target_ratio)
                    x = (src.width() - new_w) // 2
                    src = src.copy(x, 0, new_w, src.height())
                else:
                    new_h = int(src.width() / target_ratio)
                    y = (src.height() - new_h) // 2
                    src = src.copy(0, y, src.width(), new_h)

        scaled = src.scaled(
            self.image_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )

        if self._show_frame and self._apply_frame_to_display and not self._frame_pixmap.isNull():
            frame = self._frame_pixmap.scaled(
                scaled.size(),
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation,
            )
            painter = QPainter(scaled)
            painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
            painter.drawPixmap(0, 0, frame)
            painter.end()

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

    @staticmethod
    def _badge_style(color):
        return f"""
            QLabel {{
                background: {color};
                color: white;
                padding: 8px 14px;
                border-radius: 12px;
                font-size: 16px;
                font-weight: 800;
            }}
            """

    def _apply_badge(self, badge, text, color):
        badge.setText((text or "").strip())
        badge.setStyleSheet(self._badge_style(color))
        badge.show()
        self._update_status_badge_position()

    def set_status(self, text, color):
        text = (text or "").strip()
        if not text:
            self.clear_status()
            return
        self._apply_badge(self.status_badge, text, color)
        self.status_badge.adjustSize()
        self.status_badge.raise_()

    def clear_status(self):
        self.status_badge.clear()
        self.status_badge.hide()
        self._update_status_badge_position()

    def set_remote_status(self, text, color):
        self._apply_badge(self.remote_badge, text, color)

    def clear_remote_status(self):
        self.remote_badge.clear()
        self.remote_badge.hide()
        self._update_status_badge_position()

    def set_printer_status(self, text, color):
        self._apply_badge(self.printer_badge, text, color)

    def clear_printer_status(self):
        self.printer_badge.clear()
        self.printer_badge.hide()
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