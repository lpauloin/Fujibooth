from __future__ import annotations

from enum import Enum


class BoothState(str, Enum):
    BOOTING = 'booting'
    WAITING_FOR_CAMERA = 'waiting_for_camera'
    LIVE_VIEW = 'live_view'
    COUNTDOWN = 'countdown'
    CAPTURING = 'capturing'
    DOWNLOADING = 'downloading'
    FREEZE = 'freeze'
    PHOTO_SELECTED = 'photo_selected'
    PRINTING = 'printing'
    ERROR = 'error'
