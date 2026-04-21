from enum import Enum


class BackendState(str, Enum):
    WAITING_FOR_CAMERA = "waiting_for_camera"
    CAMERA_READY = "camera_ready"
    LIVE_VIEW = "live_view"
    UPDATING_EXPOSURE = "updating_exposure"
    CAPTURING = "capturing"
    DOWNLOADING = "downloading"
    ERROR = "error"
