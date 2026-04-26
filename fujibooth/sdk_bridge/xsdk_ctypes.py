import ctypes
import os
import platform
import threading
import time
import logging

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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Minimal subset of the Fujifilm SDK needed by the photobooth.
# This wrapper is intentionally conservative and verbose for debugging.
# ---------------------------------------------------------------------------

XSDK_COMPLETE = 0
XSDK_ERROR = -1

XSDK_DSC_IF_USB = 0x00000001

XSDK_PRIORITY_CAMERA = 0x0001
XSDK_PRIORITY_PC = 0x0002

# AE modes
XSDK_AE_MANUAL = 0x0001
XSDK_AE_APERTURE_PRIORITY = 0x0003
XSDK_AE_SHUTTER_PRIORITY = 0x0004
XSDK_AE_PROGRAM = 0x0006

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
    mode_value: int = field(default=None)
    drive_mode_value: int = field(default=None)
    priority_mode_value: int = field(default=None)
    release_status_value: int = field(default=None)
    cap_release: list = field(default_factory=list)
    cap_release_ex: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def ok_for_live_view(self):
        return self.connected

    @property
    def ok_for_remote_capture_pc(self):
        return bool(self.cap_release)

    @property
    def ok_for_remote_capture_camera(self):
        return bool(self.cap_release_ex)

    @property
    def blocking_issues(self):
        return [issue for issue in self.issues if issue.level == "error"]


class XsdkLoadError(RuntimeError):
    pass


class XsdkRuntimeError(RuntimeError):
    pass


