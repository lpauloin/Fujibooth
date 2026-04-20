from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QKeyEvent, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..backends.fujifilm_sdk_backend import FujifilmSdkBackend
from ..backends.state import BackendState
from ..config import Settings
from ..models.state import BoothState
from ..services.photo_repository import PhotoRepository
from ..services.printer import PrintService
from ..services.usb_monitor import USBMonitor, USBMonitorConfig
from .widgets import GalleryWidget, LiveViewWidget

FREEZE_SECONDS = 10
RETURN_TO_LIVEVIEW_SECONDS = 5

_COMBO_STYLE = (
    "QComboBox {"
    "  background: #1a1a1a;"
    "  color: white;"
    "  border: 1px solid #3a3a3a;"
    "  border-radius: 10px;"
    "  padding: 8px 12px;"
    "  font-size: 15px;"
    "  min-width: 150px;"
    "}"
    "QComboBox::drop-down { border: none; width: 28px; }"
    "QComboBox::down-arrow { width: 12px; height: 12px; }"
    "QComboBox:disabled { background: #111111; color: #555555; border-color: #222222; }"
    "QComboBox QAbstractItemView {"
    "  background: #1a1a1a;"
    "  color: white;"
    "  selection-background-color: #2f80ff;"
    "  border: 1px solid #3a3a3a;"
    "  outline: none;"
    "}"
)

