from __future__ import annotations

import ctypes
import os
import platform
import threading
import time
from ctypes import (
    CDLL,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_long,
    c_longlong,
    c_ubyte,
    c_ulong,
    c_void_p,
    create_string_buffer,
)
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal subset of the Fujifilm SDK needed by the photobooth.
# This wrapper is intentionally conservative and verbose for debugging.
# ---------------------------------------------------------------------------

XSDK_COMPLETE = 0
XSDK_ERROR = -1

XSDK_DSC_IF_USB = 0x00000001

XSDK_PRIORITY_CAMERA = 0x0001
XSDK_PRIORITY_PC = 0x0002

# ReleaseEx modes
XSDK_RELEASE_EX_S1_ON = 0x00010000
XSDK_RELEASE_EX_S2_ON = 0x00020000
XSDK_RELEASE_EX_S1_OFF = 0x00000001
XSDK_RELEASE_EX_S2_OFF = 0x00000002
XSDK_RELEASE_EX_GRAB = 0x00080000

XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF = (
    XSDK_RELEASE_EX_S1_ON
    | XSDK_RELEASE_EX_S2_ON
    | XSDK_RELEASE_EX_S2_OFF
    | XSDK_RELEASE_EX_S1_OFF
)

# Force mode
XSDK_FORCESHOOTSTANDBY_SHOOT = 0x00000001

# Release modes (PC priority)
XSDK_RELEASE_SHOOT_S1OFF = 0x00000001
XSDK_RELEASE_N_S1OFF = 0x00000002
XSDK_RELEASE_S1ON = 0x00010000
XSDK_RELEASE_S2_S1OFF = 0x00020001

# Release status bits
XSDK_RELEASE_STATUS_S1 = 0x00000001
XSDK_RELEASE_STATUS_BULB = 0x00000002
XSDK_RELEASE_STATUS_AF = 0x00000004
XSDK_RELEASE_STATUS_AEL = 0x00000008
XSDK_RELEASE_STATUS_AFL = 0x00000010
XSDK_RELEASE_STATUS_WBL = 0x00000020
XSDK_RELEASE_STATUS_SHOOTING = 0x00000040

# Low-byte image format values returned by XSDK_ReadImageInfo
XSDK_IMAGEFORMAT_RAW = 1
XSDK_IMAGEFORMAT_LIVE = 4
XSDK_IMAGEFORMAT_NONE = 5
XSDK_IMAGEFORMAT_JPEG = 7
XSDK_IMAGEFORMAT_HEIF = 18

# Media record
XSDK_MEDIAREC_RAWJPEG = 0x0001
XSDK_MEDIAREC_RAW = 0x0002
XSDK_MEDIAREC_JPEG = 0x0003
XSDK_MEDIAREC_OFF = 0x0004

# Live view helper values used by Fujifilm samples / SetProp path
SDK_LIVEVIEW_QUALITY_FINE = 0x0001
SDK_LIVEVIEW_QUALITY_BASIC = 0x0003

SDK_LIVEVIEW_SIZE_L = 0x0001
SDK_LIVEVIEW_SIZE_M = 0x0002
SDK_LIVEVIEW_SIZE_S = 0x0003

SDK_LIVEVIEW_MODE1 = 0x0001
SDK_LIVEVIEW_MODE2 = 0x0002

# Optional APICode values used via XSDK_SetProp in the Fujifilm sample path
API_CODE_SET_LIVE_VIEW_IMAGE_QUALITY = 0x3323
API_CODE_SET_LIVE_VIEW_IMAGE_SIZE = 0x3325
API_CODE_SET_LIVE_VIEW_MODE = 0x332F
API_CODE_START_LIVE_VIEW = 0x3301
API_CODE_STOP_LIVE_VIEW = 0x3302


class DeviceInformation(Structure):
    _fields_ = [
        ("strVendor", c_char * 256),
        ("strManufacturer", c_char * 256),
        ("strProduct", c_char * 256),
        ("strFirmware", c_char * 256),
        ("strDeviceType", c_char * 256),
        ("strSerialNo", c_char * 256),
        ("strFramework", c_char * 256),
        ("bDeviceId", c_ubyte),
        ("strDeviceName", c_char * 32),
        ("strYNo", c_char * 32),
    ]


class ImageInformation(Structure):
    _fields_ = [
        ("internal_name", c_char * 32),
        ("format", c_long),
        ("data_size", c_long),
        ("image_pix_height", c_long),
        ("image_pix_width", c_long),
        ("image_bit_depth", c_long),
        ("preview_size", c_long),
        ("image_handle", c_void_p),
    ]


@dataclass(slots=True)
class RawCameraInfo:
    model: str
    serial: str
    device_name: str = "ENUM:0"


@dataclass(slots=True)
class PrecheckIssue:
    level: str
    code: str
    message: str


@dataclass(slots=True)
class CameraPrecheckReport:
    connected: bool
    mode_value: int | None = None
    drive_mode_value: int | None = None
    priority_mode_value: int | None = None
    release_status_value: int | None = None
    cap_release: list[int] = field(default_factory=list)
    cap_release_ex: list[int] = field(default_factory=list)
    issues: list[PrecheckIssue] = field(default_factory=list)

    @property
    def ok_for_live_view(self) -> bool:
        return self.connected

    @property
    def ok_for_remote_capture_pc(self) -> bool:
        return bool(self.cap_release)

    @property
    def ok_for_remote_capture_camera(self) -> bool:
        return bool(self.cap_release_ex)

    @property
    def blocking_issues(self) -> list[PrecheckIssue]:
        return [issue for issue in self.issues if issue.level == "error"]


