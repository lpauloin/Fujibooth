from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer

from ..config import Settings
from ..sdk_bridge.adapter import CameraSdkAdapter
from ..sdk_bridge.wrapper import FujifilmSdkAdapter
from .base import CameraBackend
from .state import BackendState


class FujifilmSdkBackend(CameraBackend):
    def __init__(
        self,
        settings: Settings,
        *,
        adapter: CameraSdkAdapter | None = None,
        capture_dir: Path | None = None,
        live_view_interval_ms: int | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self._started = False
        self._live_view_running = False
        self._stopping = False

        self.adapter = adapter or FujifilmSdkAdapter(
            sdk_root=settings.camera.sdk.sdk_root,
            xapi_path=settings.camera.sdk.library_path or None,
        )

        self.capture_dir = capture_dir or settings.sdk_capture_path
        self.capture_dir.mkdir(parents=True, exist_ok=True)

        interval_ms = live_view_interval_ms or settings.camera.sdk.live_view_interval_ms
        self.live_timer = QTimer(self)
        self.live_timer.setInterval(max(120, int(interval_ms)))
        self.live_timer.timeout.connect(self._pull_live_frame)

        self.capture_result_timer = QTimer(self)
        self.capture_result_timer.setInterval(150)
        self.capture_result_timer.timeout.connect(self._poll_capture_result)

        print(f"[SDK] backend init adapter={self.adapter.__class__.__name__} capture_dir={self.capture_dir}")

    def start(self) -> None:
        if self._started:
            return

        print("[SDK] start()")
        self._stopping = False

        try:
            self.adapter.start()
            print("[SDK] tentative de connexion camera Fujifilm")
            descriptor = self.adapter.connect_camera()

            try:
                report = self.adapter.run_precheck_safe()
                print(self.adapter.format_precheck_summary(report))
            except Exception as exc:
                print(f"[SDK] precheck warning: {exc}")

            print(f"[SDK] connect_camera() -> model={descriptor.model} serial={descriptor.serial}")
            self.camera_connected.emit({"label": f"{descriptor.model} connecté", "serial": descriptor.serial})
            self.emit_backend_state(BackendState.CAMERA_READY)

            self._started = True
            self.start_live_view()
        except Exception as exc:
            print(f"[SDK] erreur start: {exc}")
            self._started = False
            self.error.emit(str(exc))
            self.emit_backend_state(BackendState.WAITING_FOR_CAMERA)

    def stop(self) -> None:
        print("[SDK] stop()")
        self._stopping = True
        self.capture_result_timer.stop()
        self.stop_live_view()

        try:
            self.adapter.disconnect_camera()
        except Exception as exc:
            print(f"[SDK] disconnect_camera ignore: {exc}")
        finally:
            try:
                self.adapter.stop()
            except Exception as exc:
                print(f"[SDK] adapter.stop ignore: {exc}")

        self._started = False
        self._live_view_running = False

    def is_connected(self) -> bool:
        try:
            connected = self.adapter.is_connected()
            print(f"[SDK] is_connected() -> {connected}")
            return connected
        except Exception as exc:
            print(f"[SDK] is_connected() error: {exc}")
            return False

    def connect_camera(self) -> None:
        descriptor = self.adapter.connect_camera()
        self.camera_connected.emit({"label": f"{descriptor.model} connecté", "serial": descriptor.serial})
        self.emit_backend_state(BackendState.CAMERA_READY)

    def disconnect_camera(self) -> None:
        self.stop_live_view()
        self.adapter.disconnect_camera()
        self.camera_disconnected.emit()
        self.emit_backend_state(BackendState.WAITING_FOR_CAMERA)

    def start_live_view(self) -> None:
        print("[SDK] start_live_view()")
        if self._stopping or not self.is_connected() or self._live_view_running:
            return
        try:
            self.adapter.begin_live_view()
            self._live_view_running = True
            self.live_timer.start()
            self.emit_backend_state(BackendState.LIVE_VIEW)
        except Exception as exc:
            self._live_view_running = False
            print(f"[SDK] erreur start_live_view: {exc}")
            self.error.emit(str(exc))
            self.emit_backend_state(BackendState.WAITING_FOR_CAMERA)

    def stop_live_view(self) -> None:
        print("[SDK] stop_live_view()")
        self.live_timer.stop()
        if not self._live_view_running:
            return
        try:
            self.adapter.end_live_view()
        except Exception as exc:
            print(f"[SDK] end_live_view ignore: {exc}")
        finally:
            self._live_view_running = False

    def trigger_capture(self) -> None:
        print("[SDK] trigger_capture()")
        if self._stopping:
            return
        if not self.is_connected():
            self.error.emit("Camera not connected")
            self.emit_backend_state(BackendState.WAITING_FOR_CAMERA)
            return
        self.emit_backend_state(BackendState.CAPTURING)
        try:
            self.adapter.enqueue_capture(self.capture_dir)
            self.capture_result_timer.start()
        except Exception as exc:
            print(f"[SDK] trigger_capture error: {exc}")
            self.error.emit(str(exc))
            self.emit_backend_state(BackendState.LIVE_VIEW)

    def _poll_capture_result(self) -> None:
        if not self.is_connected():
            self.capture_result_timer.stop()
            return
        path, err = self.adapter.pop_capture_result()
        if err:
            self.capture_result_timer.stop()
            self.error.emit(err)
            self.emit_backend_state(BackendState.LIVE_VIEW)
            return
        if path is None:
            return
        self.capture_result_timer.stop()
        print(f"[SDK] capture result -> {path}")
        self.emit_backend_state(BackendState.DOWNLOADING)
        self._emit_photo(path)
        self.emit_backend_state(BackendState.LIVE_VIEW)

    def _pull_live_frame(self) -> None:
        if self._stopping or not self.is_connected():
            return
        try:
            frame = self.adapter.get_live_view_frame()
        except Exception as exc:
            print(f"[SDK] get_live_view_frame error: {exc}")
            return
        if frame.isNull():
            return
        print(f"[SDK] frame live view recue null={frame.isNull()} size={frame.size()}")
        self.live_view_updated.emit(frame)