_LABEL_STYLE = (
    "color: #777777; font-size: 11px; font-weight: 700; letter-spacing: 1.5px;"
)


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

        self.setWindowTitle(settings.app.window_title)
        self.setStyleSheet(f"background: {settings.ui.background_color}; color: white;")
        self.resize(1440, 960)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

        print("[UI] building services")
        self.repository = PhotoRepository(
            output_dir=settings.output_path,
            extensions=settings.storage.accepted_extensions,
            filename_pattern=settings.storage.filename_pattern,
        )
        self.print_service = PrintService(
            enabled=settings.printing.enabled,
            command=settings.printing.command,
        )
        self.backend = FujifilmSdkBackend(settings=settings)
        print(f"[UI] backend selected: {self.backend.__class__.__name__}")

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

    def _make_exposure_column(self, label_text, placeholder_text):
        col = QWidget()
        col_layout = QVBoxLayout(col)
        col_layout.setContentsMargins(0, 0, 0, 0)
        col_layout.setSpacing(5)

        header = QLabel(label_text)
        header.setStyleSheet(_LABEL_STYLE)
        col_layout.addWidget(header)

        combo = QComboBox()
        combo.setMinimumHeight(42)
        combo.setPlaceholderText(placeholder_text)
        combo.setStyleSheet(_COMBO_STYLE)
        col_layout.addWidget(combo)

        return col, combo

    def _setup_ui(self):
        print("[UI] _setup_ui()")
        root = QWidget(self)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        self.live_view = LiveViewWidget()
        self.live_view.set_status(
            "Waiting for camera", self.settings.ui.status_disconnected_color
        )
        layout.addWidget(self.live_view, stretch=1)

        # Exposure controls: labeled columns side-by-side
        self.exposure_bar = QWidget()
        self.exposure_bar.setStyleSheet(
            "QWidget { background: #0d0d0d; border-radius: 14px; }"
        )
        exposure_layout = QHBoxLayout(self.exposure_bar)
        exposure_layout.setContentsMargins(16, 12, 16, 12)
        exposure_layout.setSpacing(16)

        ae_mode_col, self.ae_mode_combo = self._make_exposure_column("MODE", "P")
        iso_col, self.iso_combo = self._make_exposure_column("ISO", "ISO AUTO")
        shutter_col, self.shutter_combo = self._make_exposure_column("SHUTTER", "AUTO")
        aperture_col, self.aperture_combo = self._make_exposure_column(
            "APERTURE", "AUTO"
        )

        self.apply_exposure_button = QPushButton("APPLY")
        self.apply_exposure_button.setMinimumHeight(42)
        self.apply_exposure_button.setMinimumWidth(100)
        self.apply_exposure_button.setStyleSheet("""
            QPushButton {
                background: #2f80ff;
                color: white;
                border-radius: 10px;
                font-size: 14px;
                font-weight: 800;
                padding: 0 20px;
                margin-top: 18px;
            }
            QPushButton:hover { background: #2467cf; }
            QPushButton:pressed { background: #1d53a7; }
            QPushButton:disabled { background: #2a2a2a; color: #555555; }
            """)

        exposure_layout.addWidget(ae_mode_col)
        exposure_layout.addWidget(iso_col)
        exposure_layout.addWidget(shutter_col)
        exposure_layout.addWidget(aperture_col)
        exposure_layout.addStretch(1)
        exposure_layout.addWidget(self.apply_exposure_button, alignment=Qt.AlignBottom)
        self.exposure_bar.hide()
        layout.addWidget(self.exposure_bar)

        self.message_label = QLabel("Waiting for FUJIFILM camera")
        self.message_label.setAlignment(Qt.AlignCenter)
        self.message_label.setStyleSheet(
            "font-size: 24px; color: #f1f1f1; font-weight: 700; padding: 8px;"
        )
        layout.addWidget(self.message_label)

        self.print_button = QPushButton("PRINT")
        self.print_button.setFixedHeight(62)
        self.print_button.setStyleSheet("""
            QPushButton {
                background: white;
                color: black;
                border-radius: 14px;
                font-size: 28px;
                font-weight: 800;
                padding: 0 30px;
            }
            QPushButton:hover { background: #f0f0f0; }
            QPushButton:pressed { background: #dddddd; }
            """)
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
        self.gallery_slider.setFixedHeight(24)
        layout.addWidget(self.gallery_slider)

        self.setCentralWidget(root)
        print("[UI] _setup_ui() done")

    def _wire_signals(self):
        print("[UI] _wire_signals()")

        self.live_view.clicked.connect(self.start_countdown)
        self.gallery.photo_selected.connect(self.on_photo_selected)
        self.print_button.clicked.connect(self.on_print_clicked)

        self.apply_exposure_button.clicked.connect(self.on_apply_exposure_clicked)
        self.print_button_timer.timeout.disconnect()
        self.print_button_timer.timeout.connect(self.print_button.hide)

        self.backend.live_view_updated.connect(self.on_live_view_updated)
        self.backend.photo_captured.connect(self.on_photo_captured)
        self.backend.error.connect(self.on_error)
        self.backend.state_changed.connect(self.on_backend_state_changed)

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
        self.ae_mode_combo.setEnabled(enabled)
        self.iso_combo.setEnabled(enabled)
        self.shutter_combo.setEnabled(enabled)
        self.aperture_combo.setEnabled(enabled)
        self.apply_exposure_button.setEnabled(enabled)

    def _set_combo_by_value(self, combo, raw_value):
        """Select the item whose UserRole data matches raw_value."""
        if raw_value is None:
            return
        index = combo.findData(raw_value)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            print(f"[UI] _set_combo_by_value: value {raw_value!r} not found in combo")

    def _load_exposure_controls(self):
        print("[UI] _load_exposure_controls()")

        # Prevent overlapping refreshes when the camera is temporarily busy.
        if getattr(self, "_loading_exposure_controls", False):
            print("[UI] _load_exposure_controls skipped: already running")
            return

        self._loading_exposure_controls = True

        try:
            try:
                options = self.backend.get_exposure_options()
                state = self.backend.get_exposure_state()
            except Exception as exc:
                print(f"[UI] _load_exposure_controls error: {exc}")

                # Retry a limited number of times because the camera can be busy
                # right after switching AE mode or restarting live view.
                retry_count = getattr(self, "_exposure_controls_retry_count", 0) + 1
                self._exposure_controls_retry_count = retry_count
                print(f"[UI] _load_exposure_controls retry_count={retry_count}")

                if retry_count <= 3:
                    self.message_label.setText(
                        "Camera is busy, refreshing exposure controls..."
                    )
                    QTimer.singleShot(600, self._load_exposure_controls)
                else:
                    print("[UI] _load_exposure_controls giving up after retries")
                    self.message_label.setText("Failed to refresh exposure controls")

                return

            # Reset retry counter on success.
            self._exposure_controls_retry_count = 0

            self.ae_mode_combo.blockSignals(True)
            self.iso_combo.blockSignals(True)
            self.shutter_combo.blockSignals(True)
            self.aperture_combo.blockSignals(True)

            try:
                self.ae_mode_combo.clear()
                self.iso_combo.clear()
                self.shutter_combo.clear()
                self.aperture_combo.clear()

                # Populate with (display_label, raw_sdk_value) pairs.
                for label, raw in options.get("ae_mode", []):
                    self.ae_mode_combo.addItem(label, raw)

                for label, raw in options.get("iso", []):
                    self.iso_combo.addItem(label, raw)

                for label, raw in options.get("shutter", []):
                    self.shutter_combo.addItem(label, raw)

                for label, raw in options.get("aperture", []):
                    self.aperture_combo.addItem(label, raw)

                # Select the current camera values by matching raw SDK integers.
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

            # Keep ISO and AE mode available whenever the camera is connected.
            self.iso_combo.setEnabled(self._camera_connected)
            self.ae_mode_combo.setEnabled(self._camera_connected)

            # Match combo availability to the current PASM mode.
            # Fujifilm AE mode values:
            #   1 = M
            #   3 = A
            #   4 = S
            #   6 = P
            if ae_mode == 3:  # A
                self.shutter_combo.setEnabled(False)
                self.aperture_combo.setEnabled(
                    self._camera_connected and aperture_available
                )
            elif ae_mode == 4:  # S
                self.shutter_combo.setEnabled(
                    self._camera_connected and shutter_available
                )
                self.aperture_combo.setEnabled(False)
            elif ae_mode == 1:  # M
                self.shutter_combo.setEnabled(
                    self._camera_connected and shutter_available
                )
                self.aperture_combo.setEnabled(
                    self._camera_connected and aperture_available
                )
            else:  # P or fallback
                self.shutter_combo.setEnabled(False)
                self.aperture_combo.setEnabled(False)

            if self.shutter_combo.count() == 0:
                self.shutter_combo.setPlaceholderText("Auto in current mode")

            if self.aperture_combo.count() == 0:
                self.aperture_combo.setPlaceholderText("Auto in current mode")

            print(
                f"[UI] _load_exposure_controls done "
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
                    self.message_label.setText(
                        "M mode: shutter and aperture controlled"
                    )
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
        finally:
            self._loading_exposure_controls = False

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
        super().keyPressEvent(event)

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

    @Slot()
    def on_apply_ae_mode_clicked(self):
        print("[UI] on_apply_ae_mode_clicked()")
        if not self._camera_connected:
            self.message_label.setText("Waiting for FUJIFILM camera")
            return

        ae_mode = self.ae_mode_combo.currentData()
        print(f"[UI] applying AE mode ae_mode={ae_mode}")

        try:
            self.backend.set_ae_mode(ae_mode)
            self._load_exposure_controls()
        except Exception as exc:
            print(f"[UI] on_apply_ae_mode_clicked error: {exc}")
            self._set_exposure_controls_enabled(self._camera_connected)
            self.message_label.setText(str(exc))

    @Slot()
    def on_apply_exposure_clicked(self):
        print("[UI] on_apply_exposure_clicked()")
        if not self._camera_connected:
            self.message_label.setText("Waiting for FUJIFILM camera")
            return

        # currentData() returns the raw SDK integer stored via addItem(label, raw)
        ae_mode = self.ae_mode_combo.currentData()
        iso = self.iso_combo.currentData()
        shutter = (
            self.shutter_combo.currentData()
            if self.shutter_combo.isEnabled() and self.shutter_combo.count() > 0
            else None
        )
        aperture = (
            self.aperture_combo.currentData()
            if self.aperture_combo.isEnabled() and self.aperture_combo.count() > 0
            else None
        )

        print(
            f"[UI] applying exposure ae_mode={ae_mode} iso={iso} shutter={shutter} aperture={aperture}"
        )

        try:
            self.backend.set_exposure(
                iso=iso, shutter=shutter, aperture=aperture, ae_mode=ae_mode
            )
            self._load_exposure_controls()
            self.message_label.setText("Exposure settings applied")
        except Exception as exc:
            print(f"[UI] on_apply_exposure_clicked error: {exc}")
            self._set_exposure_controls_enabled(self._camera_connected)
            self.message_label.setText(str(exc))

    @Slot(QPixmap)
    def on_live_view_updated(self, pixmap):
        print(f"[UI] on_live_view_updated null={pixmap.isNull()} size={pixmap.size()}")
        if pixmap.isNull():
            return

        self.current_live_pixmap = pixmap

        if self.state in {BoothState.FREEZE, BoothState.PHOTO_SELECTED}:
            print("[UI] on_live_view_updated ignored: state is freeze/photo_selected")
            return

        self.live_view.set_pixmap(pixmap)

    @Slot(str)
    def on_photo_captured(self, source_path_str):
        print(f"[UI] on_photo_captured source={source_path_str}")
        source_path = Path(source_path_str)
        stored = self.repository.store(source_path)
        print(f"[UI] on_photo_captured stored={stored}")

        freeze_pixmap = QPixmap(str(stored))
        if freeze_pixmap.isNull():
            print("[UI] on_photo_captured freeze pixmap is null")
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()
            self.message_label.setText("Error loading photo")
            return

        self.current_freeze_pixmap = freeze_pixmap
        self.selected_photo = stored
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

        elif state is BackendState.LIVE_VIEW:
            self._set_state(BoothState.LIVE_VIEW)
            self._apply_idle_ui()
            self.message_label.setText("Tap the image to start the photobooth")
            if self._camera_connected:
                self._set_exposure_controls_enabled(True)

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
        self._set_exposure_controls_enabled(True)
        self._load_exposure_controls()
        self._debug_dump_ui_state("after _on_backend_camera_connected")

    @Slot()
    def _on_backend_camera_disconnected(self):
        print("[UI] _on_backend_camera_disconnected()")
        self._camera_connected = False
        self._camera_label = "FUJIFILM"
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
        self._set_state(BoothState.ERROR)
        self._apply_idle_ui()
        self.live_view.hide_overlay()
        self.message_label.setText(message)
        self._debug_dump_ui_state("after on_error")

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