class XsdkLoadError(RuntimeError):
    pass


class XsdkRuntimeError(RuntimeError):
    pass


class FujiSdkLibrary:
    def __init__(self, sdk_root: str | Path, xapi_path: str | Path | None = None) -> None:
        self.sdk_root = Path(os.path.expanduser(str(sdk_root))).resolve()
        self.xapi_path = (
            Path(os.path.expanduser(str(xapi_path))).resolve()
            if xapi_path
            else self._default_xapi_path()
        )

        self.module: CDLL | None = None
        self._preloaded: list[CDLL] = []
        self._loaded = False
        self._lock = threading.RLock()

        self.api_code = c_long(0)
        self.err_code = c_long(0)

        self.current_priority_mode: int | None = None
        self.live_view_running = False

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _default_xapi_path(self) -> Path:
        os_name = platform.system()
        if os_name == "Darwin":
            return self.sdk_root / "XAPI.bundle" / "Contents" / "MacOS" / "XAPI"
        if os_name == "Windows":
            return self.sdk_root / "XAPI.dll"
        return self.sdk_root / "XAPI.so"

    # ------------------------------------------------------------------
    # Loading / unloading
    # ------------------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                print("[XSDK] load() deja charge")
                return

            print(f"[XSDK] sdk_root={self.sdk_root}")
            print(f"[XSDK] xapi_path={self.xapi_path}")
            print(f"[XSDK] cwd avant chargement={Path.cwd()}")

            if not self.sdk_root.exists():
                raise XsdkLoadError(f"SDK root introuvable: {self.sdk_root}")
            if not self.xapi_path.exists():
                raise XsdkLoadError(f"XAPI introuvable: {self.xapi_path}")

            if platform.system() == "Darwin":
                self._preload_macos_dependencies()

            os.chdir(self.sdk_root)
            print(f"[XSDK] cwd apres chdir={Path.cwd()}")

            mode = getattr(ctypes, "RTLD_GLOBAL", 0)
            print(f"[XSDK] chargement bibliotheque: {self.xapi_path}")
            self.module = CDLL(str(self.xapi_path), mode=mode)
            self._bind_functions()

            self._loaded = True
            print("[XSDK] SDK charge")

    def unload(self) -> None:
        with self._lock:
            print("[XSDK] unload()")
            self.module = None
            self._preloaded.clear()
            self._loaded = False
            self.current_priority_mode = None
            self.live_view_running = False

    def _preload_macos_dependencies(self) -> None:
        mode = getattr(ctypes, "RTLD_GLOBAL", 0)

        dylibs = [
            self.sdk_root / "FTLPTP.dylib",
            self.sdk_root / "FTLPTPIP.dylib",
        ]

        for dylib in dylibs:
            print(f"[XSDK] preload dylib: {dylib}")
            if not dylib.exists():
                raise XsdkLoadError(f"Bibliotheque macOS manquante: {dylib}")
            self._preloaded.append(CDLL(str(dylib), mode=mode))

    # ------------------------------------------------------------------
    # Function binding
    # ------------------------------------------------------------------

    def _bind_functions(self) -> None:
        mod = self.module
        if mod is None:
            raise XsdkLoadError("SDK non charge")

        self.XSDK_Init = getattr(mod, "XSDK_Init")
        self.XSDK_Exit = getattr(mod, "XSDK_Exit")
        self.XSDK_Detect = getattr(mod, "XSDK_Detect")
        self.XSDK_OpenEx = getattr(mod, "XSDK_OpenEx")
        self.XSDK_Close = getattr(mod, "XSDK_Close")

        self.XSDK_GetErrorNumber = getattr(mod, "XSDK_GetErrorNumber")
        self.XSDK_GetErrorDetails = getattr(mod, "XSDK_GetErrorDetails")
        self.XSDK_GetDeviceInfo = getattr(mod, "XSDK_GetDeviceInfo")

        self.XSDK_SetPriorityMode = getattr(mod, "XSDK_SetPriorityMode")
        self.XSDK_GetPriorityMode = getattr(mod, "XSDK_GetPriorityMode")
        self.XSDK_SetMediaRecord = getattr(mod, "XSDK_SetMediaRecord")
        self.XSDK_GetDriveMode = getattr(mod, "XSDK_GetDriveMode")
        self.XSDK_GetMode = getattr(mod, "XSDK_GetMode")
        self.XSDK_SetForceMode = getattr(mod, "XSDK_SetForceMode")

        self.XSDK_CapRelease = getattr(mod, "XSDK_CapRelease")
        self.XSDK_Release = getattr(mod, "XSDK_Release")
        self.XSDK_CapReleaseEx = getattr(mod, "XSDK_CapReleaseEx")
        self.XSDK_ReleaseEx = getattr(mod, "XSDK_ReleaseEx")
        self.XSDK_GetReleaseStatus = getattr(mod, "XSDK_GetReleaseStatus")

        self.XSDK_ReadImageInfo = getattr(mod, "XSDK_ReadImageInfo")
        self.XSDK_ReadPreview = getattr(mod, "XSDK_ReadPreview")
        self.XSDK_ReadImage = getattr(mod, "XSDK_ReadImage")
        self.XSDK_DeleteImage = getattr(mod, "XSDK_DeleteImage")
        self.XSDK_GetBufferCapacity = getattr(mod, "XSDK_GetBufferCapacity")

        self.XSDK_SetProp = getattr(mod, "XSDK_SetProp")

        self.XSDK_Init.argtypes = [c_void_p]
        self.XSDK_Init.restype = c_long

        self.XSDK_Exit.argtypes = []
        self.XSDK_Exit.restype = c_long

        self.XSDK_Detect.argtypes = [c_long, c_void_p, c_void_p, ctypes.POINTER(c_long)]
        self.XSDK_Detect.restype = c_long

        self.XSDK_OpenEx.argtypes = [
            c_char_p,
            ctypes.POINTER(c_longlong),
            ctypes.POINTER(c_long),
            c_void_p,
        ]
        self.XSDK_OpenEx.restype = c_long

        self.XSDK_Close.argtypes = [c_longlong]
        self.XSDK_Close.restype = c_long

        self.XSDK_GetErrorNumber.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_GetErrorNumber.restype = c_long

        self.XSDK_GetErrorDetails.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetErrorDetails.restype = c_long

        self.XSDK_GetDeviceInfo.argtypes = [c_longlong, ctypes.POINTER(DeviceInformation)]
        self.XSDK_GetDeviceInfo.restype = c_long

        self.XSDK_SetPriorityMode.argtypes = [c_longlong, c_long]
        self.XSDK_SetPriorityMode.restype = c_long

        self.XSDK_GetPriorityMode.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetPriorityMode.restype = c_long

        self.XSDK_SetMediaRecord.argtypes = [c_longlong, c_long]
        self.XSDK_SetMediaRecord.restype = c_long

        self.XSDK_GetDriveMode.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetDriveMode.restype = c_long

        self.XSDK_GetMode.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetMode.restype = c_long

        self.XSDK_SetForceMode.argtypes = [c_longlong, c_long]
        self.XSDK_SetForceMode.restype = c_long

        self.XSDK_CapRelease.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapRelease.restype = c_long

        self.XSDK_Release.argtypes = [
            c_longlong,
            c_long,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_Release.restype = c_long

        self.XSDK_CapReleaseEx.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapReleaseEx.restype = c_long

        self.XSDK_ReleaseEx.argtypes = [
            c_longlong,
            c_long,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_ReleaseEx.restype = c_long

        self.XSDK_GetReleaseStatus.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetReleaseStatus.restype = c_long

        self.XSDK_ReadImageInfo.argtypes = [c_longlong, ctypes.POINTER(ImageInformation)]
        self.XSDK_ReadImageInfo.restype = c_long

        self.XSDK_ReadPreview.argtypes = [c_longlong, c_void_p, c_ulong]
        self.XSDK_ReadPreview.restype = c_long

        self.XSDK_ReadImage.argtypes = [c_longlong, c_void_p, c_ulong]
        self.XSDK_ReadImage.restype = c_long

        self.XSDK_DeleteImage.argtypes = [c_longlong]
        self.XSDK_DeleteImage.restype = c_long

        self.XSDK_GetBufferCapacity.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_GetBufferCapacity.restype = c_long

        # Variadic in practice in vendor sample usage.
        self.XSDK_SetProp.restype = c_long

        print("[XSDK] fonctions liees")

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _update_error(self, camera_handle: c_longlong | None = None) -> None:
        if self.module is None:
            return

        handle = camera_handle if camera_handle is not None else c_longlong(0)

        try:
            self.XSDK_GetErrorNumber(handle, byref(self.api_code), byref(self.err_code))
            print(
                f"[XSDK] last_error api_code=0x{self.api_code.value:x} "
                f"err_code=0x{self.err_code.value:x}"
            )
        except Exception as exc:
            print(f"[XSDK] impossible de recuperer last_error: {exc}")

    def _check(self, result: int, fn_name: str, camera_handle: c_longlong | None = None) -> int:
        print(f"[XSDK] {fn_name} -> rc={result}")
        if result != XSDK_COMPLETE:
            self._update_error(camera_handle)
            raise XsdkRuntimeError(
                f"{fn_name} a echoue rc={result} "
                f"api_code=0x{self.api_code.value:x} "
                f"err_code=0x{self.err_code.value:x}"
            )
        return result

    # ------------------------------------------------------------------
    # SDK lifecycle
    # ------------------------------------------------------------------

    def init_sdk(self) -> None:
        with self._lock:
            print("[XSDK] XSDK_Init(NULL)")
            result = self.XSDK_Init(None)
            self._check(result, "XSDK_Init")

    def exit_sdk(self) -> None:
        with self._lock:
            if self.module is None:
                print("[XSDK] XSDK_Exit skip (module None)")
                return
            print("[XSDK] XSDK_Exit")
            result = self.XSDK_Exit()
            self._check(result, "XSDK_Exit")

    # ------------------------------------------------------------------
    # Camera enumeration / open
    # ------------------------------------------------------------------

    def detect_usb_cameras(self) -> int:
        with self._lock:
            count = c_long(0)
            print("[XSDK] XSDK_Detect USB")
            result = self.XSDK_Detect(XSDK_DSC_IF_USB, None, None, byref(count))
            self._check(result, "XSDK_Detect")
            print(f"[XSDK] cameras detectees: {count.value}")
            return count.value

    def open_first_camera(self) -> tuple[c_longlong, RawCameraInfo]:
        with self._lock:
            camera_handle = c_longlong(0)
            camera_mode = c_long(0)
            device = b"ENUM:0"

            print(f"[XSDK] XSDK_OpenEx device={device!r}")
            print(f"[XSDK] avant open handle={camera_handle.value} mode={camera_mode.value}")

            result = self.XSDK_OpenEx(device, byref(camera_handle), byref(camera_mode), None)
            self._check(result, "XSDK_OpenEx")

            print(f"[XSDK] apres open handle={camera_handle.value} mode={camera_mode.value}")

            info = self.get_device_info(camera_handle)
            return camera_handle, info

    def close_camera(self, camera_handle: c_longlong) -> None:
        with self._lock:
            print(f"[XSDK] XSDK_Close handle={camera_handle.value}")
            result = self.XSDK_Close(camera_handle)
            self._check(result, "XSDK_Close", camera_handle)

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    def get_device_info(self, camera_handle: c_longlong) -> RawCameraInfo:
        with self._lock:
            info = DeviceInformation()

            print(f"[XSDK] XSDK_GetDeviceInfo handle={camera_handle.value}")
            result = self.XSDK_GetDeviceInfo(camera_handle, byref(info))
            self._check(result, "XSDK_GetDeviceInfo", camera_handle)

            model_str = info.strProduct.decode("utf-8", errors="ignore").strip("\x00 ").strip() or "FUJIFILM"
            serial_str = info.strSerialNo.decode("utf-8", errors="ignore").strip("\x00 ").strip() or "UNKNOWN"
            device_name = info.strDeviceName.decode("utf-8", errors="ignore").strip("\x00 ").strip() or "ENUM:0"

            print(f"[XSDK] device_info model={model_str} serial={serial_str} device_name={device_name}")
            return RawCameraInfo(model=model_str, serial=serial_str, device_name=device_name)

    # ------------------------------------------------------------------
    # Priority and camera settings
    # ------------------------------------------------------------------

    def set_priority_mode(self, camera_handle: c_longlong, mode: int = XSDK_PRIORITY_CAMERA) -> None:
        with self._lock:
            label = "PC" if mode == XSDK_PRIORITY_PC else "CAMERA"
            print(f"[XSDK] XSDK_SetPriorityMode handle={camera_handle.value} mode={mode} ({label})")
            result = self.XSDK_SetPriorityMode(camera_handle, mode)
            self._check(result, "XSDK_SetPriorityMode", camera_handle)
            self.current_priority_mode = mode

    def get_priority_mode(self, camera_handle: c_longlong) -> int:
        with self._lock:
            value = c_long(0)
            print(f"[XSDK] XSDK_GetPriorityMode handle={camera_handle.value}")
            result = self.XSDK_GetPriorityMode(camera_handle, byref(value))
            self._check(result, "XSDK_GetPriorityMode", camera_handle)
            print(f"[XSDK] XSDK_GetPriorityMode -> value={value.value}")
            return int(value.value)

    def ensure_pc_priority(self, camera_handle: c_longlong) -> None:
        if self.current_priority_mode != XSDK_PRIORITY_PC:
            print("[XSDK] ensure_pc_priority() -> switch vers PC PRIORITY")
            self.set_priority_mode(camera_handle, XSDK_PRIORITY_PC)
            time.sleep(0.5)

    def ensure_camera_priority(self, camera_handle: c_longlong) -> None:
        if self.current_priority_mode != XSDK_PRIORITY_CAMERA:
            print("[XSDK] ensure_camera_priority() -> switch vers CAMERA PRIORITY")
            self.set_priority_mode(camera_handle, XSDK_PRIORITY_CAMERA)
            time.sleep(0.5)

    def set_media_record(self, camera_handle: c_longlong, mode: int = XSDK_MEDIAREC_JPEG) -> None:
        with self._lock:
            print(f"[XSDK] XSDK_SetMediaRecord handle={camera_handle.value} mode={mode}")
            result = self.XSDK_SetMediaRecord(camera_handle, mode)
            self._check(result, "XSDK_SetMediaRecord", camera_handle)

    def get_drive_mode(self, camera_handle: c_longlong) -> int:
        with self._lock:
            value = c_long(0)
            print(f"[XSDK] XSDK_GetDriveMode handle={camera_handle.value}")
            result = self.XSDK_GetDriveMode(camera_handle, byref(value))
            self._check(result, "XSDK_GetDriveMode", camera_handle)
            print(f"[XSDK] XSDK_GetDriveMode -> value={value.value}")
            return int(value.value)

    # ------------------------------------------------------------------
    # Release / capture
    # ------------------------------------------------------------------

    def get_mode(self, camera_handle: c_longlong) -> int | None:
        with self._lock:
            value = c_long(0)
            print(f"[XSDK] XSDK_GetMode handle={camera_handle.value}")

            result = self.XSDK_GetMode(camera_handle, byref(value))
            if result == XSDK_COMPLETE:
                print(f"[XSDK] XSDK_GetMode -> value={value.value}")
                return int(value.value)

            self._update_error(camera_handle)

            if self.err_code.value == 0x1005:
                detail = self.get_error_details(camera_handle)
                detail_name = self.decode_error_details(detail)
                print(
                    "[XSDK] XSDK_GetMode busy "
                    f"api_code=0x{self.api_code.value:x} "
                    f"err_code=0x{self.err_code.value:x} "
                    f"detail={detail_name}"
                )
                return None

            self._check(result, "XSDK_GetMode", camera_handle)
            return None

    def set_force_shoot_mode(self, camera_handle: c_longlong) -> None:
        with self._lock:
            print(f"[XSDK] XSDK_SetForceMode handle={camera_handle.value} SHOOT")
            result = self.XSDK_SetForceMode(camera_handle, XSDK_FORCESHOOTSTANDBY_SHOOT)
            self._check(result, "XSDK_SetForceMode(SHOOT)", camera_handle)
            print("[XSDK] XSDK_SetForceMode(SHOOT) -> ok")

    def get_release_status(self, camera_handle: c_longlong) -> int:
        with self._lock:
            status = c_long(0)
            print(f"[XSDK] XSDK_GetReleaseStatus handle={camera_handle.value}")
            result = self.XSDK_GetReleaseStatus(camera_handle, byref(status))
            self._check(result, "XSDK_GetReleaseStatus", camera_handle)
            print(f"[XSDK] XSDK_GetReleaseStatus -> status={status.value}")
            return int(status.value)

    def _decode_release_status(self, status: int) -> list[str]:
        flags: list[str] = []
        if status & XSDK_RELEASE_STATUS_S1:
            flags.append("S1")
        if status & XSDK_RELEASE_STATUS_BULB:
            flags.append("BULB")
        if status & XSDK_RELEASE_STATUS_AF:
            flags.append("AF")
        if status & XSDK_RELEASE_STATUS_AEL:
            flags.append("AEL")
        if status & XSDK_RELEASE_STATUS_AFL:
            flags.append("AFL")
        if status & XSDK_RELEASE_STATUS_WBL:
            flags.append("WBL")
        if status & XSDK_RELEASE_STATUS_SHOOTING:
            flags.append("SHOOTING")
        return flags

    def cap_release(self, camera_handle: c_longlong) -> list[int]:
        with self._lock:
            self.ensure_pc_priority(camera_handle)

            num = c_long(0)

            print(f"[XSDK] XSDK_CapRelease handle={camera_handle.value} query count")
            result = self.XSDK_CapRelease(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapRelease(count)", camera_handle)

            print(f"[XSDK] XSDK_CapRelease supported_count={num.value}")

            if num.value <= 0:
                return []

            arr_type = c_long * num.value
            arr = arr_type()

            print(f"[XSDK] XSDK_CapRelease handle={camera_handle.value} query modes")
            result = self.XSDK_CapRelease(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapRelease(list)", camera_handle)

            supported = [int(arr[i]) for i in range(num.value)]
            print(f"[XSDK] XSDK_CapRelease supported_modes={supported}")
            return supported

    def _release_once(
        self,
        camera_handle: c_longlong,
        release_mode: int,
        label: str,
    ) -> tuple[int, int]:
        shot_option = c_long(0)
        af_status = c_long(0)

        print(f"[XSDK] XSDK_Release handle={camera_handle.value} mode={release_mode} ({label})")
        result = self.XSDK_Release(
            camera_handle,
            release_mode,
            byref(shot_option),
            byref(af_status),
        )
        self._check(result, f"XSDK_Release({label})", camera_handle)

        print(
            f"[XSDK] XSDK_Release {label} "
            f"shot_option={shot_option.value} af_status={af_status.value}"
        )
        return shot_option.value, af_status.value

    def release_pc(self, camera_handle: c_longlong) -> None:
        with self._lock:
            self.ensure_pc_priority(camera_handle)

            try:
                self.set_force_shoot_mode(camera_handle)
                time.sleep(0.8)
            except Exception as exc:
                print(f"[XSDK] set_force_shoot_mode failed: {exc}")

            try:
                status_before = self.get_release_status(camera_handle)
                print(
                    "[XSDK] release status before capture="
                    f"{status_before} flags={self._decode_release_status(status_before)}"
                )
            except Exception as exc:
                print(f"[XSDK] get_release_status(before) failed: {exc}")

            supported = self.cap_release(camera_handle)
            print(f"[XSDK] release_pc supported={supported}")

            if not supported:
                raise XsdkRuntimeError("Aucun mode de declenchement PC disponible")

            preferred_modes: list[tuple[int, str]] = []

            # Mode observe comme fonctionnel sur ton X-T4
            if 260 in supported:
                preferred_modes.append((260, "MODE_260"))

            # Fallbacks connus
            if XSDK_RELEASE_SHOOT_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_SHOOT_S1OFF, "SHOOT_S1OFF"))

            if XSDK_RELEASE_S1ON in supported:
                preferred_modes.append((XSDK_RELEASE_S1ON, "S1_ON"))

            if XSDK_RELEASE_S2_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_S2_S1OFF, "S2_S1OFF"))

            if XSDK_RELEASE_N_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_N_S1OFF, "N_S1OFF"))

            errors: list[str] = []

            for mode, label in preferred_modes:
                try:
                    self._release_once(camera_handle, mode, label)
                    time.sleep(0.8)
                    print(f"[XSDK] release_pc success with {label}")
                    return
                except Exception as exc:
                    print(f"[XSDK] release_pc mode {label} failed: {exc}")
                    errors.append(f"{label}: {exc}")

            raise XsdkRuntimeError(
                f"Aucun mode de declenchement PC n'a fonctionne. supported={supported}; errors={errors}"
            )

    def cap_release_ex(self, camera_handle: c_longlong) -> list[int]:
        with self._lock:
            self.ensure_camera_priority(camera_handle)

            num = c_long(0)

            print(f"[XSDK] XSDK_CapReleaseEx handle={camera_handle.value} query count")
            result = self.XSDK_CapReleaseEx(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapReleaseEx(count)", camera_handle)

            print(f"[XSDK] XSDK_CapReleaseEx supported_count={num.value}")

            if num.value <= 0:
                return []

            arr_type = c_long * num.value
            arr = arr_type()

            print(f"[XSDK] XSDK_CapReleaseEx handle={camera_handle.value} query modes")
            result = self.XSDK_CapReleaseEx(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapReleaseEx(list)", camera_handle)

            supported = [int(arr[i]) for i in range(num.value)]
            print(f"[XSDK] XSDK_CapReleaseEx supported_modes={supported}")
            return supported

    def release_camera_priority(self, camera_handle: c_longlong) -> bool:
        with self._lock:
            self.ensure_camera_priority(camera_handle)

            try:
                status_before = self.get_release_status(camera_handle)
                print(
                    "[XSDK] release_ex status before capture="
                    f"{status_before} flags={self._decode_release_status(status_before)}"
                )
            except Exception as exc:
                print(f"[XSDK] get_release_status(before ex) failed: {exc}")

            supported = self.cap_release_ex(camera_handle)
            print(f"[XSDK] release_camera_priority supported={supported}")

            if not supported:
                print("[XSDK] aucun mode ReleaseEx disponible dans l'etat courant")
                return False

            shot_option = c_long(0)
            af_status = c_long(0)

            if XSDK_RELEASE_EX_GRAB in supported:
                print("[XSDK] using XSDK_RELEASE_EX_GRAB")
                result = self.XSDK_ReleaseEx(
                    camera_handle,
                    XSDK_RELEASE_EX_GRAB,
                    byref(shot_option),
                    byref(af_status),
                )
                self._check(result, "XSDK_ReleaseEx(GRAB)", camera_handle)
                print(
                    f"[XSDK] XSDK_ReleaseEx(GRAB) "
                    f"shot_option={shot_option.value} af_status={af_status.value}"
                )
                time.sleep(0.8)
                return True

            if XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF in supported:
                print("[XSDK] using XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF")
                result = self.XSDK_ReleaseEx(
                    camera_handle,
                    XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF,
                    byref(shot_option),
                    byref(af_status),
                )
                self._check(
                    result,
                    "XSDK_ReleaseEx(S1_ON_S2_ON_S2_OFF_S1_OFF)",
                    camera_handle,
                )
                print(
                    f"[XSDK] XSDK_ReleaseEx(full-sequence) "
                    f"shot_option={shot_option.value} af_status={af_status.value}"
                )
                time.sleep(0.8)
                return True

            print("[XSDK] aucun mode ReleaseEx exploitable trouve")
            return False

    def release_ex(self, camera_handle: c_longlong, release_mode: int) -> bool:
        with self._lock:
            shot_option = c_long(0)
            af_status = c_long(0)

            print(
                f"[XSDK] XSDK_ReleaseEx handle={camera_handle.value} "
                f"mode=0x{release_mode:x}"
            )

            result = self.XSDK_ReleaseEx(
                camera_handle,
                c_long(release_mode),
                byref(shot_option),
                byref(af_status),
            )

            if result != XSDK_COMPLETE:
                self._update_error(camera_handle)
                print(
                    f"[XSDK] XSDK_ReleaseEx failed rc={result} "
                    f"api_code=0x{self.api_code.value:x} "
                    f"err_code=0x{self.err_code.value:x}"
                )
                return False

            print(
                f"[XSDK] XSDK_ReleaseEx ok "
                f"shot_option={shot_option.value} af_status={af_status.value}"
            )
            return True

    # ------------------------------------------------------------------
    # Image buffer access
    # ------------------------------------------------------------------

    def read_image_info(self, camera_handle: c_longlong) -> ImageInformation:
        with self._lock:
            info = ImageInformation()
            print(f"[XSDK] XSDK_ReadImageInfo handle={camera_handle.value}")
            result = self.XSDK_ReadImageInfo(camera_handle, byref(info))
            self._check(result, "XSDK_ReadImageInfo", camera_handle)
            print(
                "[XSDK] image_info "
                f"format={info.format} data_size={info.data_size} "
                f"preview_size={info.preview_size} "
                f"w={info.image_pix_width} h={info.image_pix_height}"
            )
            return info

    def read_preview(self, camera_handle: c_longlong, size: int) -> bytes:
        with self._lock:
            print(f"[XSDK] XSDK_ReadPreview handle={camera_handle.value} size={size}")

            if size <= 0:
                raise XsdkRuntimeError(f"XSDK_ReadPreview taille invalide: {size}")

            data = create_string_buffer(size)
            result = self.XSDK_ReadPreview(camera_handle, data, size)
            self._check(result, "XSDK_ReadPreview", camera_handle)

            payload = bytes(data.raw[:size])
            print(f"[XSDK] XSDK_ReadPreview bytes={len(payload)}")
            return payload

    def read_image(self, camera_handle: c_longlong, size: int) -> bytes:
        with self._lock:
            print(f"[XSDK] XSDK_ReadImage handle={camera_handle.value} size={size}")

            if size <= 0:
                raise XsdkRuntimeError(f"XSDK_ReadImage taille invalide: {size}")

            data = create_string_buffer(size)
            result = self.XSDK_ReadImage(camera_handle, data, size)
            self._check(result, "XSDK_ReadImage", camera_handle)

            payload = bytes(data.raw[:size])
            print(f"[XSDK] XSDK_ReadImage bytes={len(payload)}")
            return payload

    def get_buffer_capacity(self, camera_handle: c_longlong) -> tuple[int, int]:
        with self._lock:
            shoot_frame_num = c_long(0)
            total_frame_num = c_long(0)

            print(f"[XSDK] XSDK_GetBufferCapacity handle={camera_handle.value}")
            result = self.XSDK_GetBufferCapacity(
                camera_handle,
                byref(shoot_frame_num),
                byref(total_frame_num),
            )
            self._check(result, "XSDK_GetBufferCapacity", camera_handle)

            print(
                f"[XSDK] XSDK_GetBufferCapacity -> "
                f"shot={shoot_frame_num.value} total={total_frame_num.value}"
            )
            return int(shoot_frame_num.value), int(total_frame_num.value)

    def drain_read_buffer(self, camera_handle: c_longlong, max_items: int = 32) -> int:
        with self._lock:
            drained = 0
            print(f"[XSDK] drain_read_buffer handle={camera_handle.value}")

            for _ in range(max_items):
                info = ImageInformation()
                result = self.XSDK_ReadImageInfo(camera_handle, byref(info))
                self._check(result, "XSDK_ReadImageInfo", camera_handle)

                fmt = info.format & 0xFF
                if fmt == XSDK_IMAGEFORMAT_NONE or info.data_size <= 0:
                    print(f"[XSDK] drain_read_buffer done drained={drained}")
                    return drained

                print(
                    f"[XSDK] drain item fmt={fmt} size={info.data_size} "
                    f"preview_size={info.preview_size}"
                )
                _ = self.read_image(camera_handle, int(info.data_size))
                drained += 1

            print(f"[XSDK] drain_read_buffer reached max_items drained={drained}")
            return drained

    def delete_image(self, camera_handle: c_longlong) -> None:
        with self._lock:
            print(f"[XSDK] XSDK_DeleteImage handle={camera_handle.value}")
            result = self.XSDK_DeleteImage(camera_handle)
            self._check(result, "XSDK_DeleteImage", camera_handle)

    # ------------------------------------------------------------------
    # Generic SetProp
    # ------------------------------------------------------------------

    def set_prop(self, camera_handle: c_longlong, api_code: int, api_param: int = 0, *extra: int) -> None:
        with self._lock:
            printable = [api_param, *extra]
            print(
                f"[XSDK] XSDK_SetProp handle={camera_handle.value} "
                f"api_code=0x{api_code:x} values={printable}"
            )

            args = [camera_handle, c_long(api_code), c_long(api_param)]
            for value in extra:
                args.append(c_long(value))

            result = self.XSDK_SetProp(*args)
            self._check(result, f"XSDK_SetProp(0x{api_code:x})", camera_handle)

    # ------------------------------------------------------------------
    # Live view
    # ------------------------------------------------------------------

    def start_live_view(
        self,
        camera_handle: c_longlong,
        *,
        quality: int = SDK_LIVEVIEW_QUALITY_FINE,
        size: int = SDK_LIVEVIEW_SIZE_L,
        mode: int = SDK_LIVEVIEW_MODE1,
    ) -> None:
        with self._lock:
            print(f"[XSDK] start_live_view quality={quality} size={size} mode={mode}")

            self.ensure_pc_priority(camera_handle)

            try:
                self.drain_read_buffer(camera_handle)
            except Exception as exc:
                print(f"[XSDK] drain_read_buffer warning before StartLiveView: {exc}")

            props_ok = True

            try:
                self.set_prop(camera_handle, API_CODE_SET_LIVE_VIEW_IMAGE_QUALITY, 1, quality)
            except XsdkRuntimeError as exc:
                props_ok = False
                print(f"[XSDK] warning quality live view non appliquee: {exc}")

            try:
                self.set_prop(camera_handle, API_CODE_SET_LIVE_VIEW_IMAGE_SIZE, 1, size)
            except XsdkRuntimeError as exc:
                props_ok = False
                print(f"[XSDK] warning size live view non appliquee: {exc}")

            try:
                self.set_prop(camera_handle, API_CODE_SET_LIVE_VIEW_MODE, 1, mode)
            except XsdkRuntimeError as exc:
                props_ok = False
                print(f"[XSDK] warning mode live view non applique: {exc}")

            if not props_ok:
                print("[XSDK] certaines props live view ont echoue, tentative StartLiveView quand meme")

            self.set_prop(camera_handle, API_CODE_START_LIVE_VIEW, 0)
            self.live_view_running = True
            print("[XSDK] live view demarre")

    def stop_live_view(self, camera_handle: c_longlong) -> None:
        with self._lock:
            print("[XSDK] stop_live_view")
            try:
                self.set_prop(camera_handle, API_CODE_STOP_LIVE_VIEW, 0)
            except XsdkRuntimeError as exc:
                print(f"[XSDK] warning StopLiveView a echoue: {exc}")
            self.live_view_running = False

    # ------------------------------------------------------------------
    # End-to-end debug helper
    # ------------------------------------------------------------------

    def debug_open_sequence(self) -> tuple[c_longlong, RawCameraInfo]:
        print("[XSDK-DEBUG] debug_open_sequence()")
        self.load()
        self.init_sdk()

        if platform.system() == "Darwin":
            print("[XSDK-DEBUG] macOS detecte -> attente 3s")
            time.sleep(3)

        count = self.detect_usb_cameras()
        if count <= 0:
            raise XsdkRuntimeError("Aucune camera detectee")

        print("[XSDK-DEBUG] attente 1s avant open")
        time.sleep(1)

        handle, info = self.open_first_camera()
        print(f"[XSDK-DEBUG] session ouverte handle={handle.value} info={info}")
        return handle, info

    def get_error_details(self, camera_handle: c_longlong | None = None) -> int | None:
        with self._lock:
            if self.module is None:
                return None

            value = c_long(0)
            handle = camera_handle if camera_handle is not None else c_longlong(0)

            print(f"[XSDK] XSDK_GetErrorDetails handle={handle.value}")
            result = self.XSDK_GetErrorDetails(handle, byref(value))
            if result != XSDK_COMPLETE:
                print(f"[XSDK] XSDK_GetErrorDetails failed rc={result}")
                return None

            print(f"[XSDK] XSDK_GetErrorDetails -> value={value.value}")
            return int(value.value)

    def decode_error_details(self, detail: int | None) -> str:
        if detail is None:
            return "UNKNOWN"

        mapping = {
            0x00000001: "AEL",
            0x00000002: "AFL",
            0x00000003: "INSTANTAF",
            0x00000004: "AFON",
            0x00000005: "SHOOTING",
            0x00000006: "SHOOTINGCOUNTDOWN",
            0x00000007: "RECORDING",
            0x00000008: "LIVEVIEW",
            0x00000009: "UNTRANSFERRED_IMAGE",
        }
        return mapping.get(detail, f"UNKNOWN({detail})")

    def run_precheck(self, camera_handle: c_longlong) -> CameraPrecheckReport:
        with self._lock:
            report = CameraPrecheckReport(connected=True)

            def _safe_read(name: str, fn):
                try:
                    return fn()
                except Exception as exc:
                    print(f"[XSDK-PRECHECK] {name} failed: {exc}")
                    report.issues.append(
                        PrecheckIssue(
                            level="warning",
                            code=f"{name}_failed",
                            message=f"Lecture {name} impossible: {exc}",
                        )
                    )
                    return None

            report.priority_mode_value = _safe_read(
                "priority_mode",
                lambda: self.get_priority_mode(camera_handle),
            )
            report.drive_mode_value = _safe_read(
                "drive_mode",
                lambda: self.get_drive_mode(camera_handle),
            )
            report.mode_value = _safe_read(
                "mode",
                lambda: self.get_mode(camera_handle),
            )
            report.release_status_value = _safe_read(
                "release_status",
                lambda: self.get_release_status(camera_handle),
            )

            try:
                report.cap_release = self.cap_release(camera_handle)
            except Exception as exc:
                print(f"[XSDK-PRECHECK] cap_release failed: {exc}")
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="cap_release_failed",
                        message=f"Lecture des modes Release PC impossible: {exc}",
                    )
                )

            try:
                previous_priority = report.priority_mode_value
                try:
                    self.set_priority_mode(camera_handle, XSDK_PRIORITY_CAMERA)
                    time.sleep(0.3)
                    report.cap_release_ex = self.cap_release_ex(camera_handle)
                finally:
                    if previous_priority is not None:
                        try:
                            self.set_priority_mode(camera_handle, previous_priority)
                            time.sleep(0.2)
                        except Exception as exc:
                            print(f"[XSDK-PRECHECK] restore priority failed: {exc}")
            except Exception as exc:
                print(f"[XSDK-PRECHECK] cap_release_ex failed: {exc}")
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="cap_release_ex_failed",
                        message=f"Lecture des modes ReleaseEx impossible: {exc}",
                    )
                )

            if report.release_status_value is not None:
                flags = self._decode_release_status(report.release_status_value)
                print(
                    "[XSDK-PRECHECK] release_status="
                    f"{report.release_status_value} flags={flags}"
                )

            if not report.cap_release:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="no_pc_release_modes",
                        message="Aucun mode de declenchement PC disponible actuellement.",
                    )
                )

            if not report.cap_release_ex:
                report.issues.append(
                    PrecheckIssue(
                        level="error",
                        code="no_camera_release_ex_modes",
                        message=(
                            "Aucun mode de declenchement Camera Priority disponible. "
                            "Passe le boitier en mode photo, drive simple, pas video."
                        ),
                    )
                )

            if report.mode_value is None:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="unknown_mode",
                        message="Mode boitier non lisible via SDK.",
                    )
                )

            if report.drive_mode_value is None:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="unknown_drive_mode",
                        message="Drive mode non lisible via SDK.",
                    )
                )

            print("[XSDK-PRECHECK] report summary")
            print(f"[XSDK-PRECHECK] connected={report.connected}")
            print(f"[XSDK-PRECHECK] mode_value={report.mode_value}")
            print(f"[XSDK-PRECHECK] drive_mode_value={report.drive_mode_value}")
            print(f"[XSDK-PRECHECK] priority_mode_value={report.priority_mode_value}")
            print(f"[XSDK-PRECHECK] release_status_value={report.release_status_value}")
            print(f"[XSDK-PRECHECK] cap_release={report.cap_release}")
            print(f"[XSDK-PRECHECK] cap_release_ex={report.cap_release_ex}")
            for issue in report.issues:
                print(f"[XSDK-PRECHECK] issue {issue.level} {issue.code}: {issue.message}")

            return report


def image_suffix_for_format(image_format: int) -> str:
    fmt = image_format & 0xFF
    if fmt == XSDK_IMAGEFORMAT_RAW:
        return ".RAF"
    if fmt == XSDK_IMAGEFORMAT_HEIF:
        return ".HEIC"
    if fmt == XSDK_IMAGEFORMAT_JPEG:
        return ".JPG"
    return ".BIN"