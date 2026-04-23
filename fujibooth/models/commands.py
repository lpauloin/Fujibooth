from enum import Enum, auto


class WorkerCommand(Enum):
    SHUTDOWN = auto()
    USB_CONNECTED = auto()
    USB_DISCONNECTED = auto()
    CONNECT = auto()
    START_LIVE_VIEW = auto()
    STOP_LIVE_VIEW = auto()
    REFRESH_EXPOSURE = auto()
    APPLY_EXPOSURE = auto()
    CAPTURE = auto()
