from enum import Enum


class BackendState(str, Enum):
    WAITING_FOR_CAMERA = "waiting_for_camera"
    CAMERA_READY = "camera_ready"
    LIVE_VIEW = "live_view"
    UPDATING_CAMERA_PARAMS = "updating_camera_params"
    CAPTURING = "capturing"
    DOWNLOADING = "downloading"
    ERROR = "error"
