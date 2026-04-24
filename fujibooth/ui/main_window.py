from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..backends.fujifilm_sdk_backend import FujifilmSdkBackend
from ..models.remote import RemoteButton
from ..models.state import BackendState, BoothState
from ..services.photo_repository import PhotoRepository
from ..services.printer import PrintService
from ..services.remote_control import RemoteControlService
from ..services.usb_monitor import USBMonitor, USBMonitorConfig
from .exposure_bar import ExposureBarWidget
from .widgets import GalleryWidget, LiveViewWidget

FREEZE_SECONDS = 10
RETURN_TO_LIVEVIEW_SECONDS = 5

PRINT_BUTTON_STYLE = """
QPushButton {
    background: #ffffff;
    color: #000000;
    border: none;
    border-radius: 16px;
    font-size: 26px;
    font-weight: 800;
    letter-spacing: 3px;
    padding: 0 40px;
    min-height: 64px;
}
QPushButton:hover {
    background: #f0f0f0;
}
QPushButton:pressed {
    background: #d8d8d8;
}
"""

MESSAGE_STYLE = (
    "color: #ebebf5;"
    "font-size: 20px;"
    "font-weight: 500;"
    "padding: 4px 0;"
    "opacity: 0.7;"
)

SLIDER_STYLE = """
QSlider::groove:horizontal {
    background: #1c1c1e;
    height: 4px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: #636366;
    width: 16px;
    height: 16px;
    margin: -6px 0;
    border-radius: 8px;
}
QSlider::handle:horizontal:hover {
    background: #aeaeb2;
}
QSlider::sub-page:horizontal {
    background: #48484a;
    border-radius: 2px;
}
"""


