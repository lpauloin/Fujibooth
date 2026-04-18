from __future__ import annotations

import platform
import threading
import time
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from PySide6.QtGui import QPixmap

from .adapter import CameraDescriptor
from .xsdk_ctypes import (
    CameraPrecheckReport,
    FujiSdkLibrary,
    SDK_LIVEVIEW_MODE1,
    SDK_LIVEVIEW_QUALITY_FINE,
    SDK_LIVEVIEW_SIZE_L,
    XSDK_IMAGEFORMAT_HEIF,
    XSDK_IMAGEFORMAT_JPEG,
    XSDK_IMAGEFORMAT_LIVE,
    XSDK_IMAGEFORMAT_NONE,
    XSDK_IMAGEFORMAT_RAW,
    XSDK_MEDIAREC_RAWJPEG,
    XSDK_PRIORITY_CAMERA,
    XSDK_PRIORITY_PC,
    image_suffix_for_format,
)


class FujifilmSdkAdapter:
    def __init__(self, sdk_root: str | Path, xapi_path: str | Path | None = None) -> None:
        self.sdk_root = Path(sdk_root).expanduser().resolve()
        self.xapi_path = Path(xapi_path).expanduser().resolve() if xapi_path else None

        self.lib: FujiSdkLibrary | None = None
        self.camera_handle = None

        self._connected = False
        self._live_view_enabled = False
        self._capture_in_progress = False
        self._stopping = False

        self._last_frame = QPixmap()
        self._last_captured_path: Path | None = None
        self._last_capture_error: str | None = None

        self._state_lock = threading.RLock()
        self._worker_thread: threading.Thread | None = None
        self._worker_stop = threading.Event()
        self._worker_commands: Queue[tuple[str, object]] = Queue()
        self._min_live_poll_interval_s = 0.10

        print(f"[SDK-WRAPPER] init sdk_root={self.sdk_root} xapi_path={self.xapi_path}")

    def _ensure_session(self) -> tuple[FujiSdkLibrary, object]:
        with self._state_lock:
            if self.lib is None or self.camera_handle is None or not self._connected:
                raise RuntimeError("Session camera SDK non ouverte")
            return self.lib, self.camera_handle

    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    def start(self) -> None:
        with self._state_lock:
            print("[SDK-WRAPPER] start()")
            self._stopping = False
            self.lib = FujiSdkLibrary(self.sdk_root, self.xapi_path)
            self.lib.load()
            self.lib.init_sdk()

    def stop(self) -> None:
        print("[SDK-WRAPPER] stop()")
        with self._state_lock:
            self._stopping = True
            self._live_view_enabled = False
            self._capture_in_progress = False
        self._stop_worker()

        lib = self.lib
        handle = self.camera_handle
        try:
            if lib is not None and handle is not None:
                try:
                    lib.stop_live_view(handle)
                except Exception as exc:
                    print(f"[SDK-WRAPPER] stop_live_view ignore: {exc}")
                try:
                    lib.close_camera(handle)
                except Exception as exc:
                    print(f"[SDK-WRAPPER] close_camera ignore: {exc}")
        finally:
            self.camera_handle = None
            self._connected = False
            if self.lib is not None:
                try:
                    self.lib.exit_sdk()
                except Exception as exc:
                    print(f"[SDK-WRAPPER] exit_sdk ignore: {exc}")
                self.lib.unload()
                self.lib = None

    def connect_camera(self) -> CameraDescriptor:
        if self.lib is None:
            raise RuntimeError("SDK non demarre")

        print("[SDK-WRAPPER] connect_camera()")
        if platform.system() == "Darwin":
            print("[SDK-WRAPPER] macOS detecte -> attente 3s avant detect/open")
            time.sleep(3)

        count = self.lib.detect_usb_cameras()
        if count <= 0:
            raise RuntimeError("Aucune camera FUJIFILM detectee par le SDK")

        print("[SDK-WRAPPER] attente 1s avant XSDK_OpenEx")
        time.sleep(1)
        self.camera_handle, info = self.lib.open_first_camera()

        print("[SDK-WRAPPER] set CAMERA priority after open")
        self.lib.set_priority_mode(self.camera_handle, XSDK_PRIORITY_CAMERA)
        print("[SDK-WRAPPER] set media record RAWJPEG")
        self.lib.set_media_record(self.camera_handle, XSDK_MEDIAREC_RAWJPEG)

        with self._state_lock:
            self._connected = True
            self._live_view_enabled = False
            self._capture_in_progress = False
            self._stopping = False
            self._last_captured_path = None
            self._last_capture_error = None

        descriptor = CameraDescriptor(model=info.model, serial=info.serial, connection="usb")
        print(f"[SDK-WRAPPER] camera connectee -> {descriptor}")
        return descriptor

    def disconnect_camera(self) -> None:
        print("[SDK-WRAPPER] disconnect_camera()")
        self._stop_worker()

        lib = self.lib
        handle = self.camera_handle
        if lib is None or handle is None:
            with self._state_lock:
                self._connected = False
                self._live_view_enabled = False
                self._capture_in_progress = False
            return

        try:
            try:
                lib.stop_live_view(handle)
            except Exception as exc:
                print(f"[SDK-WRAPPER] stop_live_view ignore: {exc}")
            lib.close_camera(handle)
        finally:
            with self._state_lock:
                self.camera_handle = None
                self._connected = False
                self._live_view_enabled = False
                self._capture_in_progress = False

    def _start_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        print("[SDK-WRAPPER] start worker sdk")
        self._worker_stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="fuji-sdk-worker", daemon=True)
        self._worker_thread.start()

    def _stop_worker(self) -> None:
        if self._worker_thread is None:
            return
        print("[SDK-WRAPPER] stop worker sdk")
        self._worker_stop.set()
        self._worker_commands.put(("stop", None))
        self._worker_thread.join(timeout=3.0)
        self._worker_thread = None

    def _worker_loop(self) -> None:
        print("[SDK-WRAPPER] worker loop start")
        live_started = False
        last_poll = 0.0
        try:
            lib, handle = self._ensure_session()
            while not self._worker_stop.is_set():
                try:
                    cmd, payload = self._worker_commands.get(timeout=0.02)
                except Empty:
                    cmd, payload = None, None

                if cmd == "stop":
                    break
                if cmd == "start_live_view":
                    if not live_started:
                        try:
                            lib.start_live_view(handle, quality=SDK_LIVEVIEW_QUALITY_FINE, size=SDK_LIVEVIEW_SIZE_L, mode=SDK_LIVEVIEW_MODE1)
                            with self._state_lock:
                                self._live_view_enabled = True
                            live_started = True
                            print("[SDK-WRAPPER] live view enabled (worker)")
                        except Exception as exc:
                            print(f"[SDK-WRAPPER] start_live_view worker error: {exc}")
                elif cmd == "stop_live_view":
                    if live_started:
                        try:
                            lib.stop_live_view(handle)
                        except Exception as exc:
                            print(f"[SDK-WRAPPER] stop_live_view worker error: {exc}")
                        with self._state_lock:
                            self._live_view_enabled = False
                        live_started = False
                        print("[SDK-WRAPPER] live view disabled (worker)")
                elif cmd == "capture":
                    output_dir: Path = payload  # type: ignore[assignment]
                    try:
                        self._do_capture_in_worker(lib, handle, output_dir, live_started)
                    except Exception as exc:
                        print(f"[SDK-WRAPPER] capture worker error: {exc}")
                        self._last_capture_error = str(exc)
                    finally:
                        with self._state_lock:
                            self._capture_in_progress = False

                now = time.monotonic()
                if live_started and not self._capture_in_progress and now - last_poll >= self._min_live_poll_interval_s:
                    last_poll = now
                    try:
                        self._poll_one_live_frame(lib, handle)
                    except Exception as exc:
                        print(f"[SDK-WRAPPER] poll live frame error: {exc}")
        finally:
            print("[SDK-WRAPPER] worker loop end")

    def _poll_one_live_frame(self, lib: FujiSdkLibrary, handle: object) -> None:
        info = lib.read_image_info(handle)
        fmt = int(info.format) & 0xFF
        size = int(info.data_size)
        if fmt == XSDK_IMAGEFORMAT_NONE or size <= 0:
            return
        raw = lib.read_image(handle, size)
        print(f"[SDK-WRAPPER] buffer item fmt={fmt} size={len(raw)}")
        if fmt != XSDK_IMAGEFORMAT_LIVE:
            return
        pixmap = QPixmap()
        if pixmap.loadFromData(raw):
            self._last_frame = pixmap
        else:
            raise RuntimeError("Impossible de decoder la frame live view")

    def _do_capture_in_worker(self, lib: FujiSdkLibrary, handle: object, output_dir: Path, live_started: bool) -> None:
        print(f"[SDK-WRAPPER] capture in worker output_dir={output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)
        self._last_captured_path = None
        self._last_capture_error = None

        if live_started:
            try:
                lib.stop_live_view(handle)
                with self._state_lock:
                    self._live_view_enabled = False
                time.sleep(0.5)
            except Exception as exc:
                print(f"[SDK-WRAPPER] stop live before capture warning: {exc}")

        print("[SDK-WRAPPER] switch to PC PRIORITY for capture")
        lib.set_priority_mode(handle, XSDK_PRIORITY_PC)
        time.sleep(0.5)

        print("[SDK-WRAPPER] release in PC PRIORITY")
        lib.release_pc(handle)
        time.sleep(0.5)

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            info = lib.read_image_info(handle)
            fmt = int(info.format) & 0xFF
            size = int(info.data_size)
            if fmt == XSDK_IMAGEFORMAT_NONE or size <= 0:
                time.sleep(0.05)
                continue

            raw = lib.read_image(handle, size)
            print(f"[SDK-WRAPPER] capture buffer item fmt={fmt} size={len(raw)}")
            if fmt in {XSDK_IMAGEFORMAT_RAW, XSDK_IMAGEFORMAT_JPEG, XSDK_IMAGEFORMAT_HEIF}:
                suffix = image_suffix_for_format(fmt)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                path = output_dir / f"capture_{timestamp}{suffix}"
                path.write_bytes(raw)
                self._last_captured_path = path
                print(f"[SDK-WRAPPER] capture saved -> {path}")
                break
        else:
            raise RuntimeError("Timeout en attente de l'image capturee dans le buffer SDK")

        if live_started:
            try:
                lib.start_live_view(handle, quality=SDK_LIVEVIEW_QUALITY_FINE, size=SDK_LIVEVIEW_SIZE_L, mode=SDK_LIVEVIEW_MODE1)
                with self._state_lock:
                    self._live_view_enabled = True
            except Exception as exc:
                print(f"[SDK-WRAPPER] restart live view after capture warning: {exc}")

    def begin_live_view(self) -> None:
        self._ensure_session()
        self._start_worker()
        self._worker_commands.put(("start_live_view", None))

    def end_live_view(self) -> None:
        if self._worker_thread is not None:
            self._worker_commands.put(("stop_live_view", None))

    def get_live_view_frame(self) -> QPixmap:
        return self._last_frame

    def enqueue_capture(self, output_dir: Path) -> None:
        with self._state_lock:
            self._capture_in_progress = True
            self._last_captured_path = None
            self._last_capture_error = None
        self._start_worker()
        self._worker_commands.put(("capture", output_dir))

    def pop_capture_result(self) -> tuple[Path | None, str | None]:
        with self._state_lock:
            path = self._last_captured_path
            err = self._last_capture_error
            self._last_captured_path = None
            self._last_capture_error = None
            return path, err

    def run_precheck_safe(self) -> CameraPrecheckReport:
        lib, handle = self._ensure_session()
        print("[SDK-WRAPPER] run_precheck_safe()")
        return lib.run_precheck(handle)

    def run_precheck(self) -> CameraPrecheckReport:
        lib, handle = self._ensure_session()
        print("[SDK-WRAPPER] run_precheck()")
        return lib.run_precheck(handle)

    def format_precheck_summary(self, report: CameraPrecheckReport) -> str:
        lines = [
            f"Connecte: {'oui' if report.connected else 'non'}",
            f"Mode: {report.mode_value}",
            f"Drive mode: {report.drive_mode_value}",
            f"Priority mode: {report.priority_mode_value}",
            f"Release status: {report.release_status_value}",
            f"CapRelease (PC): {report.cap_release}",
            f"CapReleaseEx (Camera): {report.cap_release_ex}",
        ]
        if report.issues:
            lines.append("Problemes:")
            for issue in report.issues:
                lines.append(f"- [{issue.level}] {issue.message}")
        else:
            lines.append("Aucun probleme detecte.")
        return "\n".join(lines)
