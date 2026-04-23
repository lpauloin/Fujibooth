from enum import Enum, auto


class BoothState(Enum):
    BOOTING = auto()
    WAITING_FOR_CAMERA = auto()
    LIVE_VIEW = auto()
    COUNTDOWN = auto()
    CAPTURING = auto()
    DOWNLOADING = auto()
    FREEZE = auto()
    PHOTO_SELECTED = auto()
    PRINTING = auto()
    ERROR = auto()


class BackendState(Enum):
    WAITING_FOR_CAMERA = auto()
    CAMERA_READY = auto()
    LIVE_VIEW = auto()
    UPDATING_CAMERA_PARAMS = auto()
    CAPTURING = auto()
    DOWNLOADING = auto()
    ERROR = auto()


class SessionState(Enum):
    STOPPED = auto()
    WAITING_USB = auto()
    CONNECTING = auto()
    READY = auto()
    LIVE = auto()
    QUERYING = auto()
    CAPTURING = auto()
    ERROR = auto()
