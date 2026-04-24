import queue
import threading
import time

from ..models.commands import WorkerCommand
from ..models.state import BackendState, SessionState
from ..sdk_bridge.wrapper import FujifilmSdkAdapter
from .base import CameraBackend


class FujifilmSdkBackend(CameraBackend):
    def __init__(
        self, settings, *, adapter=None, repository=None, live_view_interval_ms=None
    ):
        super().__init__()
        self.settings = settings
        self.repository = repository

        self.adapter = adapter or FujifilmSdkAdapter(
            sdk_root=settings.camera.sdk.sdk_root,
            xapi_path=settings.camera.sdk.library_path or None,
        )

        self.capture_dir = settings.captures_path
        self.capture_dir.mkdir(parents=True, exist_ok=True)

        interval_ms = live_view_interval_ms or settings.camera.sdk.live_view_interval_ms

        self.state = SessionState.STOPPED
        self._lock = threading.RLock()
        self._command_queue = queue.Queue()
        self._worker_thread = None
        self._stop_event = threading.Event()

        self._usb_present = False
        self._stopping = False

        self._live_interval_s = max(0.12, float(interval_ms) / 1000.0)
        self._health_interval_s = 1.0
        self._reconnect_delay_s = 1.2

        self._next_live_poll_at = None
        self._next_health_check_at = None
        self._next_reconnect_at = None

        self._latest_exposure_request = None
        self._exposure_refresh_requested = False
        self._exposure_refresh_scheduled = False
        self._exposure_apply_scheduled = False

        print(
            f"[SDK] backend init adapter={self.adapter.__class__.__name__} "
            f"capture_dir={self.capture_dir}"
        )

    # ------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------

    def _set_session_state(self, state, reason=""):
        with self._lock:
            previous = self.state
            self.state = state

        if previous != state:
            print(
                f"[SDK] session state {previous.name} -> {state.name} reason={reason}"
            )

        self._emit_backend_state_from_session()

    def _emit_backend_state_from_session(self):
        mapping = {
            SessionState.STOPPED: BackendState.WAITING_FOR_CAMERA,
            SessionState.WAITING_USB: BackendState.WAITING_FOR_CAMERA,
            SessionState.CONNECTING: BackendState.WAITING_FOR_CAMERA,
            SessionState.READY: BackendState.CAMERA_READY,
            SessionState.LIVE: BackendState.LIVE_VIEW,
            SessionState.QUERYING: BackendState.LIVE_VIEW,
            SessionState.CAPTURING: BackendState.CAPTURING,
            SessionState.ERROR: BackendState.WAITING_FOR_CAMERA,
        }
        self.emit_backend_state(mapping[self.state])

    def _current_state(self):
        with self._lock:
            return self.state

    def _safe_is_session_open(self):
        try:
            connected = self.adapter.is_connected()
            print(f"[SDK] _safe_is_session_open() -> {connected}")
            return connected
        except Exception as exc:
            print(f"[SDK] _safe_is_session_open() error: {exc}")
            return False

    def is_connected(self):
        return self._safe_is_session_open()

    def _is_disconnect_error(self, exc):
        message = str(exc).lower()
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
            "errcode_timeout",
            "communication",
        )
        return any(marker in message for marker in disconnect_markers)

    def _clear_runtime_deadlines_from_worker(self):
        self._next_live_poll_at = None
        self._next_health_check_at = None

    def _schedule_reconnect_from_worker(self, delay=None):
        if delay is None:
            delay = self._reconnect_delay_s

        if self._stopping or not self._usb_present:
            self._next_reconnect_at = None
            return

        self._next_reconnect_at = time.monotonic() + max(0.0, delay)
        print(
            f"[SDK] reconnect scheduled in {max(0.0, delay):.2f}s "
            f"target={self._next_reconnect_at:.3f}"
        )

    def _enqueue_command(self, command, payload=None):
        self._command_queue.put((command, payload))

    def _schedule_pending_commands_from_worker(self):
        state = self._current_state()

        if state not in {SessionState.READY, SessionState.LIVE}:
            return

        if self._exposure_refresh_requested and not self._exposure_refresh_scheduled:
            self._exposure_refresh_scheduled = True
            self._enqueue_command(WorkerCommand.REFRESH_EXPOSURE)

        if self._latest_exposure_request and not self._exposure_apply_scheduled:
            self._exposure_apply_scheduled = True
            self._enqueue_command(WorkerCommand.APPLY_EXPOSURE)

    def _compute_wait_timeout(self):
        deadlines = []

        if self._next_live_poll_at is not None:
            deadlines.append(self._next_live_poll_at)
        if self._next_health_check_at is not None:
            deadlines.append(self._next_health_check_at)
        if self._next_reconnect_at is not None:
            deadlines.append(self._next_reconnect_at)

        if not deadlines:
            return 0.25

        now = time.monotonic()
        timeout = min(deadlines) - now
        if timeout < 0.0:
            return 0.0
        return timeout

    # ------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------

    def start(self):
        with self._lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return

            print("[SDK] start()")
            self._stopping = False
            self._stop_event.clear()
            self._next_live_poll_at = None
            self._next_health_check_at = None
            self._next_reconnect_at = None
            self._latest_exposure_request = None
            self._exposure_refresh_requested = False
            self._exposure_refresh_scheduled = False
            self._exposure_apply_scheduled = False

            self._worker_thread = threading.Thread(
                target=self._worker_main,
                name="fuji-backend-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def stop(self):
        print("[SDK] stop()")
        with self._lock:
            self._stopping = True

        self._stop_event.set()
        self._enqueue_command(WorkerCommand.SHUTDOWN)

        worker = self._worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=30.0)

        with self._lock:
            self._worker_thread = None
            self._usb_present = False
            self._next_live_poll_at = None
            self._next_health_check_at = None
            self._next_reconnect_at = None
            self._latest_exposure_request = None
            self._exposure_refresh_requested = False
            self._exposure_refresh_scheduled = False
            self._exposure_apply_scheduled = False

        self._set_session_state(SessionState.STOPPED, "backend stopped")

    def _worker_main(self):
        print("[SDK] worker thread started")
        adapter_started = False

        try:
            self.adapter.start()
            adapter_started = True
            self._set_session_state(SessionState.WAITING_USB, "backend started")

            if self._usb_present:
                self._schedule_reconnect_from_worker(delay=0.0)

            while not self._stop_event.is_set():
                if self._run_periodic_tasks_once():
                    continue

                try:
                    command, payload = self._command_queue.get(
                        timeout=self._compute_wait_timeout()
                    )
                except queue.Empty:
                    continue

                if command is WorkerCommand.SHUTDOWN:
                    break

                self._dispatch_worker_command(command, payload)

        except Exception as exc:
            print(f"[SDK] worker fatal error: {exc}")
            self.error.emit(str(exc))
            self._set_session_state(SessionState.ERROR, f"worker fatal error: {exc}")

        finally:
            connected = self._safe_is_session_open()
            self._clear_runtime_deadlines_from_worker()
            self._next_reconnect_at = None

            if connected:
                try:
                    self.adapter.disconnect_camera()
                except Exception as exc:
                    print(f"[SDK] disconnect during worker shutdown ignored: {exc}")
                self.camera_disconnected.emit()

            if adapter_started:
                try:
                    self.adapter.stop()
                except Exception as exc:
                    print(f"[SDK] adapter.stop during worker shutdown ignored: {exc}")

            print("[SDK] worker thread stopped")

    # ------------------------------------------------------------
    # Worker command handling
    # ------------------------------------------------------------

    def _dispatch_worker_command(self, command, payload):
        print(f"[SDK] worker command={command.name} state={self._current_state().name}")

        if command is WorkerCommand.USB_CONNECTED:
            self._handle_usb_connected_from_worker(payload)
        elif command is WorkerCommand.USB_DISCONNECTED:
            self._handle_usb_disconnected_from_worker()
        elif command is WorkerCommand.CONNECT:
            self._connect_if_possible_from_worker()
        elif command is WorkerCommand.START_LIVE_VIEW:
            self._try_start_live_view_from_worker("manual start")
        elif command is WorkerCommand.STOP_LIVE_VIEW:
            self._stop_live_view_from_worker("manual stop")
        elif command is WorkerCommand.REFRESH_EXPOSURE:
            self._process_pending_exposure_refresh_from_worker()
        elif command is WorkerCommand.APPLY_EXPOSURE:
            self._process_pending_exposure_apply_from_worker()
        elif command is WorkerCommand.CAPTURE:
            self._capture_photo_from_worker()

    def _run_periodic_tasks_once(self):
        now = time.monotonic()
        state = self._current_state()

        if (
            self._next_reconnect_at is not None
            and now >= self._next_reconnect_at
            and state in {SessionState.WAITING_USB, SessionState.ERROR}
        ):
            self._next_reconnect_at = None
            self._connect_if_possible_from_worker()
            return True

        if (
            self._next_live_poll_at is not None
            and now >= self._next_live_poll_at
            and state is SessionState.LIVE
        ):
            self._next_live_poll_at = now + self._live_interval_s
            self._poll_live_view_from_worker()
            return True

        if (
            self._next_health_check_at is not None
            and now >= self._next_health_check_at
            and state in {SessionState.READY, SessionState.LIVE, SessionState.QUERYING}
        ):
            self._next_health_check_at = now + self._health_interval_s
            self._run_health_check_from_worker()
            return True

        return False

    # ------------------------------------------------------------
    # USB-driven entry points
    # ------------------------------------------------------------

    def handle_usb_connected(self, payload=None):
        print(f"[SDK] handle_usb_connected payload={payload}")
        with self._lock:
            self._usb_present = True

        if self._stopping:
            return

        self._enqueue_command(WorkerCommand.USB_CONNECTED, payload)

    def handle_usb_disconnected(self):
        print("[SDK] handle_usb_disconnected()")
        with self._lock:
            self._usb_present = False

        self._enqueue_command(WorkerCommand.USB_DISCONNECTED)

    def _handle_usb_connected_from_worker(self, payload):
        if self._stopping or self._current_state() is SessionState.STOPPED:
            return

        if self._safe_is_session_open():
            print("[SDK] usb connect ignored: session already open")
            return

        self._schedule_reconnect_from_worker(delay=0.0)

    def _handle_usb_disconnected_from_worker(self):
        self._handle_session_lost_from_worker(
            "usb disconnected", emit_signal=True, allow_reconnect=False
        )

    # ------------------------------------------------------------
    # Connection / session
    # ------------------------------------------------------------

    def connect_camera(self):
        self._enqueue_command(WorkerCommand.CONNECT)

    def disconnect_camera(self):
        self.handle_usb_disconnected()

    def _connect_if_possible_from_worker(self):
        print(
            f"[SDK] _connect_if_possible_from_worker() "
            f"stopping={self._stopping} usb_present={self._usb_present} "
            f"state={self._current_state().name}"
        )

        if self._stopping or not self._usb_present:
            return

        if self._safe_is_session_open():
            print("[SDK] connect skipped: session already open")
            return

        self._set_session_state(SessionState.CONNECTING, "usb present -> trying connect")

        try:
            descriptor = self.adapter.connect_camera()
            print(
                f"[SDK] connect_camera ok model={descriptor.model} serial={descriptor.serial}"
            )
        except Exception as exc:
            print(f"[SDK] connect failed: {exc}")
            self.error.emit(str(exc))
            self._set_session_state(SessionState.WAITING_USB, f"connect failed: {exc}")
            self._schedule_reconnect_from_worker()
            return

        if self._try_start_live_view_from_worker("camera connected"):
            pass
        else:
            self._set_session_state(SessionState.READY, "camera connected without live view")
            self._next_health_check_at = time.monotonic() + self._health_interval_s

        self.camera_connected.emit(
            {
                "label": f"{descriptor.model} connected",
                "serial": descriptor.serial,
            }
        )

        # Keep precheck available for debugging, but never run it automatically.
        # It changes camera priority mode and was a source of unstable startup flows.
        self._schedule_pending_commands_from_worker()

    def _handle_session_lost_from_worker(
        self, reason, *, emit_signal=True, allow_reconnect=True
    ):
        print(f"[SDK] _handle_session_lost_from_worker reason={reason}")

        was_connected = self._safe_is_session_open()
        self._clear_runtime_deadlines_from_worker()

        if was_connected:
            try:
                self.adapter.disconnect_camera()
            except Exception as exc:
                print(f"[SDK] adapter.disconnect_camera ignored: {exc}")

        if emit_signal and was_connected:
            self.camera_disconnected.emit()

        self._set_session_state(SessionState.WAITING_USB, reason)

        if allow_reconnect and self._usb_present and not self._stopping:
            self._schedule_reconnect_from_worker()
        else:
            self._next_reconnect_at = None

    # ------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------

    def start_live_view(self):
        self._enqueue_command(WorkerCommand.START_LIVE_VIEW)

    def stop_live_view(self):
        self._enqueue_command(WorkerCommand.STOP_LIVE_VIEW)

    def _try_start_live_view_from_worker(self, reason):
        print(f"[SDK] _try_start_live_view_from_worker reason={reason}")

        if self._stopping:
            return False

        if not self._safe_is_session_open():
            self._handle_session_lost_from_worker(
                f"start_live_view: session not open", emit_signal=True
            )
            return False

        try:
            self.adapter.begin_live_view()
        except Exception as exc:
            print(f"[SDK] start_live_view failed: {exc}")
            self._next_live_poll_at = None
            self._next_health_check_at = time.monotonic() + self._health_interval_s

            if self._is_disconnect_error(exc):
                self._handle_session_lost_from_worker(str(exc), emit_signal=True)
            else:
                # Keep the session alive on operational errors. The UI can still
                # recover, refresh settings, or let the operator fix the camera state.
                self.error.emit(str(exc))
                self._set_session_state(SessionState.READY, str(exc))
            return False

        self._set_session_state(SessionState.LIVE, reason)
        self._next_live_poll_at = time.monotonic() + self._live_interval_s
        self._next_health_check_at = time.monotonic() + self._health_interval_s
        return True

    def _stop_live_view_from_worker(self, reason):
        print(f"[SDK] _stop_live_view_from_worker reason={reason}")

        if self._current_state() is not SessionState.LIVE:
            return

        self._next_live_poll_at = None

        if not self._safe_is_session_open():
            return

        try:
            self.adapter.end_live_view()
        except Exception as exc:
            print(f"[SDK] end_live_view ignored: {exc}")

        if self._safe_is_session_open():
            self._set_session_state(SessionState.READY, reason)
            self._next_health_check_at = time.monotonic() + self._health_interval_s

    def _poll_live_view_from_worker(self):
        if self._stopping or self._current_state() is not SessionState.LIVE:
            return

        if not self._safe_is_session_open():
            self._handle_session_lost_from_worker("live poll: session closed")
            return

        try:
            frame = self.adapter.get_live_view_frame()
        except Exception as exc:
            print(f"[SDK] get_live_view_frame failed: {exc}")
            if self._is_disconnect_error(exc):
                self._handle_session_lost_from_worker(str(exc), emit_signal=True)
            else:
                self.error.emit(str(exc))
                self._set_session_state(SessionState.READY, str(exc))
                self._next_live_poll_at = None
            return

        if frame is None or frame.isNull():
            return

        self.live_view_updated.emit(frame)

    # ------------------------------------------------------------
    # Health
    # ------------------------------------------------------------

    def _run_health_check_from_worker(self):
        print(
            f"[SDK] _run_health_check_from_worker() "
            f"state={self._current_state().name} usb_present={self._usb_present}"
        )

        if self._stopping:
            return

        if not self._usb_present:
            self._handle_session_lost_from_worker(
                "health check: usb flag absent", emit_signal=True, allow_reconnect=False
            )
            return

        if not self._safe_is_session_open():
            self._handle_session_lost_from_worker("health check failed", emit_signal=True)

    # ------------------------------------------------------------
    # Exposure control
    # ------------------------------------------------------------

    def request_exposure_data(self):
        print(f"[SDK] request_exposure_data() state={self._current_state().name}")

        self._exposure_refresh_requested = True
        if not self._exposure_refresh_scheduled:
            self._exposure_refresh_scheduled = True
            self._enqueue_command(WorkerCommand.REFRESH_EXPOSURE)

    def get_exposure_data(self):
        raise RuntimeError("Exposure data is now asynchronous. Use request_exposure_data().")

    def set_exposure(self, *, iso=None, shutter=None, aperture=None, ae_mode=None):
        print(
            f"[SDK] set_exposure() state={self._current_state().name} "
            f"iso={iso} shutter={shutter} aperture={aperture} ae_mode={ae_mode}"
        )

        self._latest_exposure_request = {
            "iso": iso,
            "shutter": shutter,
            "aperture": aperture,
            "ae_mode": ae_mode,
        }

        if not self._exposure_apply_scheduled:
            self._exposure_apply_scheduled = True
            self._enqueue_command(WorkerCommand.APPLY_EXPOSURE)

    def _process_pending_exposure_refresh_from_worker(self):
        state = self._current_state()
        if state not in {SessionState.READY, SessionState.LIVE}:
            print(
                f"[SDK] exposure refresh deferred: backend state={state.name}"
            )
            self._exposure_refresh_scheduled = False
            self._schedule_pending_commands_from_worker()
            return

        self._exposure_refresh_requested = False

        if not self._safe_is_session_open():
            self._exposure_refresh_scheduled = False
            self.exposure_data_failed.emit("Camera SDK session not open")
            self._handle_session_lost_from_worker("async load lost session", emit_signal=True)
            return

        was_live = state is SessionState.LIVE
        if was_live:
            self._stop_live_view_from_worker("paused for exposure query")

        self._set_session_state(SessionState.QUERYING, "async exposure data query")

        try:
            options = self.adapter.get_exposure_options()
            current = self.adapter.get_exposure_state()
            self.exposure_data_ready.emit({"options": options, "state": current})
        except Exception as exc:
            print(f"[SDK] exposure data query failed: {exc}")
            self.exposure_data_failed.emit(str(exc))
            if self._is_disconnect_error(exc):
                self._handle_session_lost_from_worker(str(exc), emit_signal=True)
                self._exposure_refresh_scheduled = False
                return
        finally:
            if was_live and self._safe_is_session_open() and not self._stopping:
                self._try_start_live_view_from_worker("live restored after exposure query")
            elif self._safe_is_session_open() and not self._stopping:
                self._set_session_state(SessionState.READY, "exposure query complete")
                self._next_health_check_at = time.monotonic() + self._health_interval_s

        self._exposure_refresh_scheduled = False
        self._schedule_pending_commands_from_worker()

    def _process_pending_exposure_apply_from_worker(self):
        payload = self._latest_exposure_request
        self._latest_exposure_request = None

        state = self._current_state()
        if payload is None:
            self._exposure_apply_scheduled = False
            return

        if state not in {SessionState.READY, SessionState.LIVE}:
            print(f"[SDK] exposure apply deferred: backend state={state.name}")
            self._latest_exposure_request = payload
            self._exposure_apply_scheduled = False
            self._schedule_pending_commands_from_worker()
            return

        if not self._safe_is_session_open():
            self._latest_exposure_request = payload
            self._exposure_apply_scheduled = False
            self._handle_session_lost_from_worker("set_exposure: session not open", emit_signal=True)
            return

        self.emit_backend_state(BackendState.UPDATING_CAMERA_PARAMS)

        was_live = state is SessionState.LIVE
        if was_live:
            self._stop_live_view_from_worker("paused for exposure change")

        try:
            self.adapter.set_exposure(
                iso=payload.get("iso"),
                shutter=payload.get("shutter"),
                aperture=payload.get("aperture"),
                ae_mode=payload.get("ae_mode"),
            )
        except Exception as exc:
            print(f"[SDK] set_exposure failed: {exc}")
            self.error.emit(str(exc))
            if self._is_disconnect_error(exc):
                self._handle_session_lost_from_worker(str(exc), emit_signal=True)
                self._exposure_apply_scheduled = False
                return
        finally:
            if was_live and self._safe_is_session_open() and not self._stopping:
                self._try_start_live_view_from_worker("live restored after exposure change")
            elif self._safe_is_session_open() and not self._stopping:
                self._set_session_state(SessionState.READY, "exposure change complete")
                self._next_health_check_at = time.monotonic() + self._health_interval_s

        # Always request a fresh readback after setters. This keeps the UI in sync
        # with the effective camera state, including auto-disabled controls.
        self._exposure_refresh_requested = True
        self._exposure_apply_scheduled = False
        self._schedule_pending_commands_from_worker()

    # ------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------

    def trigger_capture(self):
        print(f"[SDK] trigger_capture() state={self._current_state().name}")
        self._enqueue_command(WorkerCommand.CAPTURE)

    def _capture_photo_from_worker(self):
        state = self._current_state()
        if state not in {SessionState.READY, SessionState.LIVE}:
            print(f"[SDK] capture skipped: invalid state={state.name}")
            return

        if not self._safe_is_session_open():
            self._handle_session_lost_from_worker("trigger_capture: session not open", emit_signal=True)
            return

        was_live = state is SessionState.LIVE
        self._set_session_state(SessionState.CAPTURING, "capture requested")
        self._clear_runtime_deadlines_from_worker()

        try:
            path = self.adapter.capture_photo(self.capture_dir)
        except Exception as exc:
            print(f"[SDK] capture failed: {exc}")
            self.error.emit(str(exc))
            if self._is_disconnect_error(exc):
                self._handle_session_lost_from_worker(str(exc), emit_signal=True)
                return

            if was_live and self._safe_is_session_open() and not self._stopping:
                self._try_start_live_view_from_worker("recovered after capture error")
            elif self._safe_is_session_open() and not self._stopping:
                self._set_session_state(SessionState.READY, "capture error recovery")
            return

        self.emit_backend_state(BackendState.DOWNLOADING)
        if self.repository is not None:
            try:
                path = self.repository.store(path)
            except Exception as exc:
                print(f"[SDK] repository.store failed: {exc}")
                self.error.emit(str(exc))
                return
        self._emit_photo(path)

        if was_live and self._safe_is_session_open() and not self._stopping:
            self._try_start_live_view_from_worker("live restored after capture")
        elif self._safe_is_session_open() and not self._stopping:
            self._set_session_state(SessionState.READY, "capture complete")
            self._next_health_check_at = time.monotonic() + self._health_interval_s
