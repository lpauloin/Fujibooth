from __future__ import annotations

import platform
import threading
import time
from datetime import datetime
from pathlib import Path

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

        self._state_lock = threading.RLock()
        self._sdk_lock = threading.RLock()

        self._connected = False
        self._live_view_enabled = False
        self._capture_in_progress = False
        self._stopping = False

        self._last_frame = QPixmap()

        self._capture_thread: threading.Thread | None = None
        self._last_captured_path: Path | None = None
        self._last_capture_error: str | None = None

        self._camera_opened_monotonic: float | None = None
        self._min_live_poll_interval_s = 0.10

        print(f"[SDK-WRAPPER] init sdk_root={self.sdk_root} xapi_path={self.xapi_path}")

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _ensure_session(self) -> tuple[FujiSdkLibrary, object]:
        with self._state_lock:
            if self.lib is None or self.camera_handle is None or not self._connected:
                raise RuntimeError("Session camera SDK non ouverte")
            return self.lib, self.camera_handle

    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        with self._state_lock, self._sdk_lock:
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

        if self._capture_thread is not None and self._capture_thread.is_alive():
            print("[SDK-WRAPPER] waiting capture thread to finish")
            self._capture_thread.join(timeout=15.0)

        with self._sdk_lock:
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
                with self._state_lock:
                    self.camera_handle = None
                    self._connected = False
                    self._live_view_enabled = False
                    self._capture_in_progress = False
                    self._camera_opened_monotonic = None

                if self.lib is not None:
                    try:
                        self.lib.exit_sdk()
                    except Exception as exc:
                        print(f"[SDK-WRAPPER] exit_sdk ignore: {exc}")
                    self.lib.unload()
                    self.lib = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_camera(self) -> CameraDescriptor:
        if self.lib is None:
            raise RuntimeError("SDK non demarre")

        print("[SDK-WRAPPER] connect_camera()")

        detect_attempts = 4
        open_attempts_per_detect = 3
        last_error: Exception | None = None

        with self._sdk_lock:
            for detect_index in range(1, detect_attempts + 1):
                if platform.system() == "Darwin":
                    wait_before_detect = 3.0 if detect_index == 1 else 1.5
                    print(
                        f"[SDK-WRAPPER] macOS detecte -> attente {wait_before_detect:.1f}s "
                        f"avant detect/open (detect attempt {detect_index}/{detect_attempts})"
                    )
                    time.sleep(wait_before_detect)

                try:
                    count = self.lib.detect_usb_cameras()
                except Exception as exc:
                    last_error = exc
                    print(f"[SDK-WRAPPER] detect attempt {detect_index} failed: {exc}")
                    time.sleep(1.0)
                    continue

                print(f"[SDK-WRAPPER] detect attempt {detect_index} -> count={count}")

                if count <= 0:
                    last_error = RuntimeError("Aucune camera FUJIFILM detectee par le SDK")
                    time.sleep(1.0)
                    continue

                for open_index in range(1, open_attempts_per_detect + 1):
                    try:
                        delay = 1.0 if open_index == 1 else 2.0
                        print(
                            f"[SDK-WRAPPER] attente {delay:.1f}s avant XSDK_OpenEx "
                            f"(open attempt {open_index}/{open_attempts_per_detect})"
                        )
                        time.sleep(delay)

                        self.camera_handle, info = self.lib.open_first_camera()

                        print("[SDK-WRAPPER] set CAMERA priority after open")
                        self.lib.set_priority_mode(self.camera_handle, XSDK_PRIORITY_CAMERA)

                        print("[SDK-WRAPPER] set media record RAWJPEG")
                        self.lib.set_media_record(self.camera_handle, XSDK_MEDIAREC_RAWJPEG)

                        print("[SDK-WRAPPER] settle after open")
                        time.sleep(1.0)

                        with self._state_lock:
                            self._connected = True
                            self._live_view_enabled = False
                            self._capture_in_progress = False
                            self._stopping = False
                            self._last_captured_path = None
                            self._last_capture_error = None
                            self._camera_opened_monotonic = time.monotonic()

                        descriptor = CameraDescriptor(
                            model=info.model,
                            serial=info.serial,
                            connection="usb",
                        )
                        print(f"[SDK-WRAPPER] camera connectee -> {descriptor}")
                        return descriptor

                    except Exception as exc:
                        last_error = exc
                        print(
                            f"[SDK-WRAPPER] open attempt {open_index}/{open_attempts_per_detect} "
                            f"failed after detect success: {exc}"
                        )

                        try:
                            if self.camera_handle is not None and self.lib is not None:
                                print("[SDK-WRAPPER] cleanup partial open session")
                                self.lib.close_camera(self.camera_handle)
                        except Exception as cleanup_exc:
                            print(f"[SDK-WRAPPER] cleanup partial open ignore: {cleanup_exc}")
                        finally:
                            self.camera_handle = None
                            with self._state_lock:
                                self._connected = False
                                self._live_view_enabled = False
                                self._capture_in_progress = False
                                self._camera_opened_monotonic = None

                        time.sleep(1.5)

                print("[SDK-WRAPPER] detect succeeded but all open attempts failed; retry full detect cycle")
                time.sleep(1.5)

        if last_error is not None:
            raise RuntimeError(f"Impossible d'ouvrir la camera via le SDK: {last_error}")

        raise RuntimeError("Impossible d'ouvrir la camera via le SDK")

    def disconnect_camera(self) -> None:
        print("[SDK-WRAPPER] disconnect_camera()")

        if self._capture_thread is not None and self._capture_thread.is_alive():
            print("[SDK-WRAPPER] waiting capture thread before disconnect")
            self._capture_thread.join(timeout=15.0)

        with self._sdk_lock:
            lib = self.lib
            handle = self.camera_handle

            if lib is None or handle is None:
                with self._state_lock:
                    self._connected = False
                    self._live_view_enabled = False
                    self._capture_in_progress = False
                    self._camera_opened_monotonic = None
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
                    self._camera_opened_monotonic = None

    # ------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------

    def _start_live_view_once(self, lib: FujiSdkLibrary, handle: object) -> None:
        lib.start_live_view(
            handle,
            quality=SDK_LIVEVIEW_QUALITY_FINE,
            size=SDK_LIVEVIEW_SIZE_L,
            mode=SDK_LIVEVIEW_MODE1,
        )

    def begin_live_view(self) -> None:
        lib, handle = self._ensure_session()

        with self._state_lock:
            if self._live_view_enabled:
                print("[SDK-WRAPPER] begin_live_view skipped: already enabled")
                return
            if self._capture_in_progress:
                raise RuntimeError("Impossible de demarrer le live view pendant une capture")
            opened_at = self._camera_opened_monotonic

        with self._sdk_lock:
            if opened_at is not None:
                elapsed = time.monotonic() - opened_at
                if elapsed < 1.5:
                    wait_more = 1.5 - elapsed
                    print(f"[SDK-WRAPPER] extra settle before live view: {wait_more:.2f}s")
                    time.sleep(wait_more)

            last_error: Exception | None = None

            for attempt in range(1, 4):
                try:
                    print(f"[SDK-WRAPPER] begin_live_view() attempt {attempt}/3")
                    self._start_live_view_once(lib, handle)
                    with self._state_lock:
                        self._live_view_enabled = True
                    print("[SDK-WRAPPER] live view enabled")
                    return

                except Exception as exc:
                    last_error = exc
                    print(f"[SDK-WRAPPER] begin_live_view attempt {attempt} failed: {exc}")

                    try:
                        print("[SDK-WRAPPER] soft reset after live view failure")
                        try:
                            lib.stop_live_view(handle)
                        except Exception as stop_exc:
                            print(f"[SDK-WRAPPER] soft reset stop_live_view ignore: {stop_exc}")

                        try:
                            lib.set_priority_mode(handle, XSDK_PRIORITY_CAMERA)
                        except Exception as prio_exc:
                            print(f"[SDK-WRAPPER] soft reset set CAMERA ignore: {prio_exc}")

                        time.sleep(0.8)
                    except Exception as reset_exc:
                        print(f"[SDK-WRAPPER] soft reset failed: {reset_exc}")

            raise RuntimeError(f"Impossible de demarrer le live view: {last_error}")

    def end_live_view(self) -> None:
        lib, handle = self._ensure_session()

        with self._state_lock:
            if not self._live_view_enabled:
                print("[SDK-WRAPPER] end_live_view skipped: already disabled")
                return

        with self._sdk_lock:
            print("[SDK-WRAPPER] end_live_view()")
            lib.stop_live_view(handle)

        with self._state_lock:
            self._live_view_enabled = False

    def get_live_view_frame(self) -> QPixmap:
        lib, handle = self._ensure_session()

        with self._state_lock:
            if not self._live_view_enabled or self._capture_in_progress:
                return self._last_frame

        with self._sdk_lock:
            info = lib.read_image_info(handle)
            fmt = int(info.format) & 0xFF
            size = int(info.data_size)

            if fmt == XSDK_IMAGEFORMAT_NONE or size <= 0:
                return self._last_frame

            raw = lib.read_image(handle, size)
            print(f"[SDK-WRAPPER] buffer item fmt={fmt} size={len(raw)}")

        if fmt != XSDK_IMAGEFORMAT_LIVE:
            return self._last_frame

        pixmap = QPixmap()
        if pixmap.loadFromData(raw):
            self._last_frame = pixmap
            return pixmap

        raise RuntimeError("Impossible de decoder la frame live view")

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_worker(self, output_dir: Path) -> None:
        try:
            path = self._do_capture(output_dir)
            with self._state_lock:
                self._last_captured_path = path
                self._last_capture_error = None
        except Exception as exc:
            print(f"[SDK-WRAPPER] capture worker error: {exc}")
            with self._state_lock:
                self._last_captured_path = None
                self._last_capture_error = str(exc)
        finally:
            with self._state_lock:
                self._capture_in_progress = False

    def _do_capture(self, output_dir: Path) -> Path:
        lib, handle = self._ensure_session()

        print(f"[SDK-WRAPPER] capture start output_dir={output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

        with self._state_lock:
            was_live = self._live_view_enabled

        with self._sdk_lock:
            if was_live:
                try:
                    print("[SDK-WRAPPER] stop live view before capture")
                    lib.stop_live_view(handle)
                except Exception as exc:
                    print(f"[SDK-WRAPPER] stop live before capture warning: {exc}")
                with self._state_lock:
                    self._live_view_enabled = False
                time.sleep(0.5)

            print("[SDK-WRAPPER] switch to PC PRIORITY for capture")
            lib.set_priority_mode(handle, XSDK_PRIORITY_PC)
            time.sleep(0.5)

            print("[SDK-WRAPPER] release in PC PRIORITY")
            lib.release_pc(handle)
            time.sleep(0.5)

            deadline = time.monotonic() + 10.0
            captured_path: Path | None = None

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
                    captured_path = output_dir / f"capture_{timestamp}{suffix}"
                    captured_path.write_bytes(raw)
                    print(f"[SDK-WRAPPER] capture saved -> {captured_path}")
                    break

            if captured_path is None:
                raise RuntimeError("Timeout en attente de l'image capturee dans le buffer SDK")

            if was_live and not self._stopping:
                try:
                    print("[SDK-WRAPPER] restart live view after capture")
                    self._start_live_view_once(lib, handle)
                    with self._state_lock:
                        self._live_view_enabled = True
                except Exception as exc:
                    print(f"[SDK-WRAPPER] restart live view after capture warning: {exc}")

            return captured_path

    def enqueue_capture(self, output_dir: Path) -> None:
        with self._state_lock:
            if self._capture_in_progress:
                raise RuntimeError("Une capture est deja en cours")

            self._capture_in_progress = True
            self._last_captured_path = None
            self._last_capture_error = None

        self._capture_thread = threading.Thread(
            target=self._capture_worker,
            args=(output_dir,),
            name="fuji-capture-thread",
            daemon=True,
        )
        self._capture_thread.start()

    def pop_capture_result(self) -> tuple[Path | None, str | None]:
        with self._state_lock:
            path = self._last_captured_path
            err = self._last_capture_error
            self._last_captured_path = None
            self._last_capture_error = None
            return path, err

    # ------------------------------------------------------------------
    # Precheck
    # ------------------------------------------------------------------

    def run_precheck_safe(self) -> CameraPrecheckReport:
        lib, handle = self._ensure_session()
        print("[SDK-WRAPPER] run_precheck_safe()")
        with self._sdk_lock:
            return lib.run_precheck(handle)

    def run_precheck(self) -> CameraPrecheckReport:
        lib, handle = self._ensure_session()
        print("[SDK-WRAPPER] run_precheck()")
        with self._sdk_lock:
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