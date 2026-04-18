from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QMainWindow, QPushButton, QSlider, QVBoxLayout, QWidget

from ..backends.fujifilm_sdk_backend import FujifilmSdkBackend
from ..backends.state import BackendState
from ..config import Settings
from ..models.state import BoothState
from ..services.photo_repository import PhotoRepository
from ..services.printer import PrintService
from ..services.usb_monitor import USBMonitor, USBMonitorConfig
from .widgets import GalleryWidget, LiveViewWidget


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.state = BoothState.WAITING_FOR_CAMERA
        self.selected_photo: Path | None = None
        self.current_live_pixmap = QPixmap()
        self.current_freeze_pixmap = QPixmap()
        self.countdown_value = settings.app.countdown_seconds
        self._is_shutting_down = False
        self._gallery_scrollbar = None
        self._usb_monitor_started = False

        self.setWindowTitle(settings.app.window_title)
        self.setStyleSheet(f"background: {settings.ui.background_color}; color: white;")
        self.resize(1440, 960)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

        self.repository = PhotoRepository(
            output_dir=settings.output_path,
            extensions=settings.storage.accepted_extensions,
            filename_pattern=settings.storage.filename_pattern,
        )
        self.print_service = PrintService(enabled=settings.printing.enabled, command=settings.printing.command)
        self.backend = FujifilmSdkBackend(settings=settings)
        print(f"[UI] backend selectionne: {self.backend.__class__.__name__}")

        self.usb_monitor = USBMonitor(
            USBMonitorConfig(
                vendor_id=settings.camera.usb.vendor_id,
                product_ids=settings.camera.usb.product_ids,
                camera_name_contains=settings.camera.usb.camera_name_contains,
            )
        )

        self.freeze_timer = QTimer(self)
        self.freeze_timer.setSingleShot(True)
        self.freeze_timer.timeout.connect(self._return_to_live_view)

        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)

        self.print_button_timer = QTimer(self)
        self.print_button_timer.setSingleShot(True)

        self._setup_ui()
        self._wire_signals()
        self.refresh_gallery()

    def _setup_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self.live_view = LiveViewWidget()
        self.live_view.set_status("En attente camera", self.settings.ui.status_disconnected_color)
        layout.addWidget(self.live_view, stretch=1)

        self.message_label = QLabel("Clique sur l image pour lancer le photobooth")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setStyleSheet("font-size: 24px; color: #f1f1f1;")
        layout.addWidget(self.message_label)

        self.print_button = QPushButton("PRINT")
        self.print_button.setFixedHeight(62)
        self.print_button.setStyleSheet(
            "QPushButton { background: white; color: black; border-radius: 14px; font-size: 28px; font-weight: 800; padding: 0 30px; }"
        )
        self.print_button.hide()
        layout.addWidget(self.print_button, alignment=Qt.AlignCenter)

        self.gallery = GalleryWidget(self.settings.ui.thumbnail_width, self.settings.ui.thumbnail_height)
        self.gallery.setFixedHeight(self.settings.ui.bottom_gallery_height)
        layout.addWidget(self.gallery)

        self.gallery_slider = QSlider(Qt.Horizontal)
        self.gallery_slider.setMinimum(0)
        self.gallery_slider.setMaximum(0)
        self.gallery_slider.setSingleStep(60)
        self.gallery_slider.setPageStep(300)
        self.gallery_slider.setEnabled(False)
        self.gallery_slider.setFixedHeight(24)
        layout.addWidget(self.gallery_slider)

        self.setCentralWidget(root)

    def _wire_signals(self) -> None:
        self.live_view.clicked.connect(self.start_countdown)
        self.gallery.photo_selected.connect(self.on_photo_selected)
        self.print_button.clicked.connect(self.on_print_clicked)
        self.print_button_timer.timeout.connect(self.print_button.hide)

        self.backend.live_view_updated.connect(self.on_live_view_updated)
        self.backend.photo_captured.connect(self.on_photo_captured)
        self.backend.error.connect(self.on_error)
        self.backend.state_changed.connect(self.on_backend_state_changed)
        self.backend.camera_connected.connect(self.on_usb_connected)
        self.backend.camera_disconnected.connect(self.on_usb_disconnected)

        self.usb_monitor.connected.connect(self.on_usb_connected)
        self.usb_monitor.disconnected.connect(self.on_usb_disconnected)

        self._gallery_scrollbar = self.gallery.get_horizontal_scrollbar()
        self.gallery_slider.valueChanged.connect(self._gallery_scrollbar.setValue)
        self._gallery_scrollbar.valueChanged.connect(self.gallery_slider.setValue)
        self._gallery_scrollbar.rangeChanged.connect(self._sync_gallery_slider_range)
        self._sync_gallery_slider_range(self._gallery_scrollbar.minimum(), self._gallery_scrollbar.maximum())

    @Slot(int, int)
    def _sync_gallery_slider_range(self, minimum: int, maximum: int) -> None:
        self.gallery_slider.blockSignals(True)
        self.gallery_slider.setMinimum(minimum)
        self.gallery_slider.setMaximum(maximum)
        self.gallery_slider.setEnabled(maximum > minimum)
        self.gallery_slider.blockSignals(False)

    def start_services(self) -> None:
        print("[UI] start_services")
        print("[UI] backend.start()")
        self.backend.start()

        if self.settings.camera.usb.enabled:
            print("[UI] usb_monitor.start()")
            self.usb_monitor.start()
            self._usb_monitor_started = True

        if self.settings.app.fullscreen:
            self.showFullScreen()
        else:
            self.show()

    def stop_services(self) -> None:
        self.countdown_timer.stop()
        self.freeze_timer.stop()
        self.print_button_timer.stop()

        if self._usb_monitor_started:
            try:
                self.usb_monitor.stop()
            except Exception as exc:
                print(f"[UI] erreur stop usb_monitor: {exc}")
            self._usb_monitor_started = False

        try:
            self.backend.stop()
        except Exception as exc:
            print(f"[UI] erreur stop backend: {exc}")

    def shutdown(self) -> None:
        if self._is_shutting_down:
            return
        print("[UI] shutdown()")
        self._is_shutting_down = True
        self.stop_services()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key_Escape:
            self.shutdown()
            self.close()
            return
        super().keyPressEvent(event)

    @Slot()
    def start_countdown(self) -> None:
        if self.state in {BoothState.COUNTDOWN, BoothState.FREEZE, BoothState.CAPTURING, BoothState.DOWNLOADING}:
            print(f"[UI] start_countdown ignore etat={self.state}")
            return
        print("[UI] start_countdown")
        self.state = BoothState.COUNTDOWN
        self.selected_photo = None
        self.print_button.hide()
        self.gallery.hide()
        self.gallery_slider.hide()
        self.countdown_value = self.settings.app.countdown_seconds
        self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
        self.message_label.setText("Preparez-vous...")
        self.countdown_timer.start()

    @Slot()
    def _countdown_tick(self) -> None:
        self.countdown_value -= 1
        print(f"[UI] countdown tick -> {self.countdown_value}")
        if self.countdown_value > 0:
            self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
            return
        self.countdown_timer.stop()
        self.live_view.show_overlay_text("📸", font_px=150)
        self.message_label.setText("Capture en cours...")
        self.backend.trigger_capture()

    @Slot(QPixmap)
    def on_live_view_updated(self, pixmap: QPixmap) -> None:
        print(f"[LIVEVIEW] frame recue null={pixmap.isNull()} size={pixmap.size()}")
        self.current_live_pixmap = pixmap
        if self.state != BoothState.FREEZE:
            self.live_view.set_pixmap(pixmap)

    @Slot(str)
    def on_photo_captured(self, source_path_str: str) -> None:
        print(f"[CAPTURE] photo recue depuis backend: {source_path_str}")
        source_path = Path(source_path_str)
        stored = self.repository.store(source_path)
        print(f"[CAPTURE] photo stockee: {stored}")

        freeze_pixmap = QPixmap(str(stored))
        self.current_freeze_pixmap = freeze_pixmap
        self.live_view.set_pixmap(freeze_pixmap)
        self.live_view.hide_overlay()
        self.message_label.setText("")
        self.state = BoothState.FREEZE
        self.selected_photo = stored
        self.refresh_gallery()
        self.freeze_timer.start(self.settings.app.freeze_seconds * 1000)

    @Slot()
    def _return_to_live_view(self) -> None:
        print("[UI] retour live view")
        self.state = BoothState.LIVE_VIEW
        self.live_view.hide_overlay()
        self.gallery.show()
        self.gallery_slider.show()
        if not self.current_live_pixmap.isNull():
            self.live_view.set_pixmap(self.current_live_pixmap)
        self.message_label.setText("Clique sur l image pour relancer une photo")

    @Slot(str)
    def on_photo_selected(self, photo_path_str: str) -> None:
        print(f"[GALLERY] photo selectionnee: {photo_path_str}")
        self.selected_photo = Path(photo_path_str)
        pixmap = QPixmap(str(self.selected_photo))
        if not pixmap.isNull():
            self.live_view.set_pixmap(pixmap)
        self.state = BoothState.PHOTO_SELECTED
        self.message_label.setText(self.selected_photo.name)
        self.print_button.show()
        self.print_button.raise_()
        self.print_button_timer.start(self.settings.app.print_button_seconds * 1000)

    @Slot()
    def on_print_clicked(self) -> None:
        print(f"[PRINT] clic selected_photo={self.selected_photo}")
        if not self.selected_photo:
            return
        self.state = BoothState.PRINTING
        ok, message = self.print_service.print_photo(self.selected_photo)
        print(f"[PRINT] resultat ok={ok} message={message}")
        self.message_label.setText(message)
        self.state = BoothState.PHOTO_SELECTED if ok else BoothState.ERROR

    @Slot(object)
    def on_backend_state_changed(self, state: object) -> None:
        print(f"[BACKEND] state_changed={state}")
        if not isinstance(state, BackendState):
            return

        if state is BackendState.CAMERA_READY:
            self.state = BoothState.LIVE_VIEW
            self.message_label.setText("Camera connectee, live view en cours")
        elif state is BackendState.LIVE_VIEW and self.state != BoothState.FREEZE:
            self.state = BoothState.LIVE_VIEW
            self.gallery.show()
            self.gallery_slider.show()
            self.message_label.setText("Clique sur l image pour lancer le photobooth")
        elif state is BackendState.WAITING_FOR_CAMERA:
            self.state = BoothState.WAITING_FOR_CAMERA
            self.gallery.show()
            self.gallery_slider.show()
            self.message_label.setText("En attente de la camera FUJIFILM")
        elif state is BackendState.CAPTURING:
            self.state = BoothState.CAPTURING
            self.gallery.hide()
            self.gallery_slider.hide()
        elif state is BackendState.DOWNLOADING:
            self.state = BoothState.DOWNLOADING

    @Slot(dict)
    def on_usb_connected(self, payload: dict) -> None:
        print(f"[USB] connecte: {payload}")
        self.live_view.set_status(payload.get("label", "FUJIFILM connecte"), self.settings.ui.status_connected_color)

    @Slot()
    def on_usb_disconnected(self) -> None:
        print("[USB] deconnecte")
        self.state = BoothState.WAITING_FOR_CAMERA
        self.live_view.set_status("Aucun appareil FUJIFILM detecte", self.settings.ui.status_disconnected_color)
        self.message_label.setText("En attente de la camera FUJIFILM")

    @Slot(str)
    def on_error(self, message: str) -> None:
        print(f"[ERROR] {message}")
        self.state = BoothState.ERROR
        self.gallery.show()
        self.gallery_slider.show()
        self.message_label.setText(message)

    def refresh_gallery(self) -> None:
        photos = self.repository.recent(limit=50)
        print(f"[GALLERY] refresh photos={len(photos)}")
        self.gallery.set_photos(photos)
        self._sync_gallery_slider_range(self._gallery_scrollbar.minimum(), self._gallery_scrollbar.maximum())
        self.gallery_slider.setValue(self._gallery_scrollbar.value())


def run_app(settings: Settings) -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings)
    window.start_services()
    return app.exec()