class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        print("[UI] MainWindow.__init__()")

        self.settings = settings

        self.state = BoothState.WAITING_FOR_CAMERA
        self.selected_photo = None
        self.current_live_pixmap = QPixmap()
        self.current_freeze_pixmap = QPixmap()
        self.countdown_value = settings.app.countdown_seconds

        self._is_shutting_down = False
        self._gallery_scrollbar = None
        self._usb_monitor_started = False
        self._camera_connected = False
        self._camera_label = "FUJIFILM"
        self._remote_gallery_index: int = -1  # -1 = no remote selection active

        self.setWindowTitle(settings.app.window_title)
        self.setStyleSheet(f"background: {settings.ui.background_color}; color: white;")
        self.resize(1440, 960)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

        print("[UI] building services")
        self.repository = PhotoRepository(
            captures_dir=settings.captures_path,
            output_dir=settings.output_path,
            extensions=settings.storage.accepted_extensions,
            filename_pattern=settings.storage.filename_pattern,
            frame_path=settings.frame_path,
        )
        self.print_service = PrintService(
            enabled=settings.printing.enabled,
            command=settings.printing.command,
        )
        self.backend = FujifilmSdkBackend(settings=settings, repository=self.repository)
        print(f"[UI] backend selected: {self.backend.__class__.__name__}")

        self.remote = RemoteControlService(parent=self)
        self.remote.button_pressed.connect(self._on_remote_button)
        self.remote.hid_connected.connect(self._on_remote_hid_connected)
        self.remote.hid_disconnected.connect(self._on_remote_hid_disconnected)
        print(f"[UI] remote control enabled={settings.remote.enabled}")

        self.usb_monitor = USBMonitor(
            USBMonitorConfig(
                vendor_id=settings.camera.usb.vendor_id,
                product_ids=settings.camera.usb.product_ids,
                camera_name_contains=settings.camera.usb.camera_name_contains,
            )
        )
        print(
            "[UI] usb_monitor config "
            f"enabled={settings.camera.usb.enabled} "
            f"vendor_id={settings.camera.usb.vendor_id} "
            f"product_ids={settings.camera.usb.product_ids} "
            f"camera_name_contains={settings.camera.usb.camera_name_contains}"
        )

        self.freeze_timer = QTimer(self)
        self.freeze_timer.setSingleShot(True)
        self.freeze_timer.timeout.connect(self._return_to_live_view)

        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)

        self.print_button_timer = QTimer(self)
        self.print_button_timer.setSingleShot(True)
        self.print_button_timer.timeout.connect(
            self.print_button.hide if hasattr(self, "print_button") else lambda: None
        )

        self.return_timer = QTimer(self)
        self.return_timer.setSingleShot(True)
        self.return_timer.timeout.connect(self._return_to_live_view)

        self._setup_ui()
        self._wire_signals()
        self.refresh_gallery()
        self._set_exposure_controls_enabled(False)
        self._apply_idle_ui()
        self._debug_dump_ui_state("after __init__")

    def _debug_dump_ui_state(self, origin):
        print(
            f"[UI-DEBUG] {origin} | "
            f"state={self.state} "
            f"camera_connected={self._camera_connected} "
            f"camera_label={self._camera_label} "
            f"usb_monitor_started={self._usb_monitor_started} "
            f"selected_photo={self.selected_photo}"
        )

    def _setup_ui(self):
        print("[UI] _setup_ui()")
        root = QWidget(self)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self.live_view = LiveViewWidget()
        self.live_view.set_status(
            "Waiting for camera", self.settings.ui.status_disconnected_color
        )
        layout.addWidget(self.live_view, stretch=1)

        self.exposure_bar = ExposureBarWidget()
        self.ae_mode_combo = self.exposure_bar.ae_mode_combo
        self.iso_combo = self.exposure_bar.iso_combo
        self.shutter_combo = self.exposure_bar.shutter_combo
        self.aperture_combo = self.exposure_bar.aperture_combo
        self.exposure_bar.hide()
        layout.addWidget(self.exposure_bar)

        self.message_label = QLabel("Waiting for FUJIFILM camera")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setStyleSheet(MESSAGE_STYLE)
        layout.addWidget(self.message_label)

        self.print_button = QPushButton("PRINT")
        self.print_button.setStyleSheet(PRINT_BUTTON_STYLE)
        self.print_button.setCursor(Qt.PointingHandCursor)
        self.print_button.hide()
        layout.addWidget(self.print_button, alignment=Qt.AlignCenter)

        self.gallery = GalleryWidget(
            self.settings.ui.thumbnail_width,
            self.settings.ui.thumbnail_height,
        )
        self.gallery.setFixedHeight(self.settings.ui.bottom_gallery_height)
        layout.addWidget(self.gallery)

        self.gallery_slider = QSlider(Qt.Horizontal)
        self.gallery_slider.setMinimum(0)
        self.gallery_slider.setMaximum(0)
        self.gallery_slider.setSingleStep(60)
        self.gallery_slider.setPageStep(300)
        self.gallery_slider.setEnabled(False)
        self.gallery_slider.setFixedHeight(20)
        self.gallery_slider.setStyleSheet(SLIDER_STYLE)
        layout.addWidget(self.gallery_slider)

        self.setCentralWidget(root)
        print("[UI] _setup_ui() done")

    def _wire_signals(self):
        print("[UI] _wire_signals()")

        self.live_view.clicked.connect(self.start_countdown)
        self.gallery.photo_selected.connect(self.on_photo_selected)
        self.print_button.clicked.connect(self.on_print_clicked)

        self.exposure_bar.changed.connect(self.on_exposure_changed)
        self.print_button_timer.timeout.disconnect()
        self.print_button_timer.timeout.connect(self.print_button.hide)

        self.backend.live_view_updated.connect(self.on_live_view_updated)
        self.backend.photo_captured.connect(self.on_photo_captured)
        self.backend.error.connect(self.on_error)
        self.backend.state_changed.connect(self.on_backend_state_changed)
        self.backend.exposure_data_ready.connect(self.on_exposure_data_ready)
        self.backend.exposure_data_failed.connect(self.on_exposure_data_failed)

        self.backend.camera_connected.connect(self._on_backend_camera_connected)
        self.backend.camera_disconnected.connect(self._on_backend_camera_disconnected)

        self.usb_monitor.connected.connect(self._on_usb_monitor_connected)
        self.usb_monitor.disconnected.connect(self._on_usb_monitor_disconnected)

        self._gallery_scrollbar = self.gallery.get_horizontal_scrollbar()
        if self._gallery_scrollbar is not None:
            self.gallery_slider.valueChanged.connect(self._gallery_scrollbar.setValue)
            self._gallery_scrollbar.valueChanged.connect(self.gallery_slider.setValue)
            self._gallery_scrollbar.rangeChanged.connect(
                self._sync_gallery_slider_range
            )
            self._sync_gallery_slider_range(
                self._gallery_scrollbar.minimum(),
                self._gallery_scrollbar.maximum(),
            )

        print("[UI] _wire_signals() done")

    @Slot(int, int)
    def _sync_gallery_slider_range(self, minimum, maximum):
        print(f"[UI] _sync_gallery_slider_range min={minimum} max={maximum}")
        self.gallery_slider.blockSignals(True)
        self.gallery_slider.setMinimum(minimum)
        self.gallery_slider.setMaximum(maximum)
        self.gallery_slider.setEnabled(maximum > minimum)
        self.gallery_slider.blockSignals(False)

    def _set_state(self, state):
        if self.state != state:
            print(f"[UI] state {self.state} -> {state}")
        self.state = state
        live_states = {BoothState.LIVE_VIEW}
        self.exposure_bar.setVisible(state in live_states)

    def _show_gallery(self, visible):
        print(f"[UI] _show_gallery visible={visible}")
        self.gallery.setVisible(visible)
        self.gallery_slider.setVisible(visible)

    def _show_camera_badge(self, visible):
        print(
            f"[UI] _show_camera_badge visible={visible} "
            f"camera_connected={self._camera_connected} camera_label={self._camera_label}"
        )
        if visible:
            text = (
                f"{self._camera_label} connected"
                if self._camera_connected
                else "Waiting for camera"
            )
            color = (
                self.settings.ui.status_connected_color
                if self._camera_connected
                else self.settings.ui.status_disconnected_color
            )
            self.live_view.set_status(text, color)
        else:
            self.live_view.clear_status()

    def _set_exposure_controls_enabled(self, enabled):
        print(f"[UI] _set_exposure_controls_enabled enabled={enabled}")
        self.exposure_bar.set_controls_enabled(enabled)

    def _set_combo_by_value(self, combo, raw_value):
        if raw_value is None:
            return
        index = combo.findData(raw_value)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            print(f"[UI] _set_combo_by_value: value {raw_value!r} not found in combo")

    def _set_combo_auto(self, combo):
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("AUTO", None)
        combo.setCurrentIndex(0)
        combo.setEnabled(False)
        combo.blockSignals(False)

    @staticmethod
    def _combo_is_auto(combo):
        return combo.count() == 1 and combo.itemData(0) is None

    def _reapply_combo_enabled_state(self):
        """Re-disable AUTO combos after a blind _set_exposure_controls_enabled(True).
        No SDK calls — purely inspects current combo content."""
        if not self._camera_connected:
            return
        if self.ae_mode_combo.currentData() is None:
            return
        if self._combo_is_auto(self.shutter_combo):
            self.shutter_combo.setEnabled(False)
        if self._combo_is_auto(self.aperture_combo):
            self.aperture_combo.setEnabled(False)

    def _load_exposure_controls(self):
        print("[UI] _load_exposure_controls()")

        # Exposure refresh is asynchronous now. The backend serializes every
        # SDK call through its command queue, so the UI only requests work here.
        if getattr(self, "_loading_exposure_controls", False):
            print("[UI] _load_exposure_controls skipped: already running")
            return

        if not self._camera_connected:
            print("[UI] _load_exposure_controls skipped: camera not connected")
            return

        self._loading_exposure_controls = True
        self._set_exposure_controls_enabled(False)
        self.message_label.setText("Refreshing camera controls...")
        self.backend.request_exposure_data()

    def _apply_idle_ui(self):
        print("[UI] _apply_idle_ui()")
        self._show_gallery(True)
        self.print_button.hide()
        self.live_view.hide_overlay()
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(True)

    def _apply_countdown_ui(self):
        print("[UI] _apply_countdown_ui()")
        self._show_gallery(False)
        self.print_button.hide()
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(False)

    def _apply_capture_ui(self):
        print("[UI] _apply_capture_ui()")
        self._show_gallery(False)
        self.print_button.hide()
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(False)

    def _apply_freeze_ui(self):
        print("[UI] _apply_freeze_ui()")
        self._show_gallery(False)
        self.print_button.hide()
        self.live_view.set_freeze_frame(True)
        self._show_camera_badge(False)

    def _apply_photo_selected_ui(self):
        print("[UI] _apply_photo_selected_ui()")
        self._show_gallery(True)
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(True)

    def start_services(self):
        print("[UI] start_services() begin")
        self.backend.start()
        print("[UI] backend.start() done")

        if self.settings.camera.usb.enabled:
            print("[UI] usb_monitor.start()")
            self.usb_monitor.start()
            self._usb_monitor_started = True

        rc = self.settings.remote
        if rc.enabled and rc.hid_device_name:
            self.remote.start_hid_monitor(rc.hid_device_name)

        if self.settings.app.fullscreen:
            self.showFullScreen()
        else:
            self.show()

        self._debug_dump_ui_state("after start_services")

    def stop_services(self):
        print("[UI] stop_services()")
        self.countdown_timer.stop()
        self.freeze_timer.stop()
        self.print_button_timer.stop()
        self.return_timer.stop()
        self.remote.stop()

        if self._usb_monitor_started:
            try:
                self.usb_monitor.stop()
            except Exception as exc:
                print(f"[UI] error stopping usb_monitor: {exc}")
            self._usb_monitor_started = False

        try:
            self.backend.stop()
        except Exception as exc:
            print(f"[UI] error stopping backend: {exc}")

        self._debug_dump_ui_state("after stop_services")

    def shutdown(self):
        if self._is_shutting_down:
            return
        print("[UI] shutdown()")
        self._is_shutting_down = True
        self.stop_services()

    def closeEvent(self, event):
        print("[UI] closeEvent()")
        self.shutdown()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        print(f"[UI] keyPressEvent key={event.key()}")
        if event.key() == Qt.Key_Escape:
            self.shutdown()
            self.close()
            return
        if self.remote.handle_key(event.key()):
            return
        super().keyPressEvent(event)

    # ------------------------------------------------------------------
    # Remote control dispatch
    # ------------------------------------------------------------------

    @Slot(RemoteButton)
    def _on_remote_button(self, button):
        print(f"[UI] remote button={button.name} state={self.state}")

        _busy = {BoothState.COUNTDOWN, BoothState.CAPTURING, BoothState.DOWNLOADING}
        if self.state in _busy:
            return

        match button:
            case RemoteButton.CAMERA | RemoteButton.PHOTO | RemoteButton.CENTER if (
                self.state is BoothState.LIVE_VIEW
            ):
                self.start_countdown()

            case RemoteButton.LEFT if self.state not in {BoothState.WAITING_FOR_CAMERA}:
                self._remote_gallery_navigate(-1)

            case RemoteButton.RIGHT if self.state not in {
                BoothState.WAITING_FOR_CAMERA
            }:
                self._remote_gallery_navigate(1)

            case RemoteButton.PHOTO if self.state in {
                BoothState.FREEZE,
                BoothState.PHOTO_SELECTED,
            }:
                self.on_print_clicked()

            case RemoteButton.CENTER if self.state in {
                BoothState.FREEZE,
                BoothState.PHOTO_SELECTED,
            }:
                self.on_print_clicked()

            case RemoteButton.CAMERA if self.state in {
                BoothState.FREEZE,
                BoothState.PHOTO_SELECTED,
            }:
                self._return_to_live_view()

    def _remote_gallery_navigate(self, delta):
        photos = self.repository.recent(limit=50)
        if not photos:
            return

        if self._remote_gallery_index < 0:
            # Start at the most recent photo
            self._remote_gallery_index = len(photos) - 1
        else:
            self._remote_gallery_index = max(
                0, min(len(photos) - 1, self._remote_gallery_index + delta)
            )

        self.on_photo_selected(str(photos[self._remote_gallery_index]))

    @Slot()
    def start_countdown(self):
        print(
            f"[UI] start_countdown() state={self.state} camera_connected={self._camera_connected}"
        )

        if self.state not in {
            BoothState.LIVE_VIEW,
            BoothState.ERROR,
            BoothState.PHOTO_SELECTED,
        }:
            print(f"[UI] start_countdown ignored: state={self.state}")
            return

        if not self._camera_connected:
            self.message_label.setText("Waiting for FUJIFILM camera")
            print("[UI] start_countdown aborted: no camera connected")
            return

        self._set_state(BoothState.COUNTDOWN)
        self.selected_photo = None
        self.print_button.hide()
        self.print_button_timer.stop()
        self.freeze_timer.stop()
        self.return_timer.stop()
        self._apply_countdown_ui()

        self.countdown_value = self.settings.app.countdown_seconds
        self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
        self.message_label.setText("Get ready...")
        self.countdown_timer.start()
        self._debug_dump_ui_state("after start_countdown")

    @Slot()
    def _countdown_tick(self):
        self.countdown_value -= 1
        print(f"[UI] _countdown_tick -> {self.countdown_value}")

        if self.countdown_value > 0:
            self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
            return

        self.countdown_timer.stop()
        self._set_state(BoothState.CAPTURING)
        self._apply_capture_ui()
        self.live_view.show_overlay_text("📸", font_px=150)
        self.message_label.setText("Capturing...")

        try:
            print("[UI] backend.trigger_capture()")
            self.backend.trigger_capture()
        except Exception as exc:
            print(f"[UI] trigger_capture exception: {exc}")
            self.live_view.hide_overlay()
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()
            self.message_label.setText(str(exc))

    @Slot(str)
    def on_exposure_changed(self, field):
        print(f"[UI] on_exposure_changed field={field}")
        if not self._camera_connected:
            self.message_label.setText("Waiting for FUJIFILM camera")
            return

        ae_mode = self.ae_mode_combo.currentData()
        iso = self.iso_combo.currentData()
        # Only send the value that the user just changed. Sending stale combo
        # values for other fields causes wrong settings when switching AE modes
        # (e.g. switching M→A would send the old M-mode aperture value).
        shutter = self.shutter_combo.currentData() if field == "shutter" else None
        aperture = self.aperture_combo.currentData() if field == "aperture" else None

        print(
            f"[UI] applying exposure ae_mode={ae_mode} iso={iso} shutter={shutter} aperture={aperture}"
        )

        self.backend.set_exposure(
            iso=iso, shutter=shutter, aperture=aperture, ae_mode=ae_mode
        )
        self.message_label.setText("Applying camera settings...")

    @Slot(object)
    def on_exposure_data_ready(self, payload):
        print(f"[UI] on_exposure_data_ready payload_keys={list(payload.keys())}")

        options = payload.get("options", {})
        state = payload.get("state", {})

        self.ae_mode_combo.blockSignals(True)
        self.iso_combo.blockSignals(True)
        self.shutter_combo.blockSignals(True)
        self.aperture_combo.blockSignals(True)

        try:
            self.ae_mode_combo.clear()
            self.iso_combo.clear()
            self.shutter_combo.clear()
            self.aperture_combo.clear()

            for label, raw in options.get("ae_mode", []):
                self.ae_mode_combo.addItem(label, raw)

            for label, raw in options.get("iso", []):
                self.iso_combo.addItem(label, raw)

            for label, raw in options.get("shutter", []):
                self.shutter_combo.addItem(label, raw)

            for label, raw in options.get("aperture", []):
                self.aperture_combo.addItem(label, raw)

            self._set_combo_by_value(self.ae_mode_combo, state.get("ae_mode"))
            self._set_combo_by_value(self.iso_combo, state.get("iso"))
            self._set_combo_by_value(self.shutter_combo, state.get("shutter"))
            self._set_combo_by_value(self.aperture_combo, state.get("aperture"))

        finally:
            self.ae_mode_combo.blockSignals(False)
            self.iso_combo.blockSignals(False)
            self.shutter_combo.blockSignals(False)
            self.aperture_combo.blockSignals(False)

        ae_mode = state.get("ae_mode")
        shutter_available = self.shutter_combo.count() > 0
        aperture_available = self.aperture_combo.count() > 0

        print(
            f"[UI] exposure UX sync ae_mode={ae_mode} "
            f"shutter_available={shutter_available} "
            f"aperture_available={aperture_available}"
        )

        self.iso_combo.setEnabled(self._camera_connected)
        self.ae_mode_combo.setEnabled(self._camera_connected)

        shutter_active = ae_mode in (1, 4) and shutter_available
        aperture_active = ae_mode in (1, 3) and aperture_available

        if shutter_active:
            self.shutter_combo.setEnabled(self._camera_connected)
        else:
            self._set_combo_auto(self.shutter_combo)

        if aperture_active:
            self.aperture_combo.setEnabled(self._camera_connected)
        else:
            self._set_combo_auto(self.aperture_combo)

        self._loading_exposure_controls = False

        print(
            f"[UI] exposure controls refreshed "
            f"ae_mode_count={self.ae_mode_combo.count()} "
            f"iso_count={self.iso_combo.count()} "
            f"shutter_count={self.shutter_combo.count()} "
            f"aperture_count={self.aperture_combo.count()} "
            f"state={state}"
        )

        if self._camera_connected:
            if ae_mode == 3:
                self.message_label.setText(
                    "A mode: aperture controlled, shutter automatic"
                )
            elif ae_mode == 4:
                self.message_label.setText(
                    "S mode: shutter controlled, aperture automatic"
                )
            elif ae_mode == 1:
                self.message_label.setText("M mode: shutter and aperture controlled")
            elif ae_mode == 6:
                self.message_label.setText("P mode: shutter and aperture automatic")
            elif not shutter_available and not aperture_available:
                self.message_label.setText(
                    "Current mode does not allow shutter or aperture control"
                )
            elif not shutter_available:
                self.message_label.setText(
                    "Current mode does not allow shutter control"
                )
            elif not aperture_available:
                self.message_label.setText(
                    "Current mode does not allow aperture control"
                )

        self._set_exposure_controls_enabled(self._camera_connected)
        self._reapply_combo_enabled_state()

    @Slot(str)
    def on_exposure_data_failed(self, message):
        print(f"[UI] on_exposure_data_failed message={message}")
        self._loading_exposure_controls = False
        self._set_exposure_controls_enabled(self._camera_connected)
        if self._camera_connected:
            self.message_label.setText(message)

    @Slot(object)
    def on_live_view_updated(self, pixmap):
        if not isinstance(pixmap, QPixmap):
            pixmap = QPixmap.fromImage(pixmap)

        print(f"[UI] on_live_view_updated null={pixmap.isNull()} size={pixmap.size()}")
        if pixmap.isNull():
            return

        self.current_live_pixmap = pixmap

        if self.state in {BoothState.FREEZE, BoothState.PHOTO_SELECTED}:
            print("[UI] on_live_view_updated ignored: state is freeze/photo_selected")
            return

        self.live_view.set_pixmap(pixmap)

    @Slot(str)
    def on_photo_captured(self, display_path_str):
        print(f"[UI] on_photo_captured path={display_path_str}")
        display_path = Path(display_path_str)

        freeze_pixmap = QPixmap(str(display_path))
        if freeze_pixmap.isNull():
            print("[UI] on_photo_captured freeze pixmap is null")
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()
            self.message_label.setText("Error loading photo")
            return

        self.current_freeze_pixmap = freeze_pixmap
        self.selected_photo = display_path
        self._set_state(BoothState.FREEZE)
        self._apply_freeze_ui()

        self.live_view.hide_overlay()
        self.live_view.set_pixmap(freeze_pixmap)
        self.message_label.setText("")

        self.refresh_gallery()
        self.freeze_timer.start(FREEZE_SECONDS * 1000)
        self._debug_dump_ui_state("after on_photo_captured")

    @Slot()
    def _return_to_live_view(self):
        print("[UI] _return_to_live_view()")
        self.freeze_timer.stop()
        self.return_timer.stop()
        self.print_button_timer.stop()
        self.print_button.hide()
        self._remote_gallery_index = -1

        if self._camera_connected:
            self._set_state(BoothState.LIVE_VIEW)
            self._apply_idle_ui()
            self.live_view.hide_overlay()

            if not self.current_live_pixmap.isNull():
                self.live_view.set_pixmap(self.current_live_pixmap)

            self.message_label.setText("Tap the image to start the photobooth")
        else:
            self._set_state(BoothState.WAITING_FOR_CAMERA)
            self._apply_idle_ui()
            self.live_view.hide_overlay()
            self.message_label.setText("Waiting for FUJIFILM camera")

        self._debug_dump_ui_state("after _return_to_live_view")

    @Slot(str)
    def on_photo_selected(self, photo_path_str):
        print(f"[UI] on_photo_selected path={photo_path_str} state={self.state}")
        if self.state in {
            BoothState.COUNTDOWN,
            BoothState.CAPTURING,
            BoothState.DOWNLOADING,
            BoothState.FREEZE,
        }:
            print("[UI] on_photo_selected ignored")
            return

        self.return_timer.stop()

        self.selected_photo = Path(photo_path_str)
        pixmap = QPixmap(str(self.selected_photo))
        if not pixmap.isNull():
            self.live_view.hide_overlay()
            self.live_view.set_pixmap(pixmap)

        self._set_state(BoothState.PHOTO_SELECTED)
        self._apply_photo_selected_ui()
        self.message_label.setText(self.selected_photo.name)
        self.print_button.show()
        self.print_button.raise_()
        self.print_button_timer.start(self.settings.app.print_button_seconds * 1000)
        self.return_timer.start(RETURN_TO_LIVEVIEW_SECONDS * 1000)
        self._debug_dump_ui_state("after on_photo_selected")

    @Slot()
    def on_print_clicked(self):
        print(f"[UI] on_print_clicked selected_photo={self.selected_photo}")
        if not self.selected_photo:
            return

        self.return_timer.stop()

        self._set_state(BoothState.PRINTING)
        ok, message = self.print_service.print_photo(self.selected_photo)
        print(f"[UI] print result ok={ok} message={message}")
        self.message_label.setText(message)

        if ok:
            self._set_state(BoothState.PHOTO_SELECTED)
            self.return_timer.start(RETURN_TO_LIVEVIEW_SECONDS * 1000)
        else:
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()

    @Slot(object)
    def on_backend_state_changed(self, state):
        print(
            f"[UI] on_backend_state_changed raw={state} current_ui_state={self.state}"
        )
        if not isinstance(state, BackendState):
            print("[UI] on_backend_state_changed ignored: not BackendState")
            return

        if (
            self.state
            in {
                BoothState.COUNTDOWN,
                BoothState.FREEZE,
                BoothState.PHOTO_SELECTED,
                BoothState.PRINTING,
            }
            and state is not BackendState.WAITING_FOR_CAMERA
        ):
            print("[UI] on_backend_state_changed ignored: strong local UI state")
            return

        if state is BackendState.CAMERA_READY:
            self._set_state(BoothState.LIVE_VIEW)
            self._apply_idle_ui()
            self.message_label.setText("Tap the image to start the photobooth")
            if self._camera_connected:
                self._set_exposure_controls_enabled(True)
                if not getattr(self, "_loading_exposure_controls", False):
                    self._reapply_combo_enabled_state()

        elif state is BackendState.LIVE_VIEW:
            already_live = self.state is BoothState.LIVE_VIEW
            self._set_state(BoothState.LIVE_VIEW)
            if not already_live:
                self._apply_idle_ui()
                self.message_label.setText("Tap the image to start the photobooth")
            if self._camera_connected:
                self._set_exposure_controls_enabled(True)
                if not getattr(self, "_loading_exposure_controls", False):
                    self._reapply_combo_enabled_state()

        elif state is BackendState.UPDATING_CAMERA_PARAMS:
            self._set_exposure_controls_enabled(False)
            self.message_label.setText("Updating camera settings...")

        elif state is BackendState.WAITING_FOR_CAMERA:
            self._set_state(BoothState.WAITING_FOR_CAMERA)
            self._apply_idle_ui()
            self.message_label.setText("Waiting for FUJIFILM camera")

        elif state is BackendState.CAPTURING:
            self._set_state(BoothState.CAPTURING)
            self._apply_capture_ui()
            self.message_label.setText("Capturing...")

        elif state is BackendState.DOWNLOADING:
            self._set_state(BoothState.DOWNLOADING)
            self._apply_capture_ui()
            self.message_label.setText("Downloading photo...")

        self._debug_dump_ui_state("after on_backend_state_changed")

    @Slot(dict)
    def _on_usb_monitor_connected(self, payload):
        print(f"[UI] _on_usb_monitor_connected payload={payload}")
        self.backend.handle_usb_connected(payload)

    @Slot()
    def _on_usb_monitor_disconnected(self):
        print("[UI] _on_usb_monitor_disconnected()")
        self.backend.handle_usb_disconnected()

    @Slot(dict)
    def _on_backend_camera_connected(self, payload):
        print(f"[UI] _on_backend_camera_connected payload={payload}")
        self._camera_connected = True
        self._camera_label = (
            payload.get("label", "FUJIFILM").replace(" connected", "").strip()
        )
        self._show_camera_badge(True)
        self._set_exposure_controls_enabled(False)
        self._load_exposure_controls()
        self._debug_dump_ui_state("after _on_backend_camera_connected")

    @Slot()
    def _on_backend_camera_disconnected(self):
        print("[UI] _on_backend_camera_disconnected()")
        self._camera_connected = False
        self._camera_label = "FUJIFILM"
        self._loading_exposure_controls = False
        self._set_exposure_controls_enabled(False)
        self.ae_mode_combo.clear()
        self.iso_combo.clear()
        self.shutter_combo.clear()
        self.aperture_combo.clear()
        self._set_state(BoothState.WAITING_FOR_CAMERA)
        self._apply_idle_ui()
        self.message_label.setText("Waiting for FUJIFILM camera")
        self._debug_dump_ui_state("after _on_backend_camera_disconnected")

    @Slot(str)
    def on_error(self, message):
        print(f"[UI] on_error message={message}")
        self._loading_exposure_controls = False
        self._set_state(BoothState.ERROR)
        self._apply_idle_ui()
        self.live_view.hide_overlay()
        self.message_label.setText(message)
        self._debug_dump_ui_state("after on_error")

    @Slot(str)
    def _on_remote_hid_connected(self, name):
        print(f"[UI] remote HID connected name={name}")
        self.live_view.set_remote_status(
            "Remote connected", self.settings.ui.status_connected_color
        )

    @Slot(str)
    def _on_remote_hid_disconnected(self, name):
        print(f"[UI] remote HID disconnected name={name}")
        self.live_view.set_remote_status(
            "Remote disconnected", self.settings.ui.status_disconnected_color
        )

    def refresh_gallery(self):
        photos = self.repository.recent(limit=50)
        print(f"[UI] refresh_gallery photos={len(photos)}")
        self.gallery.set_photos(photos)

        if self._gallery_scrollbar is not None:
            self._sync_gallery_slider_range(
                self._gallery_scrollbar.minimum(),
                self._gallery_scrollbar.maximum(),
            )
            self.gallery_slider.setValue(self._gallery_scrollbar.value())


def run_app(settings):
    print("[UI] run_app()")
    app = QApplication.instance() or QApplication([])
    window = MainWindow(settings)
    window.start_services()
    return app.exec()
