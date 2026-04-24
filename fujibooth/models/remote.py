from enum import Enum, auto


class RemoteButton(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    CENTER = auto()
    CAMERA = auto()  # camera-icon button (middle row)
    PHOTO = auto()  # photo button (bottom row)
