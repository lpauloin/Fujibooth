import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..backends.fujifilm_sdk_backend import FujifilmSdkBackend
from ..models.remote import RemoteButton, RemoteFocus
from ..models.state import BackendState, BoothState
from ..services.bluetooth_monitor import (
    BluetoothPrinterMonitor,
    BluetoothPrinterMonitorConfig,
)
from ..services.photo_repository import PhotoRepository
from ..services.printer import PrintService
from ..services.remote_control import RemoteControlService
from ..services.usb_monitor import USBMonitor, USBMonitorConfig
from .exposure_bar import ExposureBarWidget
from .widgets import GalleryWidget, LiveViewWidget

logger = logging.getLogger(__name__)

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
    "color: #636366;"
    "font-size: 14px;"
    "font-weight: 400;"
    "letter-spacing: 0.3px;"
    "padding: 2px 0;"
)


class MainWindow(QMainWindow):
    def __init__(self, settings):
        super().__init__()
        logger.info("MainWindow.__init__()")

        self.settings = settings

        self.state = BoothState.WAITING_FOR_CAMERA
        self.selected_photo = None
        self.current_live_pixmap = QPixmap()
        self.current_freeze_pixmap = QPixmap()
        self.countdown_value = settings.app.countdown_seconds

        self._is_shutting_down = False
        self._usb_monitor_started = False
        self._camera_connected = False
        self._camera_label = "FUJIFILM"
        self._remote_focus = RemoteFocus.SLIDESHOW
        self._remote_gallery_index = -1
        self._remote_control_index = 0
        self._remote_in_setting = False
        self._remote_connected = False
        self._printer_connected = False
        self._printer_label = "Printer"
        self._printer_monitor_started = False
        self._badges_visible = True
        self._loading_exposure_controls = False

        self.setWindowTitle(settings.app.window_title)
        self.setStyleSheet(f"background: {settings.ui.background_color}; color: white;")
        self.resize(1440, 960)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()

        logger.info("building services")
        self.repository = PhotoRepository(
            captures_dir=settings.captures_path,
            output_dir=settings.output_path,
            extensions=settings.storage.accepted_extensions,
            filename_pattern=settings.storage.filename_pattern,
            frame_path=settings.frame_path,
            thumbnails_dir=settings.thumbnails_path,
            thumbnail_size=(settings.ui.thumbnail_width, settings.ui.thumbnail_height),
        )
        self.repository.ensure_all_thumbnails()
        self.print_service = PrintService(
            enabled=settings.printing.enabled,
            mode=settings.printing.mode,
            command=settings.printing.command,
            device_name=settings.printing.device_name,
            device_address=settings.printing.device_address,
            wait_after_print_seconds=settings.printing.wait_after_print_seconds,
            max_queue_size=settings.printing.max_queue_size,
            auto_rotate_landscape=settings.printing.auto_rotate_landscape,
            image_fit=settings.printing.image_fit,
            autocontrast=settings.printing.autocontrast,
            color_boost=settings.printing.color_boost,
            contrast_boost=settings.printing.contrast_boost,
            brightness_boost=settings.printing.brightness_boost,
            sharpness_boost=settings.printing.sharpness_boost,
        )
        self.backend = FujifilmSdkBackend(settings=settings, repository=self.repository)
        logger.info("backend selected: %s", self.backend.__class__.__name__)

        self.printer_monitor = BluetoothPrinterMonitor(
            BluetoothPrinterMonitorConfig(
                enabled=settings.printing.monitor_enabled,
                device_name=settings.printing.device_name,
                device_address=settings.printing.device_address,
                scan_interval_ms=settings.printing.scan_interval_ms,
                scan_duration_ms=settings.printing.scan_duration_ms,
                lost_after_misses=settings.printing.lost_after_misses,
                initial_grace_ms=settings.printing.initial_grace_ms,
                cooldown_after_busy_ms=settings.printing.cooldown_after_busy_ms,
            ),
            is_busy_callback=self.print_service.is_busy,
        )

        self.remote = RemoteControlService(parent=self)
        self.remote.button_pressed.connect(self._on_remote_button)
        self.remote.hid_connected.connect(self._on_remote_hid_connected)
        self.remote.hid_disconnected.connect(self._on_remote_hid_disconnected)
        logger.info("remote control enabled=%s", settings.remote.enabled)

        self.usb_monitor = USBMonitor(
            USBMonitorConfig(
                vendor_id=settings.camera.usb.vendor_id,
                product_ids=settings.camera.usb.product_ids,
                camera_name_contains=settings.camera.usb.camera_name_contains,
            )
        )
        logger.info(
            "usb_monitor config enabled=%s vendor_id=%s product_ids=%s camera_name_contains=%s",
            settings.camera.usb.enabled,
            settings.camera.usb.vendor_id,
            settings.camera.usb.product_ids,
            settings.camera.usb.camera_name_contains,
        )

        self.freeze_timer = QTimer(self)
        self.freeze_timer.setSingleShot(True)
        self.freeze_timer.timeout.connect(self._return_to_live_view)

        self.countdown_timer = QTimer(self)
        self.countdown_timer.setInterval(1000)
        self.countdown_timer.timeout.connect(self._countdown_tick)


        self.return_timer = QTimer(self)
        self.return_timer.setSingleShot(True)
        self.return_timer.timeout.connect(self._return_to_live_view)

        self._setup_ui()
        self._wire_signals()
        self.live_view.load_frame(settings.frame_path if settings.frame_path else None)
        self.refresh_gallery()
        self._remote_gallery_index = 0
        self.gallery.set_remote_selection(0)
        self._set_exposure_controls_enabled(False)
        self._apply_idle_ui()
        self._sync_print_button_visibility()
        self._debug_dump_ui_state("after __init__")

    def _debug_dump_ui_state(self, origin):
        logger.debug(
            "%s | state=%s camera_connected=%s camera_label=%s "
            "usb_monitor_started=%s selected_photo=%s",
            origin,
            self.state,
            self._camera_connected,
            self._camera_label,
            self._usb_monitor_started,
            self.selected_photo,
        )

    def _setup_ui(self):
        logger.debug("_setup_ui()")
        root = QWidget(self)

        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        self.live_view = LiveViewWidget()
        self.live_view.set_status("📷", self.settings.ui.status_disconnected_color)
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

        self.gallery_card = QFrame()
        self.gallery_card.setObjectName("GalleryCard")
        self.gallery_card.setStyleSheet("""
            QFrame#GalleryCard {
                background: #0a0a0b;
                border-radius: 16px;
                border: 1px solid #1c1c1e;
            }
        """)
        card_layout = QVBoxLayout(self.gallery_card)
        card_layout.setContentsMargins(0, 0, 0, 0)

        self.gallery = GalleryWidget(
            self.settings.ui.thumbnail_width,
            self.settings.ui.thumbnail_height,
        )
        self.gallery.setFixedHeight(self.settings.ui.bottom_gallery_height)
        card_layout.addWidget(self.gallery)
        layout.addWidget(self.gallery_card)

        self.setCentralWidget(root)
        logger.debug("_setup_ui() done")

    def _wire_signals(self):
        logger.debug("_wire_signals()")

        self.live_view.clicked.connect(self.start_countdown)
        self.gallery.photo_selected.connect(self.on_photo_selected)
        self.print_button.clicked.connect(self.on_print_clicked)

        self.exposure_bar.changed.connect(self.on_exposure_changed)
        self.exposure_bar.frame_toggled.connect(self.live_view.set_frame_visible)

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

        self.printer_monitor.connected.connect(self._on_printer_connected)
        self.printer_monitor.disconnected.connect(self._on_printer_disconnected)

        logger.debug("_wire_signals() done")

    def _set_state(self, state):
        if self.state != state:
            logger.info("state %s -> %s", self.state, state)
        self.state = state
        live_states = {BoothState.LIVE_VIEW}
        self.exposure_bar.setVisible(state in live_states)
        self._sync_print_button_visibility()

        if state is BoothState.FREEZE:
            self._apply_freeze_ui()

    def _sync_print_button_visibility(self):
        visible = self.state is BoothState.PHOTO_SELECTED and self.selected_photo is not None
        self.print_button.setVisible(visible)
        if visible:
            self.print_button.raise_()

    def _show_gallery(self, visible):
        logger.debug("_show_gallery visible=%s", visible)
        self.gallery_card.setVisible(visible)

    def _show_camera_badge(self, visible):
        logger.debug(
            "_show_camera_badge visible=%s camera_connected=%s camera_label=%s",
            visible,
            self._camera_connected,
            self._camera_label,
        )
        self._badges_visible = visible
        if visible:
            color = (
                self.settings.ui.status_connected_color
                if self._camera_connected
                else self.settings.ui.status_disconnected_color
            )
            self.live_view.set_status("📷", color)
            self._refresh_remote_badge()
            self._refresh_printer_badge()
        else:
            self.live_view.clear_status()
            self.live_view.clear_remote_status()
            self.live_view.clear_printer_status()

    def _refresh_remote_badge(self):
        if not self._badges_visible:
            return
        color = (
            self.settings.ui.status_connected_color
            if self._remote_connected
            else self.settings.ui.status_disconnected_color
        )
        self.live_view.set_remote_status("📱️", color)

    def _refresh_printer_badge(self):
        if not self._badges_visible:
            return
        color = (
            self.settings.ui.status_connected_color
            if self._printer_connected
            else self.settings.ui.status_disconnected_color
        )
        self.live_view.set_printer_status("🖨️", color)

    def _set_exposure_controls_enabled(self, enabled):
        logger.debug("_set_exposure_controls_enabled enabled=%s", enabled)
        self.exposure_bar.set_controls_enabled(enabled)

    def _set_combo_by_value(self, combo, raw_value):
        if raw_value is None:
            return
        index = combo.findData(raw_value)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            logger.warning(
                "_set_combo_by_value: value %r not found in combo", raw_value
            )

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
        logger.debug("_load_exposure_controls()")

        # Exposure refresh is asynchronous now. The backend serializes every
        # SDK call through its command queue, so the UI only requests work here.
        if self._loading_exposure_controls:
            logger.debug("_load_exposure_controls skipped: already running")
            return

        if not self._camera_connected:
            logger.debug("_load_exposure_controls skipped: camera not connected")
            return

        self._loading_exposure_controls = True
        self._set_exposure_controls_enabled(False)
        self.message_label.setText("Refreshing camera controls...")
        self.backend.request_exposure_data()

    def _apply_idle_ui(self):
        logger.debug("_apply_idle_ui()")
        self._show_gallery(True)
        self._sync_print_button_visibility()
        self.live_view.hide_overlay()
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(True)

    def _apply_busy_ui(self):
        logger.debug("_apply_busy_ui()")
        self._show_gallery(False)
        self._sync_print_button_visibility()
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(False)

    def _apply_freeze_ui(self):
        logger.debug("_apply_freeze_ui()")
        self._show_gallery(False)
        self._sync_print_button_visibility()
        self.live_view.hide_overlay()
        self.live_view.set_freeze_frame(True)
        self._show_camera_badge(False)

        if self.current_freeze_pixmap.isNull():
            logger.warning("_apply_freeze_ui called without a freeze pixmap")
            return

        self.live_view.set_pixmap(self.current_freeze_pixmap, apply_frame=False)

    def _apply_photo_selected_ui(self):
        logger.debug("_apply_photo_selected_ui()")
        self._show_gallery(True)
        self.live_view.set_freeze_frame(False)
        self._show_camera_badge(True)

    def start_services(self):
        logger.info("start_services() begin")
        self.backend.start()
        logger.info("backend.start() done")

        if self.settings.camera.usb.enabled:
            logger.info("usb_monitor.start()")
            self.usb_monitor.start()
            self._usb_monitor_started = True

        if self.settings.printing.monitor_enabled:
            logger.info("printer_monitor.start()")
            self.printer_monitor.start()
            self._printer_monitor_started = True

        rc = self.settings.remote
        if rc.enabled:
            if rc.hid_device_name:
                self.remote.start_hid_monitor(rc.hid_device_name)
            if rc.hid_vendor_id and rc.hid_product_id:
                self.remote.start_hid_capture(rc.hid_vendor_id, rc.hid_product_id)
            else:
                logger.warning(
                    "hid_vendor_id/hid_product_id not set — HID capture disabled"
                )

        if self.settings.app.fullscreen:
            self.showFullScreen()
        else:
            self.show()

        self._debug_dump_ui_state("after start_services")

    def stop_services(self):
        logger.info("stop_services()")
        self.countdown_timer.stop()
        self.freeze_timer.stop()
        self.return_timer.stop()
        self.remote.stop()
        if self._printer_monitor_started:
            try:
                self.printer_monitor.stop()
            except Exception as exc:
                logger.error("error stopping printer_monitor: %s", exc)
            self._printer_monitor_started = False

        try:
            self.print_service.stop()
        except Exception as exc:
            logger.error("error stopping print_service: %s", exc)

        if self._usb_monitor_started:
            try:
                self.usb_monitor.stop()
            except Exception as exc:
                logger.error("error stopping usb_monitor: %s", exc)
            self._usb_monitor_started = False

        try:
            self.backend.stop()
        except Exception as exc:
            logger.error("error stopping backend: %s", exc)

        self._debug_dump_ui_state("after stop_services")

    def shutdown(self):
        if self._is_shutting_down:
            return
        logger.info("shutdown()")
        self._is_shutting_down = True
        self.stop_services()

    def closeEvent(self, event):
        logger.info("closeEvent()")
        self.shutdown()
        super().closeEvent(event)

    def keyPressEvent(self, event):
        logger.debug("keyPressEvent key=%s", event.key())
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
        logger.debug(
            "remote button=%s state=%s focus=%s in_setting=%s",
            button.name,
            self.state,
            self._remote_focus.name,
            self._remote_in_setting,
        )

        _busy = {BoothState.COUNTDOWN, BoothState.CAPTURING, BoothState.DOWNLOADING}
        if self.state in _busy:
            return

        # ── UP / DOWN : switch focus or navigate inside a setting ──────────
        if button is RemoteButton.UP:
            if self._remote_in_setting:
                self._remote_setting_navigate(-1)
            elif self._camera_connected:
                if self.state is BoothState.PHOTO_SELECTED:
                    self._return_to_live_view()
                self._switch_remote_focus(RemoteFocus.CONTROLS)
            return

        if button is RemoteButton.DOWN:
            if self._remote_in_setting:
                self._remote_setting_navigate(1)
            elif self._camera_connected:
                self._switch_remote_focus(RemoteFocus.SLIDESHOW)
            return

        # ── LEFT / RIGHT : navigate gallery or controls ────────────────────
        if button is RemoteButton.LEFT:
            if not self._remote_in_setting:
                if self._remote_focus is RemoteFocus.SLIDESHOW:
                    self._remote_gallery_navigate(-1)
                elif self._remote_focus is RemoteFocus.CONTROLS:
                    self._remote_control_navigate(-1)
            return

        if button is RemoteButton.RIGHT:
            if not self._remote_in_setting:
                if self._remote_focus is RemoteFocus.SLIDESHOW:
                    self._remote_gallery_navigate(1)
                elif self._remote_focus is RemoteFocus.CONTROLS:
                    self._remote_control_navigate(1)
            return

        # ── CENTER : open / confirm a setting ─────────────────────────────
        if button is RemoteButton.CENTER:
            if self._remote_focus is RemoteFocus.CONTROLS:
                if self._remote_in_setting:
                    self._remote_setting_confirm()
                else:
                    self._remote_open_setting()
            return

        # ── PHOTO : capture in live view, print when a photo is selected ───
        if button is RemoteButton.PHOTO:
            if self.state is BoothState.LIVE_VIEW:
                self.start_countdown()
            elif self.state in {BoothState.FREEZE, BoothState.PHOTO_SELECTED}:
                self.on_print_clicked()
            return

    # ------------------------------------------------------------------
    # Remote focus / navigation helpers
    # ------------------------------------------------------------------

    def _switch_remote_focus(self, focus):
        self._remote_focus = focus
        self._remote_in_setting = False
        if focus is RemoteFocus.SLIDESHOW:
            self.exposure_bar.set_focused_control(-1)
            if self._remote_gallery_index >= 0:
                self.gallery.set_remote_selection(self._remote_gallery_index)
                photos = self.repository.recent(limit=50)
                if self._remote_gallery_index < len(photos):
                    self.on_photo_selected(str(photos[self._remote_gallery_index]))
                    self.return_timer.stop()
                    self._sync_print_button_visibility()
        else:
            self.gallery.set_remote_selection(-1)
            self.exposure_bar.set_focused_control(self._remote_control_index)
        logger.info("remote focus → %s", focus.name)

    def _remote_gallery_navigate(self, delta):
        photos = self.repository.recent(limit=50)
        if not photos:
            return
        if self._remote_gallery_index < 0:
            self._remote_gallery_index = len(photos) - 1
        else:
            self._remote_gallery_index = max(
                0, min(len(photos) - 1, self._remote_gallery_index + delta)
            )
        self.on_photo_selected(str(photos[self._remote_gallery_index]))
        self.return_timer.stop()
        self._sync_print_button_visibility()

    def _remote_control_navigate(self, delta):
        combos = [
            self.ae_mode_combo,
            self.iso_combo,
            self.shutter_combo,
            self.aperture_combo,
        ]
        enabled = [i for i, c in enumerate(combos) if c.isEnabled() and c.count() > 0]
        if self.repository.has_frame:
            enabled.append(4)
        if not enabled:
            return
        try:
            pos = enabled.index(self._remote_control_index)
        except ValueError:
            pos = 0
        self._remote_control_index = enabled[(pos + delta) % len(enabled)]
        self.exposure_bar.set_focused_control(self._remote_control_index)

    def _remote_open_setting(self):
        if self._remote_control_index == 4:
            self.exposure_bar.toggle_frame()
            return
        combos = [
            self.ae_mode_combo,
            self.iso_combo,
            self.shutter_combo,
            self.aperture_combo,
        ]
        if not combos[self._remote_control_index].isEnabled():
            return
        self._remote_in_setting = True
        self.exposure_bar.set_editing(self._remote_control_index, True)
        logger.info("remote: editing control %s", self._remote_control_index)

    def _remote_setting_navigate(self, delta):
        combos = [
            self.ae_mode_combo,
            self.iso_combo,
            self.shutter_combo,
            self.aperture_combo,
        ]
        combo = combos[self._remote_control_index]
        if not combo.isEnabled() or combo.count() == 0:
            return
        new_index = (combo.currentIndex() + delta) % combo.count()
        combo.blockSignals(True)
        combo.setCurrentIndex(new_index)
        combo.blockSignals(False)

    def _remote_setting_confirm(self):
        self._remote_in_setting = False
        self.exposure_bar.set_editing(self._remote_control_index, False)
        field = ["ae_mode", "iso", "shutter", "aperture"][self._remote_control_index]
        logger.info("remote: confirmed %s", field)
        self.on_exposure_changed(field)

    def _reset_remote_state(self):
        self._remote_focus = RemoteFocus.SLIDESHOW
        self._remote_gallery_index = -1
        self._remote_control_index = 0
        self._remote_in_setting = False
        self.gallery.set_remote_selection(-1)
        self.exposure_bar.set_focused_control(-1)

    @Slot()
    def start_countdown(self):
        logger.info(
            "start_countdown() state=%s camera_connected=%s",
            self.state,
            self._camera_connected,
        )

        if self.state not in {
            BoothState.LIVE_VIEW,
            BoothState.ERROR,
            BoothState.PHOTO_SELECTED,
        }:
            logger.debug("start_countdown ignored: state=%s", self.state)
            return

        if not self._camera_connected:
            self.message_label.setText("Waiting for FUJIFILM camera")
            logger.debug("start_countdown aborted: no camera connected")
            return

        self._reset_remote_state()
        self._set_state(BoothState.COUNTDOWN)
        self.selected_photo = None
        self._sync_print_button_visibility()
        self.freeze_timer.stop()
        self.return_timer.stop()
        self._apply_busy_ui()

        self.countdown_value = self.settings.app.countdown_seconds
        self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
        self.message_label.setText("Get ready...")
        self.countdown_timer.start()
        self._debug_dump_ui_state("after start_countdown")

    @Slot()
    def _countdown_tick(self):
        self.countdown_value -= 1
        logger.debug("_countdown_tick -> %s", self.countdown_value)

        if self.countdown_value > 0:
            self.live_view.show_overlay_text(str(self.countdown_value), font_px=150)
            return

        self.countdown_timer.stop()
        self._set_state(BoothState.CAPTURING)
        self._apply_busy_ui()
        self.live_view.show_overlay_text("📸", font_px=150)
        self.message_label.setText("Capturing...")

        try:
            logger.info("backend.trigger_capture()")
            self.backend.trigger_capture()
        except Exception as exc:
            logger.error("trigger_capture exception: %s", exc)
            self.live_view.hide_overlay()
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()
            self.message_label.setText(str(exc))

    @Slot(dict)
    def _on_printer_connected(self, payload):
        logger.info("printer connected payload=%s", payload)
        self._printer_connected = True
        self._printer_label = payload.get("label") or payload.get("name") or "Printer"
        self._refresh_printer_badge()

    @Slot()
    def _on_printer_disconnected(self):
        logger.info("printer disconnected")
        self._printer_connected = False
        self._printer_label = "Printer"
        self._refresh_printer_badge()

    @Slot(str)
    def on_exposure_changed(self, field):
        logger.info("on_exposure_changed field=%s", field)
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

        logger.info(
            "applying exposure ae_mode=%s iso=%s shutter=%s aperture=%s",
            ae_mode,
            iso,
            shutter,
            aperture,
        )

        self.backend.set_exposure(
            iso=iso, shutter=shutter, aperture=aperture, ae_mode=ae_mode
        )
        self.message_label.setText("Applying camera settings...")

    @Slot(object)
    def on_exposure_data_ready(self, payload):
        logger.info("on_exposure_data_ready payload_keys=%s", list(payload.keys()))

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

        logger.info(
            "exposure UX sync ae_mode=%s shutter_available=%s aperture_available=%s",
            ae_mode,
            shutter_available,
            aperture_available,
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

        logger.info(
            "exposure controls refreshed ae_mode_count=%s iso_count=%s "
            "shutter_count=%s aperture_count=%s state=%s",
            self.ae_mode_combo.count(),
            self.iso_combo.count(),
            self.shutter_combo.count(),
            self.aperture_combo.count(),
            state,
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
        logger.warning("on_exposure_data_failed message=%s", message)
        self._loading_exposure_controls = False
        self._set_exposure_controls_enabled(self._camera_connected)
        if self._camera_connected:
            self.message_label.setText(message)

    @Slot(object)
    def on_live_view_updated(self, pixmap):
        if not isinstance(pixmap, QPixmap):
            pixmap = QPixmap.fromImage(pixmap)

        if pixmap.isNull():
            return

        self.current_live_pixmap = pixmap

        if self.state in {BoothState.FREEZE, BoothState.PHOTO_SELECTED}:
            return

        self.live_view.set_pixmap(pixmap, apply_frame=True)

    @Slot(str)
    def on_photo_captured(self, display_path_str):
        logger.info("on_photo_captured path=%s", display_path_str)
        display_path = Path(display_path_str)

        freeze_pixmap = QPixmap(str(display_path))
        if freeze_pixmap.isNull():
            logger.error("on_photo_captured freeze pixmap is null")
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()
            self.message_label.setText("Error loading photo")
            return

        self.current_freeze_pixmap = freeze_pixmap
        self.selected_photo = display_path
        self._set_state(BoothState.FREEZE)
        self.message_label.setText("")

        self.repository.ensure_thumbnail(display_path)
        self.refresh_gallery()
        self.freeze_timer.start(FREEZE_SECONDS * 1000)
        self._debug_dump_ui_state("after on_photo_captured")

    @Slot()
    def _return_to_live_view(self):
        logger.info("_return_to_live_view()")
        self.freeze_timer.stop()
        self.return_timer.stop()
        self._sync_print_button_visibility()
        self._reset_remote_state()
        self._remote_gallery_index = 0
        self.gallery.set_remote_selection(0)

        if self._camera_connected:
            self.current_freeze_pixmap = QPixmap()
            self._set_state(BoothState.LIVE_VIEW)
            self._apply_idle_ui()
            self.live_view.hide_overlay()

            if not self.current_live_pixmap.isNull():
                self.live_view.set_pixmap(self.current_live_pixmap, apply_frame=True)

            self.message_label.setText("Tap the image to start the photobooth")
            self._switch_remote_focus(RemoteFocus.CONTROLS)
        else:
            self._set_state(BoothState.WAITING_FOR_CAMERA)
            self._apply_idle_ui()
            self.live_view.hide_overlay()
            self.message_label.setText("Waiting for FUJIFILM camera")

        self._debug_dump_ui_state("after _return_to_live_view")

    @Slot(str)
    def on_photo_selected(self, photo_path_str):
        logger.info("on_photo_selected path=%s state=%s", photo_path_str, self.state)
        if self.state in {
            BoothState.COUNTDOWN,
            BoothState.CAPTURING,
            BoothState.DOWNLOADING,
            BoothState.FREEZE,
        }:
            logger.debug("on_photo_selected ignored")
            return

        self.return_timer.stop()

        self.selected_photo = Path(photo_path_str)
        idx = self.gallery.find_photo_index(self.selected_photo)
        self._remote_gallery_index = idx
        self.gallery.set_remote_selection(idx)

        pixmap = QPixmap(str(self.selected_photo))
        if not pixmap.isNull():
            self.live_view.hide_overlay()
            self.live_view.set_pixmap(pixmap)

        self._set_state(BoothState.PHOTO_SELECTED)
        self._apply_photo_selected_ui()
        self.message_label.setText(self.selected_photo.name)
        self._sync_print_button_visibility()
        self.return_timer.start(RETURN_TO_LIVEVIEW_SECONDS * 1000)
        self._debug_dump_ui_state("after on_photo_selected")

    @Slot()
    def on_print_clicked(self):
        logger.info("on_print_clicked selected_photo=%s", self.selected_photo)
        if not self.selected_photo:
            return

        self.return_timer.stop()

        self._set_state(BoothState.PRINTING)
        ok, message = self.print_service.print_photo(self.selected_photo)
        logger.info("print result ok=%s message=%s", ok, message)
        self.message_label.setText(message)

        if ok:
            self._set_state(BoothState.PHOTO_SELECTED)
            self.return_timer.start(RETURN_TO_LIVEVIEW_SECONDS * 1000)
        else:
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()

    @Slot(object)
    def on_backend_state_changed(self, state):
        logger.info(
            "on_backend_state_changed raw=%s current_ui_state=%s", state, self.state
        )
        if not isinstance(state, BackendState):
            logger.debug("on_backend_state_changed ignored: not BackendState")
            return

        if self.state in {
            BoothState.COUNTDOWN,
            BoothState.FREEZE,
            BoothState.PHOTO_SELECTED,
            BoothState.PRINTING,
        }:
            logger.debug("on_backend_state_changed ignored: strong local UI state")
            return

        if state is BackendState.CAMERA_READY:
            self._set_state(BoothState.LIVE_VIEW)
            self._apply_idle_ui()
            self.message_label.setText("Tap the image to start the photobooth")
            if self._camera_connected:
                self._set_exposure_controls_enabled(True)
                if not self._loading_exposure_controls:
                    self._reapply_combo_enabled_state()

        elif state is BackendState.LIVE_VIEW:
            already_live = self.state is BoothState.LIVE_VIEW
            self._set_state(BoothState.LIVE_VIEW)
            if not already_live:
                self._apply_idle_ui()
                self.message_label.setText("Tap the image to start the photobooth")
            if self._camera_connected:
                self._set_exposure_controls_enabled(True)
                if not self._loading_exposure_controls:
                    self._reapply_combo_enabled_state()

        elif state is BackendState.UPDATING_CAMERA_PARAMS:
            self._set_exposure_controls_enabled(False)
            self.message_label.setText("Updating camera settings...")

        elif state is BackendState.WAITING_FOR_CAMERA:
            if self.state is BoothState.ERROR:
                logger.debug("on_backend_state_changed ignored: keep explicit error visible")
                return
            self._set_state(BoothState.WAITING_FOR_CAMERA)
            self._apply_idle_ui()
            self.message_label.setText("Waiting for FUJIFILM camera")

        elif state is BackendState.CAPTURING:
            self._set_state(BoothState.CAPTURING)
            self._apply_busy_ui()
            self.message_label.setText("Capturing...")

        elif state is BackendState.DOWNLOADING:
            self._set_state(BoothState.DOWNLOADING)
            self._apply_busy_ui()
            self.message_label.setText("Downloading photo...")

        elif state is BackendState.ERROR:
            self._set_state(BoothState.ERROR)
            self._apply_idle_ui()

        self._debug_dump_ui_state("after on_backend_state_changed")

    @Slot(dict)
    def _on_usb_monitor_connected(self, payload):
        logger.info("_on_usb_monitor_connected payload=%s", payload)
        self.backend.handle_usb_connected(payload)

    @Slot()
    def _on_usb_monitor_disconnected(self):
        logger.info("_on_usb_monitor_disconnected()")
        self.backend.handle_usb_disconnected()

    @Slot(dict)
    def _on_backend_camera_connected(self, payload):
        logger.info("_on_backend_camera_connected payload=%s", payload)
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
        logger.info("_on_backend_camera_disconnected()")
        self._camera_connected = False
        self._camera_label = "FUJIFILM"
        self._loading_exposure_controls = False
        self._remote_focus = RemoteFocus.SLIDESHOW
        self._remote_in_setting = False
        self.exposure_bar.set_focused_control(-1)
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
        logger.error("on_error message=%s", message)
        self._loading_exposure_controls = False
        self._set_state(BoothState.ERROR)
        self._apply_idle_ui()
        self.live_view.hide_overlay()
        self.message_label.setText(message)
        self._debug_dump_ui_state("after on_error")

    @Slot(str)
    def _on_remote_hid_connected(self, name):
        logger.info("remote HID connected name=%s", name)
        self._remote_connected = True
        self._refresh_remote_badge()

    @Slot(str)
    def _on_remote_hid_disconnected(self, name):
        logger.info("remote HID disconnected name=%s", name)
        self._remote_connected = False
        self._refresh_remote_badge()

    def refresh_gallery(self):
        records = self.repository.recent_thumbnails(limit=50)
        logger.info("refresh_gallery thumbnails=%s", len(records))
        self.gallery.set_photos(records)
