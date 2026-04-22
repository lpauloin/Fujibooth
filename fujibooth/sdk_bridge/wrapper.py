import platform
import threading
import time
from datetime import datetime
from pathlib import Path
from PySide6.QtGui import QImage

from .adapter import CameraDescriptor
from .xsdk_ctypes import (
    FujiSdkLibrary,
    SDK_LIVEVIEW_MODE1,
    SDK_LIVEVIEW_QUALITY_FINE,
    SDK_LIVEVIEW_SIZE_L,
    XSDK_AE_APERTURE_PRIORITY,
    XSDK_AE_MANUAL,
    XSDK_AE_SHUTTER_PRIORITY,
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

AE_MODE_LABELS = {
    1: "M",
    3: "A",
    4: "S",
    6: "P",
}

# Human-readable shutter speed labels keyed by SDK integer value.
# Values are exposure periods in microseconds as defined in XAPI.H.
SHUTTER_LABELS = {
    -1: "BULB",
    0: "AUTO",
    5: "1/180000",
    6: "1/160000",
    7: "1/128000",
    9: "1/102400",
    12: "1/80000",
    15: "1/64000",
    19: "1/51200",
    24: "1/40000",
    30: "1/32000",
    38: "1/25600",
    43: "1/24000",
    48: "1/20000",
    61: "1/16000",
    76: "1/12800",
    86: "1/12000",
    96: "1/10000",
    122: "1/8000",
    153: "1/6400",
    172: "1/6000",
    193: "1/5000",
    244: "1/4000",
    307: "1/3200",
    345: "1/3000",
    387: "1/2500",
    488: "1/2000",
    615: "1/1600",
    690: "1/1500",
    775: "1/1250",
    976: "1/1000",
    1230: "1/800",
    1381: "1/750",
    1550: "1/640",
    1953: "1/500",
    2460: "1/400",
    2762: "1/350",
    3100: "1/320",
    3906: "1/250",
    4921: "1/200",
    5524: "1/180",
    6200: "1/160",
    7812: "1/125",
    9843: "1/100",
    11048: "1/90",
    12401: "1/80",
    15625: "1/60",
    19686: "1/50",
    22097: "1/45",
    24803: "1/40",
    31250: "1/30",
    39372: "1/25",
    44194: "1/20H",
    49606: "1/20",
    62500: "1/15",
    78745: "1/13",
    88388: "1/10H",
    99212: "1/10",
    125000: "1/8",
    157490: "1/6",
    176776: "1/6H",
    198425: "1/5",
    250000: "1/4",
    314980: "1/3",
    353553: "1/3H",
    396850: "1/2.5",
    500000: "1/2",
    629960: "1/1.6",
    707106: "1/1.5",
    793700: "1/1.3",
    1000000: '1"',
    1259921: '1.3"',
    1414213: '1.5"',
    1587401: '1.6"',
    2000000: '2"',
    2519842: '2.5"',
    2828427: '3"H',
    3174802: '3"',
    4000000: '4"',
    5039684: '5"',
    5656854: '6"H',
    6349604: '6"',
    8000000: '8"',
    10079368: '10"',
    11313708: '10"H',
    12699208: '13"',
    16000000: '15"',
    20158736: '20"',
    22627416: '20"H',
    25398416: '25"',
    32000000: '30"',
}

# Human-readable labels for ISO AUTO sensitivity modes.
ISO_AUTO_LABELS = {
    -1: "ISO AUTO (1)",
    -2: "ISO AUTO (2)",
    -3: "ISO AUTO (3)",
    -4: "ISO AUTO (4)",
    -10: "ISO AUTO",
    -400: "ISO AUTO 400",
    -800: "ISO AUTO 800",
    -1600: "ISO AUTO 1600",
    -3200: "ISO AUTO 3200",
    -6400: "ISO AUTO 6400",
}


def format_iso(value):
    if value in ISO_AUTO_LABELS:
        return ISO_AUTO_LABELS[value]
    if value < 0:
        return f"ISO AUTO {abs(value)}"
    return f"ISO {value}"


def format_shutter(value):
    if value in SHUTTER_LABELS:
        return SHUTTER_LABELS[value]
    if 0 < value < 1_000_000:
        denom = round(1_000_000 / value)
        return f"1/{denom}"
    if value >= 1_000_000:
        secs = value / 1_000_000
        if abs(secs - round(secs)) < 1e-4:
            return f'{int(round(secs))}"'
        return f'{secs:.1f}"'
    return str(value)


def format_aperture(value):
    return f"f/{value / 100:.1f}"


class FujifilmSdkAdapter:
    def __init__(self, sdk_root, xapi_path=None):
        self.sdk_root = Path(sdk_root).expanduser().resolve()
        self.xapi_path = Path(xapi_path).expanduser().resolve() if xapi_path else None

        self.lib = None
        self.camera_handle = None

        self._state_lock = threading.RLock()
        self._sdk_lock = threading.RLock()

        self._connected = False
        self._live_view_enabled = False
        self._capture_in_progress = False
        self._stopping = False

        self._last_frame = QImage()

        self._capture_thread = None
        self._last_captured_path = None
        self._last_capture_error = None

        self._camera_opened_monotonic = None
        self._min_live_poll_interval_s = 0.10

        print(f"[SDK-WRAPPER] init sdk_root={self.sdk_root} xapi_path={self.xapi_path}")

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def ensure_session(self):
        with self._state_lock:
            if self.lib is None or self.camera_handle is None or not self._connected:
                raise RuntimeError("Camera SDK session not open")
            return self.lib, self.camera_handle

    def is_connected(self):
        with self._state_lock:
            return self._connected

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        with self._state_lock, self._sdk_lock:
            print("[SDK-WRAPPER] start()")
            self._stopping = False
            self.lib = FujiSdkLibrary(self.sdk_root, self.xapi_path)
            self.lib.load()
            self.lib.init_sdk()

    def stop(self):
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
                        print(f"[SDK-WRAPPER] stop_live_view ignored: {exc}")

                    try:
                        lib.close_camera(handle)
                    except Exception as exc:
                        print(f"[SDK-WRAPPER] close_camera ignored: {exc}")
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
                        print(f"[SDK-WRAPPER] exit_sdk ignored: {exc}")
                    self.lib.unload()
                    self.lib = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_camera(self):
        if self.lib is None:
            raise RuntimeError("SDK not started")

        print("[SDK-WRAPPER] connect_camera()")

        detect_attempts = 4
        open_attempts_per_detect = 3
        last_error = None

        with self._sdk_lock:
            for detect_index in range(1, detect_attempts + 1):
                if platform.system() == "Darwin":
                    wait_before_detect = 3.0 if detect_index == 1 else 1.5
                    print(
                        f"[SDK-WRAPPER] macOS detected -> waiting {wait_before_detect:.1f}s "
                        f"before detect/open (attempt {detect_index}/{detect_attempts})"
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
                    last_error = RuntimeError("No FUJIFILM camera detected by the SDK")
                    time.sleep(1.0)
                    continue

                for open_index in range(1, open_attempts_per_detect + 1):
                    try:
                        delay = 1.0 if open_index == 1 else 2.0
                        print(
                            f"[SDK-WRAPPER] waiting {delay:.1f}s before XSDK_OpenEx "
                            f"(open attempt {open_index}/{open_attempts_per_detect})"
                        )
                        time.sleep(delay)

                        self.camera_handle, info = self.lib.open_first_camera()

                        print("[SDK-WRAPPER] set CAMERA priority after open")
                        self.lib.set_priority_mode(
                            self.camera_handle, XSDK_PRIORITY_CAMERA
                        )

                        print("[SDK-WRAPPER] set media record RAWJPEG")
                        self.lib.set_media_record(
                            self.camera_handle, XSDK_MEDIAREC_RAWJPEG
                        )

                        print("[SDK-WRAPPER] settling after open")
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
                        print(f"[SDK-WRAPPER] camera connected -> {descriptor}")
                        return descriptor

                    except Exception as exc:
                        last_error = exc
                        print(
                            f"[SDK-WRAPPER] open attempt {open_index}/{open_attempts_per_detect} "
                            f"failed after detect success: {exc}"
                        )

                        try:
                            if self.camera_handle is not None and self.lib is not None:
                                print("[SDK-WRAPPER] cleaning up partial open session")
                                self.lib.close_camera(self.camera_handle)
                        except Exception as cleanup_exc:
                            print(
                                f"[SDK-WRAPPER] cleanup partial open ignored: {cleanup_exc}"
                            )
                        finally:
                            self.camera_handle = None
                            with self._state_lock:
                                self._connected = False
                                self._live_view_enabled = False
                                self._capture_in_progress = False
                                self._camera_opened_monotonic = None

                        time.sleep(1.5)

                print(
                    "[SDK-WRAPPER] detect succeeded but all open attempts failed; retrying full detect cycle"
                )
                time.sleep(1.5)

        if last_error is not None:
            raise RuntimeError(f"Cannot open camera via SDK: {last_error}")

        raise RuntimeError("Cannot open camera via SDK")

    def disconnect_camera(self):
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
                    print(f"[SDK-WRAPPER] stop_live_view ignored: {exc}")

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

    def start_live_view_once(self, lib, handle):
        lib.start_live_view(
            handle,
            quality=SDK_LIVEVIEW_QUALITY_FINE,
            size=SDK_LIVEVIEW_SIZE_L,
            mode=SDK_LIVEVIEW_MODE1,
        )

    def begin_live_view(self):
        lib, handle = self.ensure_session()

        with self._state_lock:
            if self._live_view_enabled:
                print("[SDK-WRAPPER] begin_live_view skipped: already enabled")
                return
            if self._capture_in_progress:
                raise RuntimeError("Cannot start live view during a capture")
            opened_at = self._camera_opened_monotonic

        with self._sdk_lock:
            if opened_at is not None:
                elapsed = time.monotonic() - opened_at
                if elapsed < 1.5:
                    wait_more = 1.5 - elapsed
                    print(
                        f"[SDK-WRAPPER] extra settle before live view: {wait_more:.2f}s"
                    )
                    time.sleep(wait_more)

            last_error = None

            for attempt in range(1, 4):
                try:
                    print(f"[SDK-WRAPPER] begin_live_view() attempt {attempt}/3")
                    self.start_live_view_once(lib, handle)
                    with self._state_lock:
                        self._live_view_enabled = True
                    print("[SDK-WRAPPER] live view enabled")
                    return

                except Exception as exc:
                    last_error = exc
                    print(
                        f"[SDK-WRAPPER] begin_live_view attempt {attempt} failed: {exc}"
                    )

                    try:
                        print("[SDK-WRAPPER] soft reset after live view failure")
                        try:
                            lib.stop_live_view(handle)
                        except Exception as stop_exc:
                            print(
                                f"[SDK-WRAPPER] soft reset stop_live_view ignored: {stop_exc}"
                            )

                        try:
                            lib.set_priority_mode(handle, XSDK_PRIORITY_CAMERA)
                        except Exception as prio_exc:
                            print(
                                f"[SDK-WRAPPER] soft reset set CAMERA ignored: {prio_exc}"
                            )

                        time.sleep(0.8)
                    except Exception as reset_exc:
                        print(f"[SDK-WRAPPER] soft reset failed: {reset_exc}")

            raise RuntimeError(f"Cannot start live view: {last_error}")

    def end_live_view(self):
        lib, handle = self.ensure_session()

        with self._state_lock:
            if not self._live_view_enabled:
                print("[SDK-WRAPPER] end_live_view skipped: already disabled")
                return

        with self._sdk_lock:
            try:
                lib.drain_read_buffer(handle)
            except Exception as exc:
                print(f"[SDK-WRAPPER] end_live_view drain ignored: {exc}")
            print("[SDK-WRAPPER] end_live_view()")
            lib.stop_live_view(handle)

        with self._state_lock:
            self._live_view_enabled = False

    def get_live_view_frame(self):
        lib, handle = self.ensure_session()

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

        image = QImage()
        if image.loadFromData(raw):
            self._last_frame = image
            return image

        raise RuntimeError("Cannot decode live view frame")

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _capture_worker(self, output_dir):
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

    def _do_capture(self, output_dir):
        lib, handle = self.ensure_session()

        print(f"[SDK-WRAPPER] capture start output_dir={output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

        with self._state_lock:
            was_live = self._live_view_enabled

        with self._sdk_lock:
            if was_live:
                try:
                    print("[SDK-WRAPPER] stopping live view before capture")
                    lib.stop_live_view(handle)
                except Exception as exc:
                    print(f"[SDK-WRAPPER] stop live before capture warning: {exc}")
                with self._state_lock:
                    self._live_view_enabled = False
                time.sleep(0.5)

            print("[SDK-WRAPPER] switching to PC PRIORITY for capture")
            lib.set_priority_mode(handle, XSDK_PRIORITY_PC)
            time.sleep(0.5)

            print("[SDK-WRAPPER] releasing in PC PRIORITY")
            lib.release_pc(handle)
            time.sleep(0.5)

            deadline = time.monotonic() + 10.0
            captured_path = None

            while time.monotonic() < deadline:
                info = lib.read_image_info(handle)
                fmt = int(info.format) & 0xFF
                size = int(info.data_size)

                if fmt == XSDK_IMAGEFORMAT_NONE or size <= 0:
                    time.sleep(0.05)
                    continue

                raw = lib.read_image(handle, size)
                print(f"[SDK-WRAPPER] capture buffer item fmt={fmt} size={len(raw)}")

                if fmt in {
                    XSDK_IMAGEFORMAT_RAW,
                    XSDK_IMAGEFORMAT_JPEG,
                    XSDK_IMAGEFORMAT_HEIF,
                }:
                    suffix = image_suffix_for_format(fmt)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
                    captured_path = output_dir / f"capture_{timestamp}{suffix}"
                    captured_path.write_bytes(raw)
                    print(f"[SDK-WRAPPER] capture saved -> {captured_path}")
                    break

            if captured_path is None:
                raise RuntimeError("Timeout waiting for captured image in SDK buffer")

            if was_live and not self._stopping:
                try:
                    print("[SDK-WRAPPER] restarting live view after capture")
                    self.start_live_view_once(lib, handle)
                    with self._state_lock:
                        self._live_view_enabled = True
                except Exception as exc:
                    print(
                        f"[SDK-WRAPPER] restart live view after capture warning: {exc}"
                    )

            return captured_path

    def capture_photo(self, output_dir):
        """
        Execute a capture synchronously from the caller thread.

        The backend now owns the global command queue, so capture must remain
        inside that serialized execution path instead of starting an extra SDK
        thread here.
        """
        with self._state_lock:
            if self._capture_in_progress:
                raise RuntimeError("A capture is already in progress")
            self._capture_in_progress = True
            self._last_captured_path = None
            self._last_capture_error = None

        try:
            path = self._do_capture(output_dir)
            with self._state_lock:
                self._last_captured_path = path
                self._last_capture_error = None
            return path
        except Exception as exc:
            print(f"[SDK-WRAPPER] capture_photo error: {exc}")
            with self._state_lock:
                self._last_captured_path = None
                self._last_capture_error = str(exc)
            raise
        finally:
            with self._state_lock:
                self._capture_in_progress = False

    def enqueue_capture(self, output_dir):
        with self._state_lock:
            if self._capture_in_progress:
                raise RuntimeError("A capture is already in progress")

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

    def pop_capture_result(self):
        with self._state_lock:
            path = self._last_captured_path
            err = self._last_capture_error
            self._last_captured_path = None
            self._last_capture_error = None
            return path, err

    # ------------------------------------------------------------------
    # PASM
    # ------------------------------------------------------------------
    def get_ae_mode_options(self):
        lib, handle = self.ensure_session()

        print("[SDK-WRAPPER] get_ae_mode_options()")

        supported = lib.cap_ae_mode(handle)
        result = [(self._format_ae_mode_label(v), v) for v in supported]

        print(f"[SDK-WRAPPER] get_ae_mode_options -> {result}")
        return result

    def get_ae_mode(self):
        lib, handle = self.ensure_session()

        print("[SDK-WRAPPER] get_ae_mode()")
        value = lib.get_ae_mode(handle)
        print(
            f"[SDK-WRAPPER] get_ae_mode -> value={value} "
            f"label={self._format_ae_mode_label(value)}"
        )
        return value

    def set_ae_mode(self, ae_mode):
        lib, handle = self.ensure_session()

        print(
            f"[SDK-WRAPPER] set_ae_mode requested value={ae_mode} "
            f"label={self._format_ae_mode_label(ae_mode)}"
        )

        with self._sdk_lock:
            was_live = self._live_view_enabled
            print(f"[SDK-WRAPPER] set_ae_mode live_view_before={was_live}")

            if was_live:
                try:
                    print("[SDK-WRAPPER] stopping live view before AE mode change")
                    lib.stop_live_view(handle)
                    self._live_view_enabled = False
                    time.sleep(0.3)
                except Exception as exc:
                    print(
                        f"[SDK-WRAPPER] stop_live_view before set_ae_mode ignored: {exc}"
                    )

            print("[SDK-WRAPPER] forcing PC priority before AE mode change")
            lib.set_priority_mode(handle, XSDK_PRIORITY_PC)
            time.sleep(0.2)

            supported = lib.cap_ae_mode(handle)
            print(f"[SDK-WRAPPER] set_ae_mode supported={supported}")

            if ae_mode not in supported:
                raise RuntimeError(
                    f"AE mode {ae_mode} ({self._format_ae_mode_label(ae_mode)}) "
                    f"not supported in current camera state: {supported}"
                )

            current = lib.get_ae_mode(handle)
            print(
                f"[SDK-WRAPPER] set_ae_mode current={current} "
                f"({self._format_ae_mode_label(current)})"
            )

            if current != ae_mode:
                lib.set_ae_mode(handle, ae_mode)
                time.sleep(0.3)

            current_after = lib.get_ae_mode(handle)
            print(
                f"[SDK-WRAPPER] set_ae_mode after={current_after} "
                f"({self._format_ae_mode_label(current_after)})"
            )

            if was_live:
                time.sleep(0.3)
                print("[SDK-WRAPPER] restarting live view after AE mode change")
                lib.start_live_view(handle)
                self._live_view_enabled = True
                print("[SDK-WRAPPER] live view restored after AE mode change")

    # ------------------------------------------------------------------
    # Exposure
    # ------------------------------------------------------------------

    def get_current_zoom_pos(self):
        lib, handle = self.ensure_session()

        print("[SDK-WRAPPER] _get_current_zoom_pos()")

        try:
            zoom_pos = lib.get_lens_zoom_pos(handle)
            print(f"[SDK-WRAPPER] current zoom pos from GetLensZoomPos={zoom_pos}")
            return int(zoom_pos)
        except Exception as exc:
            print(f"[SDK-WRAPPER] get_lens_zoom_pos failed: {exc}")

        try:
            zoom_positions = lib.cap_lens_zoom_pos(handle)
            if zoom_positions:
                zoom_pos = int(zoom_positions[0])
                print(f"[SDK-WRAPPER] fallback zoom pos from CapLensZoomPos={zoom_pos}")
                return zoom_pos
        except Exception as exc:
            print(f"[SDK-WRAPPER] cap_lens_zoom_pos failed: {exc}")

        print("[SDK-WRAPPER] fallback zoom pos=0")
        return 0

    def _format_ae_mode_label(self, ae_mode):
        if ae_mode is None:
            return "UNKNOWN"
        return AE_MODE_LABELS.get(ae_mode, f"UNKNOWN({ae_mode})")

    def _choose_ae_mode_for_request(self, *, shutter=None, aperture=None):
        """
        Returns the preferred AE mode for the requested exposure change.

        Minimal policy:
        - shutter only   -> S
        - aperture only  -> A
        - shutter+aperture -> M
        - iso only       -> no AE mode change
        """
        if shutter is not None and aperture is not None:
            return XSDK_AE_MANUAL
        if shutter is not None:
            return XSDK_AE_SHUTTER_PRIORITY
        if aperture is not None:
            return XSDK_AE_APERTURE_PRIORITY
        return None

    def get_exposure_options(self):
        """
        Returns supported exposure values for the camera's current mode.

        Each entry is a list of (display_label, raw_sdk_value) tuples.
        Lists are empty for parameters auto-controlled by the camera in the
        current AE mode (e.g. shutter speed in aperture-priority mode).
        """
        lib, handle = self.ensure_session()

        print("[SDK-WRAPPER] get_exposure_options()")

        iso_values = lib.cap_sensitivity(handle)
        shutter_values, shutter_bulb_supported = lib.cap_shutter_speed(handle)
        zoom_pos = self.get_current_zoom_pos()
        aperture_values = lib.cap_aperture(handle, zoom_pos)

        result = {
            "ae_mode": self.get_ae_mode_options(),
            "iso": [(format_iso(v), v) for v in iso_values],
            "shutter": [(format_shutter(v), v) for v in shutter_values],
            "aperture": [(format_aperture(v), v) for v in aperture_values],
        }

        print(
            f"[SDK-WRAPPER] get_exposure_options zoom_pos={zoom_pos} "
            f"bulb_supported={shutter_bulb_supported} "
            f"iso={len(result['iso'])} shutter={len(result['shutter'])} "
            f"aperture={len(result['aperture'])}"
        )
        return result

    def get_exposure_state(self):
        """
        Returns the current exposure settings as raw SDK integer values.

        Keys: 'iso', 'shutter', 'aperture', 'shutter_bulb'.
        Values are raw SDK integers (matching the values in get_exposure_options tuples),
        or None when the setting is unavailable.
        """
        lib, handle = self.ensure_session()

        print("[SDK-WRAPPER] get_exposure_state()")

        iso_value = lib.get_sensitivity(handle)
        shutter_value, shutter_bulb = lib.get_shutter_speed(handle)
        aperture_value = lib.get_aperture(handle)
        ae_mode_value = lib.get_ae_mode(handle)

        state = {
            "ae_mode": ae_mode_value,
            "iso": iso_value,
            "shutter": shutter_value,
            "aperture": aperture_value,
            "shutter_bulb": shutter_bulb,
        }

        print(f"[SDK-WRAPPER] get_exposure_state -> {state}")
        return state

    def set_exposure(self, *, iso=None, shutter=None, aperture=None, ae_mode=None):
        lib, handle = self.ensure_session()

        print(
            f"[SDK-WRAPPER] set_exposure requested "
            f"iso={iso} shutter={shutter} aperture={aperture} ae_mode={ae_mode}"
        )

        with self._sdk_lock:
            # 1) stop live view completely, pas seulement les timers Qt
            was_live = self._live_view_enabled
            print(f"[SDK-WRAPPER] set_exposure live_view_before={was_live}")

            if was_live:
                try:
                    print("[SDK-WRAPPER] stopping live view before exposure change")
                    lib.stop_live_view(handle)
                    self._live_view_enabled = False
                    time.sleep(0.3)
                except Exception as exc:
                    print(
                        f"[SDK-WRAPPER] stop_live_view before set_exposure ignored: {exc}"
                    )

            # 2) pour X-T4: re-force PC priority juste avant les setters
            print("[SDK-WRAPPER] forcing PC priority before exposure change")
            lib.set_priority_mode(handle, XSDK_PRIORITY_PC)
            time.sleep(0.2)

            try:
                current_ae_mode = lib.get_ae_mode(handle)
                print(
                    f"[SDK-WRAPPER] current AE mode before change="
                    f"{current_ae_mode} ({self._format_ae_mode_label(current_ae_mode)})"
                )
            except Exception as exc:
                current_ae_mode = None
                print(f"[SDK-WRAPPER] get_ae_mode before change failed: {exc}")

            try:
                supported_ae_modes = lib.cap_ae_mode(handle)
                print(
                    f"[SDK-WRAPPER] supported AE modes={supported_ae_modes} "
                    f"labels={[self._format_ae_mode_label(v) for v in supported_ae_modes]}"
                )
            except Exception as exc:
                supported_ae_modes = []
                print(f"[SDK-WRAPPER] cap_ae_mode failed: {exc}")

            # Use the explicitly requested AE mode if provided; otherwise infer
            # from which parameters are set (e.g. aperture-only → A priority).
            if ae_mode is not None:
                requested_ae_mode = ae_mode
            else:
                requested_ae_mode = self._choose_ae_mode_for_request(
                    shutter=shutter,
                    aperture=aperture,
                )
            print(
                f"[SDK-WRAPPER] requested AE mode for exposure change="
                f"{requested_ae_mode} "
                f"({self._format_ae_mode_label(requested_ae_mode) if requested_ae_mode is not None else 'UNCHANGED'})"
            )

            # Drop params that are auto-controlled in the target AE mode so we
            # never call SetAperture/SetShutter when the mode doesn't allow it.
            effective_mode = (
                requested_ae_mode if requested_ae_mode is not None else current_ae_mode
            )
            if effective_mode == XSDK_AE_SHUTTER_PRIORITY:
                if aperture is not None:
                    print(
                        f"[SDK-WRAPPER] dropping aperture={aperture} (auto in S mode)"
                    )
                    aperture = None
            elif effective_mode == XSDK_AE_APERTURE_PRIORITY:
                if shutter is not None:
                    print(f"[SDK-WRAPPER] dropping shutter={shutter} (auto in A mode)")
                    shutter = None
            elif effective_mode not in (XSDK_AE_MANUAL, None):
                # P or any other auto mode: both shutter and aperture are auto
                if shutter is not None:
                    print(
                        f"[SDK-WRAPPER] dropping shutter={shutter} (auto in P/auto mode)"
                    )
                    shutter = None
                if aperture is not None:
                    print(
                        f"[SDK-WRAPPER] dropping aperture={aperture} (auto in P/auto mode)"
                    )
                    aperture = None

            if (
                requested_ae_mode is not None
                and requested_ae_mode in supported_ae_modes
                and requested_ae_mode != current_ae_mode
            ):
                print(
                    f"[SDK-WRAPPER] changing AE mode "
                    f"{current_ae_mode} ({self._format_ae_mode_label(current_ae_mode)}) "
                    f"-> {requested_ae_mode} ({self._format_ae_mode_label(requested_ae_mode)})"
                )
                lib.set_ae_mode(handle, requested_ae_mode)
                time.sleep(0.3)

                try:
                    ae_mode_after = lib.get_ae_mode(handle)
                    print(
                        f"[SDK-WRAPPER] AE mode after change="
                        f"{ae_mode_after} ({self._format_ae_mode_label(ae_mode_after)})"
                    )
                except Exception as exc:
                    print(f"[SDK-WRAPPER] get_ae_mode after change failed: {exc}")
            elif (
                requested_ae_mode is not None
                and requested_ae_mode not in supported_ae_modes
            ):
                print(
                    f"[SDK-WRAPPER] requested AE mode {requested_ae_mode} "
                    f"({self._format_ae_mode_label(requested_ae_mode)}) "
                    "not supported by current camera state"
                )
            else:
                print("[SDK-WRAPPER] AE mode change not needed")

            try:
                exposure_state_before = self.get_exposure_state()
                print(
                    f"[SDK-WRAPPER] exposure_state_before_set={exposure_state_before}"
                )
            except Exception as exc:
                print(f"[SDK-WRAPPER] get_exposure_state before set failed: {exc}")

            # 3) applique seulement les réglages réellement supportés
            supported_iso = set(lib.cap_sensitivity(handle))
            supported_shutter, shutter_bulb_supported = lib.cap_shutter_speed(handle)
            supported_shutter = set(supported_shutter)
            zoom_pos = lib.get_lens_zoom_pos(handle)
            supported_aperture = set(lib.cap_aperture(handle, zoom_pos))

            print(
                f"[SDK-WRAPPER] supported exposure values after AE sync: "
                f"iso_count={len(supported_iso)} "
                f"shutter_count={len(supported_shutter)} "
                f"shutter_bulb_supported={shutter_bulb_supported} "
                f"aperture_count={len(supported_aperture)} "
                f"zoom_pos={zoom_pos}"
            )
            print(f"[SDK-WRAPPER] supported ISO values={sorted(supported_iso)}")
            print(f"[SDK-WRAPPER] supported shutter values={sorted(supported_shutter)}")
            print(
                f"[SDK-WRAPPER] supported aperture values={sorted(supported_aperture)}"
            )

            if iso is not None:
                if iso in supported_iso:
                    print(f"[SDK-WRAPPER] applying ISO={iso}")
                    lib.set_sensitivity(handle, iso)
                    time.sleep(0.15)
                else:
                    print(
                        f"[SDK-WRAPPER] requested ISO={iso} not supported in current state"
                    )

            if shutter is not None:
                if shutter in supported_shutter:
                    print(f"[SDK-WRAPPER] applying shutter={shutter}")
                    lib.set_shutter_speed(handle, shutter)
                    time.sleep(0.15)
                else:
                    print(
                        f"[SDK-WRAPPER] requested shutter={shutter} "
                        f"not supported in current state; supported={sorted(supported_shutter)}"
                    )

            if aperture is not None:
                if aperture in supported_aperture:
                    print(f"[SDK-WRAPPER] applying aperture={aperture}")
                    lib.set_aperture(handle, aperture)
                    time.sleep(0.15)
                else:
                    print(
                        f"[SDK-WRAPPER] requested aperture={aperture} "
                        f"not supported in current state; supported={sorted(supported_aperture)}"
                    )

            try:
                exposure_state_after = self.get_exposure_state()
                print(f"[SDK-WRAPPER] exposure_state_after_set={exposure_state_after}")
            except Exception as exc:
                print(f"[SDK-WRAPPER] get_exposure_state after set failed: {exc}")

            # 4) redémarre le live view après
            if was_live:
                time.sleep(0.3)
                print("[SDK-WRAPPER] restarting live view after exposure change")
                lib.start_live_view(handle)
                self._live_view_enabled = True
                print("[SDK-WRAPPER] live view restored after exposure change")

    # ------------------------------------------------------------------
    # Precheck
    # ------------------------------------------------------------------

    def run_precheck_safe(self):
        lib, handle = self.ensure_session()
        print("[SDK-WRAPPER] run_precheck_safe()")
        with self._sdk_lock:
            return lib.run_precheck(handle)

    def run_precheck(self):
        lib, handle = self.ensure_session()
        print("[SDK-WRAPPER] run_precheck()")
        with self._sdk_lock:
            return lib.run_precheck(handle)

    def format_precheck_summary(self, report):
        lines = [
            f"Connected: {'yes' if report.connected else 'no'}",
            f"Mode: {report.mode_value}",
            f"Drive mode: {report.drive_mode_value}",
            f"Priority mode: {report.priority_mode_value}",
            f"Release status: {report.release_status_value}",
            f"CapRelease (PC): {report.cap_release}",
            f"CapReleaseEx (Camera): {report.cap_release_ex}",
        ]

        if report.issues:
            lines.append("Issues:")
            for issue in report.issues:
                lines.append(f"- [{issue.level}] {issue.message}")
        else:
            lines.append("No issues detected.")

        return "\n".join(lines)
