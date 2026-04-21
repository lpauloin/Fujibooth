from enum import Enum, auto
from pathlib import Path

from PySide6.QtCore import QTimer

from ..config import Settings
from ..sdk_bridge.adapter import CameraSdkAdapter
from ..sdk_bridge.wrapper import FujifilmSdkAdapter
from .base import CameraBackend
from .state import BackendState


class SessionState(Enum):
    STOPPED = auto()
    WAITING_USB = auto()
    CONNECTING = auto()
    READY = auto()
    LIVE = auto()
    CAPTURING = auto()
    ERROR = auto()


class FujifilmSdkBackend(CameraBackend):
    def __init__(
        self, settings, *, adapter=None, capture_dir=None, live_view_interval_ms=None
    ):
        super().__init__()
        self.settings = settings

        self.adapter = adapter or FujifilmSdkAdapter(
            sdk_root=settings.camera.sdk.sdk_root,
            xapi_path=settings.camera.sdk.library_path or None,
        )

        self.capture_dir = capture_dir or settings.sdk_capture_path
        self.capture_dir.mkdir(parents=True, exist_ok=True)

        self.state = SessionState.STOPPED
        self._usb_present = False
        self._stopping = False
        self._capture_pending = False

        interval_ms = live_view_interval_ms or settings.camera.sdk.live_view_interval_ms

        self.live_timer = QTimer(self)
        self.live_timer.setInterval(max(120, int(interval_ms)))
        self.live_timer.timeout.connect(self._pull_live_frame)

        self.capture_result_timer = QTimer(self)
        self.capture_result_timer.setInterval(150)
        self.capture_result_timer.timeout.connect(self._poll_capture_result)

        self.connect_timer = QTimer(self)
        self.connect_timer.setSingleShot(True)
        self.connect_timer.setInterval(1200)
        self.connect_timer.timeout.connect(self._connect_if_possible)

        self.health_timer = QTimer(self)
        self.health_timer.setInterval(1000)
        self.health_timer.timeout.connect(self._health_check)

        print(
            f"[SDK] backend init adapter={self.adapter.__class__.__name__} "
            f"capture_dir={self.capture_dir}"
        )

    # ------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------

    def _set_session_state(self, state, reason=""):
        if self.state != state:
            print(
                f"[SDK] session state {self.state.name} -> {state.name} reason={reason}"
            )
        self.state = state

    def _emit_backend_state_from_session(self):
        mapping = {
            SessionState.STOPPED: BackendState.WAITING_FOR_CAMERA,
            SessionState.WAITING_USB: BackendState.WAITING_FOR_CAMERA,
            SessionState.CONNECTING: BackendState.WAITING_FOR_CAMERA,
            SessionState.READY: BackendState.CAMERA_READY,
            SessionState.LIVE: BackendState.LIVE_VIEW,
            SessionState.CAPTURING: BackendState.CAPTURING,
            SessionState.ERROR: BackendState.WAITING_FOR_CAMERA,
        }
        self.emit_backend_state(mapping[self.state])

    def _is_session_open(self):
        try:
            connected = self.adapter.is_connected()
            print(f"[SDK] _is_session_open() -> {connected}")
            return connected
        except Exception as exc:
            print(f"[SDK] _is_session_open() error: {exc}")
            return False

    def _schedule_reconnect(self):
        if self._usb_present and not self._stopping:
            print("[SDK] schedule reconnect")
            self.connect_timer.stop()
            self.connect_timer.start()

    def _pause_runtime_activity(self):
        live_active = self.live_timer.isActive()
        health_active = self.health_timer.isActive()

        print(
            f"[SDK] _pause_runtime_activity() "
            f"live_active={live_active} health_active={health_active}"
        )

        self.live_timer.stop()
        self.health_timer.stop()
        return live_active, health_active

    def _resume_runtime_activity(self, *, live_active, health_active):
        print(
            f"[SDK] _resume_runtime_activity() "
            f"live_active={live_active} health_active={health_active}"
        )

        if self._stopping:
            return

        if not self._is_session_open():
            print("[SDK] _resume_runtime_activity skipped: session not open")
            return

        if health_active:
            self.health_timer.start()

        if live_active and self.state is SessionState.LIVE:
            self.live_timer.start()

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def start(self):
        if self.state is not SessionState.STOPPED:
            return

        print("[SDK] start()")
        self._stopping = False
        self._capture_pending = False

        try:
            self.adapter.start()
        except Exception as exc:
            print(f"[SDK] adapter.start failed: {exc}")
            self._set_session_state(SessionState.ERROR, f"adapter.start failed: {exc}")
            self.error.emit(str(exc))
            self._emit_backend_state_from_session()
            return

        self._set_session_state(SessionState.WAITING_USB, "backend started")
        self._emit_backend_state_from_session()

    def stop(self):
        print("[SDK] stop()")
        self._stopping = True

        self.live_timer.stop()
        self.capture_result_timer.stop()
        self.connect_timer.stop()
        self.health_timer.stop()
        self._capture_pending = False

        try:
            self._close_session(emit_signal=False)
        finally:
            try:
                self.adapter.stop()
            except Exception as exc:
                print(f"[SDK] adapter.stop ignored: {exc}")

            self._set_session_state(SessionState.STOPPED, "backend stopped")

    # ------------------------------------------------------------
    # USB-driven entry points
    # ------------------------------------------------------------

    def handle_usb_connected(self, payload=None):
        print(f"[SDK] handle_usb_connected payload={payload}")
        self._usb_present = True

        if self._stopping or self.state is SessionState.STOPPED:
            return

        if self.state in {
            SessionState.CONNECTING,
            SessionState.READY,
            SessionState.LIVE,
            SessionState.CAPTURING,
        }:
            print("[SDK] usb connect ignored: session already active")
            return

        self._schedule_reconnect()

    def handle_usb_disconnected(self):
        print("[SDK] handle_usb_disconnected()")
        self._usb_present = False
        self._capture_pending = False

        self.live_timer.stop()
        self.capture_result_timer.stop()
        self.connect_timer.stop()
        self.health_timer.stop()

        self._close_session(emit_signal=True)
        self._set_session_state(SessionState.WAITING_USB, "usb disconnected")
        self._emit_backend_state_from_session()

    # ------------------------------------------------------------
    # Connection / session
    # ------------------------------------------------------------

    def _connect_if_possible(self):
        print(
            f"[SDK] _connect_if_possible() "
            f"stopping={self._stopping} usb_present={self._usb_present} state={self.state.name}"
        )

        if self._stopping or not self._usb_present:
            return

        if self.state in {
            SessionState.CONNECTING,
            SessionState.READY,
            SessionState.LIVE,
            SessionState.CAPTURING,
        }:
            print("[SDK] _connect_if_possible skipped: state already active")
            return

        self._set_session_state(
            SessionState.CONNECTING, "usb present -> trying connect"
        )
        self._emit_backend_state_from_session()

        try:
            descriptor = self.adapter.connect_camera()
            print(
                f"[SDK] connect_camera ok model={descriptor.model} serial={descriptor.serial}"
            )

            self.camera_connected.emit(
                {
                    "label": f"{descriptor.model} connected",
                    "serial": descriptor.serial,
                }
            )

            try:
                report = self.adapter.run_precheck_safe()
                print(self.adapter.format_precheck_summary(report))
            except Exception as exc:
                print(f"[SDK] precheck warning: {exc}")

            self._set_session_state(SessionState.READY, "camera connected")
            self._emit_backend_state_from_session()
            self.health_timer.start()
            self.start_live_view()

        except Exception as exc:
            print(f"[SDK] connect failed: {exc}")
            self._set_session_state(SessionState.WAITING_USB, f"connect failed: {exc}")
            self._emit_backend_state_from_session()
            self._schedule_reconnect()

    def _close_session(self, *, emit_signal):
        print(f"[SDK] _close_session emit_signal={emit_signal}")
        self.live_timer.stop()
        self.capture_result_timer.stop()

        try:
            self.adapter.disconnect_camera()
        except Exception as exc:
            print(f"[SDK] adapter.disconnect_camera ignored: {exc}")

        if emit_signal:
            self.camera_disconnected.emit()

    # ------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------

    def start_live_view(self):
        print(f"[SDK] start_live_view() state={self.state.name}")

        if self._stopping:
            return

        if self.state not in {SessionState.READY, SessionState.ERROR}:
            print("[SDK] start_live_view skipped: invalid state")
            return

        if not self._is_session_open():
            print("[SDK] start_live_view aborted: session not open")
            self._handle_session_lost("start_live_view: session not open")
            return

        try:
            self.adapter.begin_live_view()
            self.live_timer.start()
            self._set_session_state(SessionState.LIVE, "live started")
            self._emit_backend_state_from_session()
        except Exception as exc:
            print(f"[SDK] start_live_view failed: {exc}")
            self._handle_runtime_error(exc)

    def stop_live_view(self):
        print(f"[SDK] stop_live_view() state={self.state.name}")
        self.live_timer.stop()

        if self.state is not SessionState.LIVE:
            return

        try:
            self.adapter.end_live_view()
        except Exception as exc:
            print(f"[SDK] end_live_view ignored: {exc}")
        finally:
            self._set_session_state(SessionState.READY, "live stopped")

    def _restart_live_view_after_capture(self):
        print("[SDK] _restart_live_view_after_capture()")
        self.live_timer.stop()

        if not self._is_session_open():
            self._handle_session_lost(
                "restart_live_view_after_capture: session not open"
            )
            return

        try:
            try:
                self.adapter.end_live_view()
            except Exception as exc:
                print(f"[SDK] restart live pre-clean ignored: {exc}")

            self._set_session_state(SessionState.READY, "restart live pre-start")
            self.adapter.begin_live_view()
            self.live_timer.start()
            self._set_session_state(SessionState.LIVE, "live restarted after capture")
            self._emit_backend_state_from_session()
        except Exception as exc:
            print(f"[SDK] restart live failed: {exc}")
            self._handle_runtime_error(exc)

    # ------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------

    def trigger_capture(self):
        print(f"[SDK] trigger_capture() state={self.state.name}")

        if self._stopping:
            return

        if self.state not in {SessionState.LIVE, SessionState.READY}:
            print("[SDK] trigger_capture skipped: invalid state")
            return

        if self._capture_pending:
            print("[SDK] trigger_capture skipped: already pending")
            return

        if not self._is_session_open():
            self._handle_session_lost("trigger_capture: session not open")
            return

        self._capture_pending = True
        self.live_timer.stop()
        self._set_session_state(SessionState.CAPTURING, "capture requested")
        self._emit_backend_state_from_session()

        try:
            self.adapter.enqueue_capture(self.capture_dir)
            self.capture_result_timer.start()
        except Exception as exc:
            self._capture_pending = False
            print(f"[SDK] enqueue_capture failed: {exc}")
            self._handle_runtime_error(exc)

    def _poll_capture_result(self):
        print(
            f"[SDK] _poll_capture_result() "
            f"state={self.state.name} capture_pending={self._capture_pending}"
        )

        if self._stopping:
            self.capture_result_timer.stop()
            self._capture_pending = False
            return

        if not self._is_session_open():
            self.capture_result_timer.stop()
            self._capture_pending = False
            self._handle_session_lost("poll_capture_result: session not open")
            return

        try:
            path, err = self.adapter.pop_capture_result()
        except Exception as exc:
            self.capture_result_timer.stop()
            self._capture_pending = False
            print(f"[SDK] pop_capture_result failed: {exc}")
            self._handle_runtime_error(exc)
            return

        if err:
            self.capture_result_timer.stop()
            self._capture_pending = False
            print(f"[SDK] capture result error: {err}")
            self.error.emit(err)
            self._set_session_state(SessionState.ERROR, "capture returned error")
            self._restart_live_view_after_capture()
            return

        if path is None:
            return

        self.capture_result_timer.stop()
        self._capture_pending = False

        print(f"[SDK] capture result path={path}")
        self.emit_backend_state(BackendState.DOWNLOADING)
        self._emit_photo(path)
        self._restart_live_view_after_capture()

    # ------------------------------------------------------------
    # Polling / health
    # ------------------------------------------------------------

    def _pull_live_frame(self):
        print(f"[SDK] _pull_live_frame() state={self.state.name}")

        if self._stopping:
            return

        if self.state is not SessionState.LIVE:
            print("[SDK] _pull_live_frame skipped: not in LIVE")
            return

        if not self._is_session_open():
            print("[SDK] _pull_live_frame detected closed session")
            self._handle_session_lost("live poll: session closed")
            return

        try:
            frame = self.adapter.get_live_view_frame()
        except Exception as exc:
            print(f"[SDK] get_live_view_frame failed: {exc}")
            self._handle_runtime_error(exc)
            return

        if frame.isNull():
            print("[SDK] _pull_live_frame got null frame")
            return

        print(f"[SDK] live view frame received size={frame.size()}")
        self.live_view_updated.emit(frame)

    def _health_check(self):
        print(
            f"[SDK] _health_check() "
            f"state={self.state.name} usb_present={self._usb_present}"
        )

        if self._stopping:
            return

        if self.state in {
            SessionState.STOPPED,
            SessionState.WAITING_USB,
            SessionState.CONNECTING,
        }:
            return

        if not self._usb_present:
            print("[SDK] _health_check -> usb flag absent")
            self.handle_usb_disconnected()
            return

        if not self._is_session_open():
            print("[SDK] _health_check -> session lost")
            self._handle_session_lost("health check failed")

    # ------------------------------------------------------------
    # Exposure control
    # ------------------------------------------------------------

    def get_ae_mode_options(self):
        print(f"[SDK] get_ae_mode_options() state={self._state.name}")
        return self.adapter.get_ae_mode_options()

    def get_ae_mode(self):
        print(f"[SDK] get_ae_mode() state={self._state.name}")
        return self.adapter.get_ae_mode()

    def set_ae_mode(self, ae_mode):
        print(f"[SDK] set_ae_mode() state={self._state.name} ae_mode={ae_mode}")
        live_active, health_active = self._pause_runtime_activity()
        self.emit_backend_state(BackendState.UPDATING_CAMERA_PARAMS)
        try:
            self.adapter.set_ae_mode(ae_mode)
        except Exception as exc:
            print(f"[SDK] set_ae_mode() failed: {exc}")
            raise
        finally:
            self._resume_runtime_activity(live_active=live_active, health_active=health_active)
            self._emit_backend_state_from_session()

    def get_exposure_options(self):
        """
        Returns supported values per exposure parameter for the current camera mode.

        Each value is a list of (display_label, raw_sdk_int) tuples.
        Lists are empty for parameters auto-controlled by the current AE mode.
        """
        print(f"[SDK] get_exposure_options() state={self.state.name}")
        if not self._is_session_open():
            raise RuntimeError("Camera SDK session not open")
        return self.adapter.get_exposure_options()

    def get_exposure_state(self):
        """
        Returns the current exposure settings as raw SDK integer values.

        Keys: 'iso', 'shutter', 'aperture', 'shutter_bulb'.
        """
        print(f"[SDK] get_exposure_state() state={self.state.name}")
        if not self._is_session_open():
            raise RuntimeError("Camera SDK session not open")
        return self.adapter.get_exposure_state()

    def set_exposure(self, *, iso=None, shutter=None, aperture=None):
        """
        Applies exposure settings.  Parameters are raw SDK integer values
        as returned by get_exposure_options.  Pass None to leave unchanged.
        """
        print(
            f"[SDK] set_exposure() state={self.state.name} "
            f"iso={iso} shutter={shutter} aperture={aperture}"
        )

        if not self._is_session_open():
            raise RuntimeError("Camera not connected")

        live_active, health_active = self._pause_runtime_activity()
        self.emit_backend_state(BackendState.UPDATING_CAMERA_PARAMS)
        try:
            self.adapter.set_exposure(iso=iso, shutter=shutter, aperture=aperture)
        except Exception as exc:
            print(f"[SDK] set_exposure() failed: {exc}")
            raise
        finally:
            self._resume_runtime_activity(live_active=live_active, health_active=health_active)
            self._emit_backend_state_from_session()

    # ------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------

    def _handle_session_lost(self, reason):
        print(f"[SDK] _handle_session_lost reason={reason}")

        self.live_timer.stop()
        self.capture_result_timer.stop()
        self.health_timer.stop()
        self._capture_pending = False

        self._close_session(emit_signal=True)
        self._set_session_state(SessionState.WAITING_USB, reason)
        self._emit_backend_state_from_session()
        self._schedule_reconnect()

    def _handle_runtime_error(self, exc):
        message = str(exc)
        print(f"[SDK] _handle_runtime_error message={message}")

        disconnect_markers = (
            "camera sdk session not open",
            "camera not connected",
            "not connected",
            "device not found",
            "broken pipe",
            "i/o error",
            "usb",
            "deconnect",
            "err_code=0x2001",
        )

        if any(marker in message.lower() for marker in disconnect_markers):
            self._handle_session_lost(message)
            return

        self.error.emit(message)
        self._set_session_state(SessionState.ERROR, message)

        if self._capture_pending or self.state is SessionState.CAPTURING:
            self._restart_live_view_after_capture()
            return

        if self._is_session_open():
            try:
                self.adapter.end_live_view()
            except Exception as live_stop_exc:
                print(
                    f"[SDK] end_live_view after runtime error ignored: {live_stop_exc}"
                )

            self._set_session_state(SessionState.READY, "runtime error recovery")
            try:
                self.start_live_view()
            except Exception as restart_exc:
                print(
                    f"[SDK] start_live_view after runtime error failed: {restart_exc}"
                )
