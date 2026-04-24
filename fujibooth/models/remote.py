from enum import Enum, auto


class RemoteButton(Enum):
    UP = auto()
    DOWN = auto()
    LEFT = auto()
    RIGHT = auto()
    CENTER = auto()
    PHOTO = auto()


class RemoteFocus(Enum):
    SLIDESHOW = auto()
    CONTROLS = auto()