class FujiSdkLibrary:
    def __init__(self, sdk_root, xapi_path=None):
        self.sdk_root = Path(os.path.expanduser(str(sdk_root))).resolve()
        self.xapi_path = (
            Path(os.path.expanduser(str(xapi_path))).resolve()
            if xapi_path
            else self._default_xapi_path()
        )

        self.module = None
        self._preloaded = []
        self._loaded = False
        self._lock = threading.RLock()

        self.api_code = c_long(0)
        self.err_code = c_long(0)

        self.current_priority_mode = None
        self.live_view_running = False

        self.XSDK_CapLensZoomPos = None

    # ------------------------------------------------------------------
    # Paths
    # ------------------------------------------------------------------

    def _default_xapi_path(self):
        os_name = platform.system()
        if os_name == "Darwin":
            return self.sdk_root / "XAPI.bundle" / "Contents" / "MacOS" / "XAPI"
        if os_name == "Windows":
            return self.sdk_root / "XAPI.dll"
        return self.sdk_root / "XAPI.so"

    # ------------------------------------------------------------------
    # Loading / unloading
    # ------------------------------------------------------------------

    def load(self):
        with self._lock:
            if self._loaded:
                logger.debug("load() already loaded")
                return

            logger.debug(f"sdk_root={self.sdk_root}")
            logger.debug(f"xapi_path={self.xapi_path}")
            logger.debug(f"cwd before loading={Path.cwd()}")

            if not self.sdk_root.exists():
                raise XsdkLoadError(f"SDK root not found: {self.sdk_root}")
            if not self.xapi_path.exists():
                raise XsdkLoadError(f"XAPI not found: {self.xapi_path}")

            if platform.system() == "Darwin":
                self._preload_macos_dependencies()

            os.chdir(self.sdk_root)
            logger.debug(f"cwd after chdir={Path.cwd()}")

            mode = getattr(ctypes, "RTLD_GLOBAL", 0)
            logger.debug(f"loading library: {self.xapi_path}")
            self.module = CDLL(str(self.xapi_path), mode=mode)
            self._bind_functions()

            self._loaded = True
            logger.debug("SDK loaded")

    def unload(self):
        with self._lock:
            logger.debug("unload()")
            self.module = None
            self._preloaded.clear()
            self._loaded = False
            self.current_priority_mode = None
            self.live_view_running = False
            self.XSDK_CapLensZoomPos = None

    def _preload_macos_dependencies(self):
        mode = getattr(ctypes, "RTLD_GLOBAL", 0)

        dylibs = [
            self.sdk_root / "FTLPTP.dylib",
            self.sdk_root / "FTLPTPIP.dylib",
        ]

        for dylib in dylibs:
            logger.debug(f"preloading dylib: {dylib}")
            if not dylib.exists():
                raise XsdkLoadError(f"Missing macOS library: {dylib}")
            self._preloaded.append(CDLL(str(dylib), mode=mode))

    # ------------------------------------------------------------------
    # Function binding
    # ------------------------------------------------------------------

    def _bind_functions(self):
        mod = self.module
        if mod is None:
            raise XsdkLoadError("SDK not loaded")

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
        self.XSDK_CapAEMode = getattr(mod, "XSDK_CapAEMode")
        self.XSDK_SetAEMode = getattr(mod, "XSDK_SetAEMode")
        self.XSDK_GetAEMode = getattr(mod, "XSDK_GetAEMode")
        self.XSDK_CapShutterSpeed = getattr(mod, "XSDK_CapShutterSpeed")
        self.XSDK_SetShutterSpeed = getattr(mod, "XSDK_SetShutterSpeed")
        self.XSDK_GetShutterSpeed = getattr(mod, "XSDK_GetShutterSpeed")
        self.XSDK_CapSensitivity = getattr(mod, "XSDK_CapSensitivity")
        self.XSDK_SetSensitivity = getattr(mod, "XSDK_SetSensitivity")
        self.XSDK_GetSensitivity = getattr(mod, "XSDK_GetSensitivity")
        self.XSDK_GetLensZoomPos = getattr(mod, "XSDK_GetLensZoomPos")
        self.XSDK_CapAperture = getattr(mod, "XSDK_CapAperture")
        self.XSDK_SetAperture = getattr(mod, "XSDK_SetAperture")
        self.XSDK_GetAperture = getattr(mod, "XSDK_GetAperture")

        # Optional symbol — not present in all SDK builds/models
        self.XSDK_CapLensZoomPos = getattr(mod, "XSDK_CapLensZoomPos", None)

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

        self.XSDK_GetDeviceInfo.argtypes = [
            c_longlong,
            ctypes.POINTER(DeviceInformation),
        ]
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

        self.XSDK_CapAEMode.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapAEMode.restype = c_long

        self.XSDK_SetAEMode.argtypes = [c_longlong, c_long]
        self.XSDK_SetAEMode.restype = c_long

        self.XSDK_GetAEMode.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetAEMode.restype = c_long

        self.XSDK_CapShutterSpeed.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapShutterSpeed.restype = c_long

        self.XSDK_SetShutterSpeed.argtypes = [c_longlong, c_long, c_long]
        self.XSDK_SetShutterSpeed.restype = c_long

        self.XSDK_GetShutterSpeed.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_GetShutterSpeed.restype = c_long

        self.XSDK_CapSensitivity.argtypes = [
            c_longlong,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapSensitivity.restype = c_long

        self.XSDK_SetSensitivity.argtypes = [c_longlong, c_long]
        self.XSDK_SetSensitivity.restype = c_long

        self.XSDK_GetSensitivity.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetSensitivity.restype = c_long

        self.XSDK_GetLensZoomPos.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetLensZoomPos.restype = c_long

        if self.XSDK_CapLensZoomPos is not None:
            self.XSDK_CapLensZoomPos.argtypes = [
                c_longlong,
                ctypes.POINTER(c_long),
                ctypes.POINTER(c_long),
            ]
            self.XSDK_CapLensZoomPos.restype = c_long

        self.XSDK_CapAperture.argtypes = [
            c_longlong,
            c_long,
            ctypes.POINTER(c_long),
            ctypes.POINTER(c_long),
        ]
        self.XSDK_CapAperture.restype = c_long

        self.XSDK_SetAperture.argtypes = [c_longlong, c_long]
        self.XSDK_SetAperture.restype = c_long

        self.XSDK_GetAperture.argtypes = [c_longlong, ctypes.POINTER(c_long)]
        self.XSDK_GetAperture.restype = c_long

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

        self.XSDK_ReadImageInfo.argtypes = [
            c_longlong,
            ctypes.POINTER(ImageInformation),
        ]
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

        logger.debug("functions bound")

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _update_error(self, camera_handle=None):
        if self.module is None:
            return

        handle = camera_handle if camera_handle is not None else c_longlong(0)

        try:
            self.XSDK_GetErrorNumber(handle, byref(self.api_code), byref(self.err_code))
            logger.debug(
                f"last_error api_code=0x{self.api_code.value:x} "
                f"err_code=0x{self.err_code.value:x}"
            )
        except Exception as exc:
            logger.debug(f"could not retrieve last_error: {exc}")

    def _check(self, result, fn_name, camera_handle=None):
        logger.debug(f"{fn_name} -> rc={result}")
        if result != XSDK_COMPLETE:
            self._update_error(camera_handle)
            raise XsdkRuntimeError(
                f"{fn_name} failed rc={result} "
                f"api_code=0x{self.api_code.value:x} "
                f"err_code=0x{self.err_code.value:x}"
            )
        return result

    # ------------------------------------------------------------------
    # SDK lifecycle
    # ------------------------------------------------------------------

    def init_sdk(self):
        with self._lock:
            logger.debug("XSDK_Init(NULL)")
            result = self.XSDK_Init(None)
            self._check(result, "XSDK_Init")

    def exit_sdk(self):
        with self._lock:
            if self.module is None:
                logger.debug("XSDK_Exit skip (module None)")
                return
            logger.debug("XSDK_Exit")
            result = self.XSDK_Exit()
            self._check(result, "XSDK_Exit")

    # ------------------------------------------------------------------
    # Camera enumeration / open
    # ------------------------------------------------------------------

    def detect_usb_cameras(self):
        with self._lock:
            count = c_long(0)
            logger.debug("XSDK_Detect USB")
            result = self.XSDK_Detect(XSDK_DSC_IF_USB, None, None, byref(count))
            self._check(result, "XSDK_Detect")
            logger.debug(f"cameras detected: {count.value}")
            return count.value

    def open_first_camera(self):
        with self._lock:
            camera_handle = c_longlong(0)
            camera_mode = c_long(0)
            device = b"ENUM:0"

            logger.debug(f"XSDK_OpenEx device={device!r}")
            logger.debug(
                f"before open handle={camera_handle.value} mode={camera_mode.value}"
            )

            result = self.XSDK_OpenEx(
                device, byref(camera_handle), byref(camera_mode), None
            )
            self._check(result, "XSDK_OpenEx")

            logger.debug(
                f"after open handle={camera_handle.value} mode={camera_mode.value}"
            )

            info = self.get_device_info(camera_handle)
            return camera_handle, info

    def close_camera(self, camera_handle):
        with self._lock:
            logger.debug(f"XSDK_Close handle={camera_handle.value}")
            result = self.XSDK_Close(camera_handle)
            self._check(result, "XSDK_Close", camera_handle)

    # ------------------------------------------------------------------
    # Device info
    # ------------------------------------------------------------------

    def get_device_info(self, camera_handle):
        with self._lock:
            info = DeviceInformation()

            logger.debug(f"XSDK_GetDeviceInfo handle={camera_handle.value}")
            result = self.XSDK_GetDeviceInfo(camera_handle, byref(info))
            self._check(result, "XSDK_GetDeviceInfo", camera_handle)

            model_str = (
                info.strProduct.decode("utf-8", errors="ignore").strip("\x00 ").strip()
                or "FUJIFILM"
            )
            serial_str = (
                info.strSerialNo.decode("utf-8", errors="ignore").strip("\x00 ").strip()
                or "UNKNOWN"
            )
            device_name = (
                info.strDeviceName.decode("utf-8", errors="ignore")
                .strip("\x00 ")
                .strip()
                or "ENUM:0"
            )

            logger.debug(
                f"device_info model={model_str} serial={serial_str} device_name={device_name}"
            )
            return RawCameraInfo(
                model=model_str, serial=serial_str, device_name=device_name
            )

    # ------------------------------------------------------------------
    # Priority and camera settings
    # ------------------------------------------------------------------

    def set_priority_mode(self, camera_handle, mode=XSDK_PRIORITY_CAMERA):
        with self._lock:
            label = "PC" if mode == XSDK_PRIORITY_PC else "CAMERA"
            logger.debug(
                f"XSDK_SetPriorityMode handle={camera_handle.value} mode={mode} ({label})"
            )
            result = self.XSDK_SetPriorityMode(camera_handle, mode)
            self._check(result, "XSDK_SetPriorityMode", camera_handle)
            self.current_priority_mode = mode

    def get_priority_mode(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetPriorityMode handle={camera_handle.value}")
            result = self.XSDK_GetPriorityMode(camera_handle, byref(value))
            self._check(result, "XSDK_GetPriorityMode", camera_handle)
            logger.debug(f"XSDK_GetPriorityMode -> value={value.value}")
            return int(value.value)

    def ensure_pc_priority(self, camera_handle):
        if self.current_priority_mode != XSDK_PRIORITY_PC:
            logger.debug("ensure_pc_priority() -> switching to PC PRIORITY")
            self.set_priority_mode(camera_handle, XSDK_PRIORITY_PC)
            time.sleep(0.5)

    def ensure_camera_priority(self, camera_handle):
        if self.current_priority_mode != XSDK_PRIORITY_CAMERA:
            logger.debug(
                "ensure_camera_priority() -> switching to CAMERA PRIORITY"
            )
            self.set_priority_mode(camera_handle, XSDK_PRIORITY_CAMERA)
            time.sleep(0.5)

    def set_media_record(self, camera_handle, mode=XSDK_MEDIAREC_JPEG):
        with self._lock:
            logger.debug(
                f"XSDK_SetMediaRecord handle={camera_handle.value} mode={mode}"
            )
            result = self.XSDK_SetMediaRecord(camera_handle, mode)
            self._check(result, "XSDK_SetMediaRecord", camera_handle)

    def get_drive_mode(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetDriveMode handle={camera_handle.value}")
            result = self.XSDK_GetDriveMode(camera_handle, byref(value))
            self._check(result, "XSDK_GetDriveMode", camera_handle)
            logger.debug(f"XSDK_GetDriveMode -> value={value.value}")
            return int(value.value)

    def cap_ae_mode(self, camera_handle):
        with self._lock:
            num = c_long(0)
            logger.debug(
                f"XSDK_CapAEMode handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapAEMode(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapAEMode(count)", camera_handle)
            if num.value <= 0:
                return []
            arr_type = c_long * num.value
            arr = arr_type()
            logger.debug(
                f"XSDK_CapAEMode handle={camera_handle.value} query modes"
            )
            result = self.XSDK_CapAEMode(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapAEMode(list)", camera_handle)
            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapAEMode supported_modes={supported}")
            return supported

    def set_ae_mode(self, camera_handle, mode):
        with self._lock:
            logger.debug(
                f"XSDK_SetAEMode handle={camera_handle.value} mode={mode}"
            )
            result = self.XSDK_SetAEMode(camera_handle, mode)
            self._check(result, "XSDK_SetAEMode", camera_handle)

    def get_ae_mode(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetAEMode handle={camera_handle.value}")
            result = self.XSDK_GetAEMode(camera_handle, byref(value))
            self._check(result, "XSDK_GetAEMode", camera_handle)
            logger.debug(f"XSDK_GetAEMode -> value={value.value}")
            return int(value.value)

    def cap_shutter_speed(self, camera_handle):
        with self._lock:
            num = c_long(0)
            bulb = c_long(0)
            logger.debug(
                f"XSDK_CapShutterSpeed handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapShutterSpeed(
                camera_handle, byref(num), None, byref(bulb)
            )
            self._check(result, "XSDK_CapShutterSpeed(count)", camera_handle)
            if num.value <= 0:
                return [], bool(bulb.value)
            arr_type = c_long * num.value
            arr = arr_type()
            logger.debug(
                f"XSDK_CapShutterSpeed handle={camera_handle.value} query list"
            )
            result = self.XSDK_CapShutterSpeed(
                camera_handle, byref(num), arr, byref(bulb)
            )
            self._check(result, "XSDK_CapShutterSpeed(list)", camera_handle)
            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(
                f"XSDK_CapShutterSpeed supported={supported} bulb={bulb.value}"
            )
            return supported, bool(bulb.value)

    def set_shutter_speed(self, camera_handle, shutter_speed, bulb=False):
        with self._lock:
            logger.debug(
                f"XSDK_SetShutterSpeed handle={camera_handle.value} value={shutter_speed} bulb={bulb}"
            )
            result = self.XSDK_SetShutterSpeed(
                camera_handle, shutter_speed, 1 if bulb else 0
            )
            self._check(result, "XSDK_SetShutterSpeed", camera_handle)

    def get_shutter_speed(self, camera_handle):
        with self._lock:
            shutter_speed = c_long(0)
            bulb = c_long(0)
            logger.debug(f"XSDK_GetShutterSpeed handle={camera_handle.value}")
            result = self.XSDK_GetShutterSpeed(
                camera_handle, byref(shutter_speed), byref(bulb)
            )
            self._check(result, "XSDK_GetShutterSpeed", camera_handle)
            logger.debug(
                f"XSDK_GetShutterSpeed -> value={shutter_speed.value} bulb={bulb.value}"
            )
            return int(shutter_speed.value), bool(bulb.value)

    def cap_sensitivity(self, camera_handle):
        with self._lock:
            num = c_long(0)
            logger.debug(
                f"XSDK_CapSensitivity handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapSensitivity(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapSensitivity(count)", camera_handle)
            if num.value <= 0:
                return []
            arr_type = c_long * num.value
            arr = arr_type()
            logger.debug(
                f"XSDK_CapSensitivity handle={camera_handle.value} query list"
            )
            result = self.XSDK_CapSensitivity(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapSensitivity(list)", camera_handle)
            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapSensitivity supported={supported}")
            return supported

    def set_sensitivity(self, camera_handle, sensitivity):
        with self._lock:
            logger.debug(
                f"XSDK_SetSensitivity handle={camera_handle.value} value={sensitivity}"
            )
            result = self.XSDK_SetSensitivity(camera_handle, sensitivity)
            self._check(result, "XSDK_SetSensitivity", camera_handle)

    def get_sensitivity(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetSensitivity handle={camera_handle.value}")
            result = self.XSDK_GetSensitivity(camera_handle, byref(value))
            self._check(result, "XSDK_GetSensitivity", camera_handle)
            logger.debug(f"XSDK_GetSensitivity -> value={value.value}")
            return int(value.value)

    def get_lens_zoom_pos(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetLensZoomPos handle={camera_handle.value}")
            result = self.XSDK_GetLensZoomPos(camera_handle, byref(value))
            self._check(result, "XSDK_GetLensZoomPos", camera_handle)
            logger.debug(f"XSDK_GetLensZoomPos -> value={value.value}")
            return int(value.value)

    def cap_lens_zoom_pos(self, camera_handle):
        with self._lock:
            if self.XSDK_CapLensZoomPos is None:
                logger.debug(
                    "XSDK_CapLensZoomPos not available in this library build"
                )
                return []

            num = c_long(0)
            logger.debug(
                f"XSDK_CapLensZoomPos handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapLensZoomPos(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapLensZoomPos(count)", camera_handle)

            if num.value <= 0:
                return []

            arr_type = c_long * num.value
            arr = arr_type()

            logger.debug(
                f"XSDK_CapLensZoomPos handle={camera_handle.value} query list"
            )
            result = self.XSDK_CapLensZoomPos(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapLensZoomPos(list)", camera_handle)

            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapLensZoomPos supported={supported}")
            return supported

    def get_current_zoom_pos(self, camera_handle):
        try:
            zoom_pos = self.get_lens_zoom_pos(camera_handle)
            logger.debug(
                f"get_current_zoom_pos -> from GetLensZoomPos={zoom_pos}"
            )
            return int(zoom_pos)
        except Exception as exc:
            logger.debug(f"get_current_zoom_pos GetLensZoomPos failed: {exc}")

        try:
            positions = self.cap_lens_zoom_pos(camera_handle)
            if positions:
                zoom_pos = int(positions[0])
                logger.debug(
                    f"get_current_zoom_pos -> fallback CapLensZoomPos={zoom_pos}"
                )
                return zoom_pos
        except Exception as exc:
            logger.debug(f"get_current_zoom_pos CapLensZoomPos failed: {exc}")

        logger.debug("get_current_zoom_pos -> fallback 0")
        return 0

    def cap_aperture(self, camera_handle, zoom_pos):
        with self._lock:
            num = c_long(0)
            logger.debug(
                f"XSDK_CapAperture handle={camera_handle.value} zoom_pos={zoom_pos} query count"
            )
            result = self.XSDK_CapAperture(camera_handle, zoom_pos, byref(num), None)
            self._check(result, "XSDK_CapAperture(count)", camera_handle)
            if num.value <= 0:
                return []
            arr_type = c_long * num.value
            arr = arr_type()
            logger.debug(
                f"XSDK_CapAperture handle={camera_handle.value} zoom_pos={zoom_pos} query list"
            )
            result = self.XSDK_CapAperture(camera_handle, zoom_pos, byref(num), arr)
            self._check(result, "XSDK_CapAperture(list)", camera_handle)
            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapAperture supported={supported}")
            return supported

    def set_aperture(self, camera_handle, f_number_x100):
        with self._lock:
            logger.debug(
                f"XSDK_SetAperture handle={camera_handle.value} value={f_number_x100}"
            )
            result = self.XSDK_SetAperture(camera_handle, f_number_x100)
            self._check(result, "XSDK_SetAperture", camera_handle)

    def get_aperture(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetAperture handle={camera_handle.value}")
            result = self.XSDK_GetAperture(camera_handle, byref(value))
            self._check(result, "XSDK_GetAperture", camera_handle)
            logger.debug(f"XSDK_GetAperture -> value={value.value}")
            return int(value.value)

    # ------------------------------------------------------------------
    # Release / capture
    # ------------------------------------------------------------------

    def get_mode(self, camera_handle):
        with self._lock:
            value = c_long(0)
            logger.debug(f"XSDK_GetMode handle={camera_handle.value}")

            result = self.XSDK_GetMode(camera_handle, byref(value))
            if result == XSDK_COMPLETE:
                logger.debug(f"XSDK_GetMode -> value={value.value}")
                return int(value.value)

            self._update_error(camera_handle)

            if self.err_code.value == 0x1005:
                detail = self.get_error_details(camera_handle)
                detail_name = self.decode_error_details(detail)
                logger.debug(
                    "XSDK_GetMode busy "
                    f"api_code=0x{self.api_code.value:x} "
                    f"err_code=0x{self.err_code.value:x} "
                    f"detail={detail_name}"
                )
                return None

            self._check(result, "XSDK_GetMode", camera_handle)
            return None

    def set_force_shoot_mode(self, camera_handle):
        with self._lock:
            logger.debug(f"XSDK_SetForceMode handle={camera_handle.value} SHOOT")
            result = self.XSDK_SetForceMode(camera_handle, XSDK_FORCESHOOTSTANDBY_SHOOT)
            self._check(result, "XSDK_SetForceMode(SHOOT)", camera_handle)
            logger.debug("XSDK_SetForceMode(SHOOT) -> ok")

    def get_release_status(self, camera_handle):
        with self._lock:
            status = c_long(0)
            logger.debug(f"XSDK_GetReleaseStatus handle={camera_handle.value}")
            result = self.XSDK_GetReleaseStatus(camera_handle, byref(status))
            self._check(result, "XSDK_GetReleaseStatus", camera_handle)
            logger.debug(f"XSDK_GetReleaseStatus -> status={status.value}")
            return int(status.value)

    def _decode_release_status(self, status):
        flags = []
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

    def cap_release(self, camera_handle):
        with self._lock:
            self.ensure_pc_priority(camera_handle)

            num = c_long(0)

            logger.debug(
                f"XSDK_CapRelease handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapRelease(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapRelease(count)", camera_handle)

            logger.debug(f"XSDK_CapRelease supported_count={num.value}")

            if num.value <= 0:
                return []

            arr_type = c_long * num.value
            arr = arr_type()

            logger.debug(
                f"XSDK_CapRelease handle={camera_handle.value} query modes"
            )
            result = self.XSDK_CapRelease(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapRelease(list)", camera_handle)

            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapRelease supported_modes={supported}")
            return supported

    def _release_once(self, camera_handle, release_mode, label):
        shot_option = c_long(0)
        af_status = c_long(0)

        logger.debug(
            f"XSDK_Release handle={camera_handle.value} mode={release_mode} ({label})"
        )
        result = self.XSDK_Release(
            camera_handle,
            release_mode,
            byref(shot_option),
            byref(af_status),
        )
        self._check(result, f"XSDK_Release({label})", camera_handle)

        logger.debug(
            f"XSDK_Release {label} "
            f"shot_option={shot_option.value} af_status={af_status.value}"
        )
        return shot_option.value, af_status.value

    def release_pc(self, camera_handle):
        with self._lock:
            self.ensure_pc_priority(camera_handle)

            try:
                self.set_force_shoot_mode(camera_handle)
                time.sleep(0.8)
            except Exception as exc:
                logger.debug(f"set_force_shoot_mode failed: {exc}")

            try:
                status_before = self.get_release_status(camera_handle)
                logger.debug(
                    "release status before capture="
                    f"{status_before} flags={self._decode_release_status(status_before)}"
                )
            except Exception as exc:
                logger.debug(f"get_release_status(before) failed: {exc}")

            supported = self.cap_release(camera_handle)
            logger.debug(f"release_pc supported={supported}")

            if not supported:
                raise XsdkRuntimeError("No PC release mode available")

            preferred_modes = []

            if 260 in supported:
                preferred_modes.append((260, "MODE_260"))

            if XSDK_RELEASE_SHOOT_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_SHOOT_S1OFF, "SHOOT_S1OFF"))

            if XSDK_RELEASE_S1ON in supported:
                preferred_modes.append((XSDK_RELEASE_S1ON, "S1_ON"))

            if XSDK_RELEASE_S2_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_S2_S1OFF, "S2_S1OFF"))

            if XSDK_RELEASE_N_S1OFF in supported:
                preferred_modes.append((XSDK_RELEASE_N_S1OFF, "N_S1OFF"))

            errors = []

            for mode, label in preferred_modes:
                try:
                    self._release_once(camera_handle, mode, label)
                    time.sleep(0.8)
                    logger.debug(f"release_pc success with {label}")
                    return
                except Exception as exc:
                    logger.debug(f"release_pc mode {label} failed: {exc}")
                    errors.append(f"{label}: {exc}")

            raise XsdkRuntimeError(
                f"No PC release mode succeeded. supported={supported}; errors={errors}"
            )

    def cap_release_ex(self, camera_handle):
        with self._lock:
            self.ensure_camera_priority(camera_handle)

            num = c_long(0)

            logger.debug(
                f"XSDK_CapReleaseEx handle={camera_handle.value} query count"
            )
            result = self.XSDK_CapReleaseEx(camera_handle, byref(num), None)
            self._check(result, "XSDK_CapReleaseEx(count)", camera_handle)

            logger.debug(f"XSDK_CapReleaseEx supported_count={num.value}")

            if num.value <= 0:
                return []

            arr_type = c_long * num.value
            arr = arr_type()

            logger.debug(
                f"XSDK_CapReleaseEx handle={camera_handle.value} query modes"
            )
            result = self.XSDK_CapReleaseEx(camera_handle, byref(num), arr)
            self._check(result, "XSDK_CapReleaseEx(list)", camera_handle)

            supported = [int(arr[i]) for i in range(num.value)]
            logger.debug(f"XSDK_CapReleaseEx supported_modes={supported}")
            return supported

    def release_camera_priority(self, camera_handle):
        with self._lock:
            self.ensure_camera_priority(camera_handle)

            try:
                status_before = self.get_release_status(camera_handle)
                logger.debug(
                    "release_ex status before capture="
                    f"{status_before} flags={self._decode_release_status(status_before)}"
                )
            except Exception as exc:
                logger.debug(f"get_release_status(before ex) failed: {exc}")

            supported = self.cap_release_ex(camera_handle)
            logger.debug(f"release_camera_priority supported={supported}")

            if not supported:
                logger.debug("no ReleaseEx mode available in current state")
                return False

            shot_option = c_long(0)
            af_status = c_long(0)

            if XSDK_RELEASE_EX_GRAB in supported:
                logger.debug("using XSDK_RELEASE_EX_GRAB")
                result = self.XSDK_ReleaseEx(
                    camera_handle,
                    XSDK_RELEASE_EX_GRAB,
                    byref(shot_option),
                    byref(af_status),
                )
                self._check(result, "XSDK_ReleaseEx(GRAB)", camera_handle)
                logger.debug(
                    f"XSDK_ReleaseEx(GRAB) "
                    f"shot_option={shot_option.value} af_status={af_status.value}"
                )
                time.sleep(0.8)
                return True

            if XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF in supported:
                logger.debug("using XSDK_RELEASE_EX_S1_ON_S2_ON_S2_OFF_S1_OFF")
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
                logger.debug(
                    f"XSDK_ReleaseEx(full-sequence) "
                    f"shot_option={shot_option.value} af_status={af_status.value}"
                )
                time.sleep(0.8)
                return True

            logger.debug("no usable ReleaseEx mode found")
            return False

    def release_ex(self, camera_handle, release_mode):
        with self._lock:
            shot_option = c_long(0)
            af_status = c_long(0)

            logger.debug(
                f"XSDK_ReleaseEx handle={camera_handle.value} "
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
                logger.debug(
                    f"XSDK_ReleaseEx failed rc={result} "
                    f"api_code=0x{self.api_code.value:x} "
                    f"err_code=0x{self.err_code.value:x}"
                )
                return False

            logger.debug(
                f"XSDK_ReleaseEx ok "
                f"shot_option={shot_option.value} af_status={af_status.value}"
            )
            return True

    # ------------------------------------------------------------------
    # Image buffer access
    # ------------------------------------------------------------------

    def read_image_info(self, camera_handle):
        with self._lock:
            info = ImageInformation()
            logger.debug(f"XSDK_ReadImageInfo handle={camera_handle.value}")
            result = self.XSDK_ReadImageInfo(camera_handle, byref(info))
            self._check(result, "XSDK_ReadImageInfo", camera_handle)
            logger.debug(
                "image_info "
                f"format={info.format} data_size={info.data_size} "
                f"preview_size={info.preview_size} "
                f"w={info.image_pix_width} h={info.image_pix_height}"
            )
            return info

    def read_preview(self, camera_handle, size):
        with self._lock:
            logger.debug(
                f"XSDK_ReadPreview handle={camera_handle.value} size={size}"
            )

            if size <= 0:
                raise XsdkRuntimeError(f"XSDK_ReadPreview invalid size: {size}")

            data = create_string_buffer(size)
            result = self.XSDK_ReadPreview(camera_handle, data, size)
            self._check(result, "XSDK_ReadPreview", camera_handle)

            payload = bytes(data.raw[:size])
            logger.debug(f"XSDK_ReadPreview bytes={len(payload)}")
            return payload

    def read_image(self, camera_handle, size):
        with self._lock:
            logger.debug(
                f"XSDK_ReadImage handle={camera_handle.value} size={size}"
            )

            if size <= 0:
                raise XsdkRuntimeError(f"XSDK_ReadImage invalid size: {size}")

            data = create_string_buffer(size)
            result = self.XSDK_ReadImage(camera_handle, data, size)
            self._check(result, "XSDK_ReadImage", camera_handle)

            payload = bytes(data.raw[:size])
            logger.debug(f"XSDK_ReadImage bytes={len(payload)}")
            return payload

    def get_buffer_capacity(self, camera_handle):
        with self._lock:
            shoot_frame_num = c_long(0)
            total_frame_num = c_long(0)

            logger.debug(f"XSDK_GetBufferCapacity handle={camera_handle.value}")
            result = self.XSDK_GetBufferCapacity(
                camera_handle,
                byref(shoot_frame_num),
                byref(total_frame_num),
            )
            self._check(result, "XSDK_GetBufferCapacity", camera_handle)

            logger.debug(
                f"XSDK_GetBufferCapacity -> "
                f"shot={shoot_frame_num.value} total={total_frame_num.value}"
            )
            return int(shoot_frame_num.value), int(total_frame_num.value)

    def drain_read_buffer(self, camera_handle, max_items=32):
        with self._lock:
            drained = 0
            logger.debug(f"drain_read_buffer handle={camera_handle.value}")

            for _ in range(max_items):
                info = ImageInformation()
                result = self.XSDK_ReadImageInfo(camera_handle, byref(info))
                self._check(result, "XSDK_ReadImageInfo", camera_handle)

                fmt = info.format & 0xFF
                if fmt == XSDK_IMAGEFORMAT_NONE or info.data_size <= 0:
                    logger.debug(f"drain_read_buffer done drained={drained}")
                    return drained

                logger.debug(
                    f"drain item fmt={fmt} size={info.data_size} "
                    f"preview_size={info.preview_size}"
                )
                _ = self.read_image(camera_handle, int(info.data_size))
                drained += 1

            logger.debug(
                f"drain_read_buffer reached max_items drained={drained}"
            )
            return drained

    def delete_image(self, camera_handle):
        with self._lock:
            logger.debug(f"XSDK_DeleteImage handle={camera_handle.value}")
            result = self.XSDK_DeleteImage(camera_handle)
            self._check(result, "XSDK_DeleteImage", camera_handle)

    # ------------------------------------------------------------------
    # Generic SetProp
    # ------------------------------------------------------------------

    def set_prop(self, camera_handle, api_code, api_param=0, *extra):
        with self._lock:
            printable = [api_param, *extra]
            logger.debug(
                f"XSDK_SetProp handle={camera_handle.value} "
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
        camera_handle,
        *,
        quality=SDK_LIVEVIEW_QUALITY_FINE,
        size=SDK_LIVEVIEW_SIZE_L,
        mode=SDK_LIVEVIEW_MODE1,
    ):
        with self._lock:
            logger.debug(
                f"start_live_view quality={quality} size={size} mode={mode}"
            )

            self.ensure_pc_priority(camera_handle)

            try:
                self.drain_read_buffer(camera_handle)
            except Exception as exc:
                logger.debug(
                    f"drain_read_buffer warning before StartLiveView: {exc}"
                )

            props_ok = True

            try:
                self.set_prop(
                    camera_handle, API_CODE_SET_LIVE_VIEW_IMAGE_QUALITY, 1, quality
                )
            except XsdkRuntimeError as exc:
                props_ok = False
                logger.debug(f"warning: live view quality not applied: {exc}")

            try:
                self.set_prop(camera_handle, API_CODE_SET_LIVE_VIEW_IMAGE_SIZE, 1, size)
            except XsdkRuntimeError as exc:
                props_ok = False
                logger.debug(f"warning: live view size not applied: {exc}")

            try:
                self.set_prop(camera_handle, API_CODE_SET_LIVE_VIEW_MODE, 1, mode)
            except XsdkRuntimeError as exc:
                props_ok = False
                logger.debug(f"warning: live view mode not applied: {exc}")

            if not props_ok:
                logger.debug(
                    "some live view props failed, attempting StartLiveView anyway"
                )

            self.set_prop(camera_handle, API_CODE_START_LIVE_VIEW, 0)
            self.live_view_running = True
            logger.debug("live view started")

    def stop_live_view(self, camera_handle):
        with self._lock:
            logger.debug("stop_live_view")
            try:
                self.set_prop(camera_handle, API_CODE_STOP_LIVE_VIEW, 0)
            except XsdkRuntimeError as exc:
                logger.debug(f"warning: StopLiveView failed: {exc}")
            self.live_view_running = False

    # ------------------------------------------------------------------
    # End-to-end debug helper
    # ------------------------------------------------------------------

    def debug_open_sequence(self):
        logger.debug("debug_open_sequence()")
        self.load()
        self.init_sdk()

        if platform.system() == "Darwin":
            logger.debug("macOS detected -> waiting 3s")
            time.sleep(3)

        count = self.detect_usb_cameras()
        if count <= 0:
            raise XsdkRuntimeError("No camera detected")

        logger.debug("waiting 1s before open")
        time.sleep(1)

        handle, info = self.open_first_camera()
        logger.debug(f"session opened handle={handle.value} info={info}")
        return handle, info

    def get_error_details(self, camera_handle=None):
        with self._lock:
            if self.module is None:
                return None

            value = c_long(0)
            handle = camera_handle if camera_handle is not None else c_longlong(0)

            logger.debug(f"XSDK_GetErrorDetails handle={handle.value}")
            result = self.XSDK_GetErrorDetails(handle, byref(value))
            if result != XSDK_COMPLETE:
                logger.debug(f"XSDK_GetErrorDetails failed rc={result}")
                return None

            logger.debug(f"XSDK_GetErrorDetails -> value={value.value}")
            return int(value.value)

    def decode_error_details(self, detail):
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

    def run_precheck(self, camera_handle):
        with self._lock:
            report = CameraPrecheckReport(connected=True)

            def _safe_read(name, fn):
                try:
                    return fn()
                except Exception as exc:
                    logger.debug(f"{name} failed: {exc}")
                    report.issues.append(
                        PrecheckIssue(
                            level="warning",
                            code=f"{name}_failed",
                            message=f"Could not read {name}: {exc}",
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
                logger.debug(f"cap_release failed: {exc}")
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="cap_release_failed",
                        message=f"Could not read PC release modes: {exc}",
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
                            logger.debug(
                                f"restore priority failed: {exc}"
                            )
            except Exception as exc:
                logger.debug(f"cap_release_ex failed: {exc}")
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="cap_release_ex_failed",
                        message=f"Could not read ReleaseEx modes: {exc}",
                    )
                )

            if report.release_status_value is not None:
                flags = self._decode_release_status(report.release_status_value)
                logger.debug(
                    "release_status="
                    f"{report.release_status_value} flags={flags}"
                )

            if not report.cap_release:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="no_pc_release_modes",
                        message="No PC release mode currently available.",
                    )
                )

            if not report.cap_release_ex:
                report.issues.append(
                    PrecheckIssue(
                        level="error",
                        code="no_camera_release_ex_modes",
                        message=(
                            "No Camera Priority release mode available. "
                            "Set camera to photo mode, single drive, not video."
                        ),
                    )
                )

            if report.mode_value is None:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="unknown_mode",
                        message="Camera mode could not be read via SDK.",
                    )
                )

            if report.drive_mode_value is None:
                report.issues.append(
                    PrecheckIssue(
                        level="warning",
                        code="unknown_drive_mode",
                        message="Drive mode could not be read via SDK.",
                    )
                )

            logger.debug("report summary")
            logger.debug(f"connected={report.connected}")
            logger.debug(f"mode_value={report.mode_value}")
            logger.debug(f"drive_mode_value={report.drive_mode_value}")
            logger.debug(
                f"priority_mode_value={report.priority_mode_value}"
            )
            logger.debug(
                f"release_status_value={report.release_status_value}"
            )
            logger.debug(f"cap_release={report.cap_release}")
            logger.debug(f"cap_release_ex={report.cap_release_ex}")
            for issue in report.issues:
                logger.debug(
                    f"issue {issue.level} {issue.code}: {issue.message}"
                )

            return report


def image_suffix_for_format(image_format):
    fmt = image_format & 0xFF
    if fmt == XSDK_IMAGEFORMAT_RAW:
        return ".RAF"
    if fmt == XSDK_IMAGEFORMAT_HEIF:
        return ".HEIC"
    if fmt == XSDK_IMAGEFORMAT_JPEG:
        return ".JPG"
    return ".BIN"
