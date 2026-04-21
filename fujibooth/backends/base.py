from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QPixmap

from .state import BackendState


class CameraBackend(QObject):
    live_view_updated = Signal(QPixmap)
    photo_captured = Signal(str)
    camera_connected = Signal(dict)
    camera_disconnected = Signal()
    state_changed = Signal(object)
    error = Signal(str)

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def is_connected(self) -> bool:
        raise NotImplementedError

    def connect_camera(self) -> None:
        raise NotImplementedError

    def disconnect_camera(self) -> None:
        raise NotImplementedError

    def start_live_view(self) -> None:
        raise NotImplementedError

    def stop_live_view(self) -> None:
        raise NotImplementedError

    def trigger_capture(self) -> None:
        raise NotImplementedError

    def emit_backend_state(self, state: BackendState) -> None:
        self.state_changed.emit(state)

    def _emit_photo(self, path: Path) -> None:
        self.photo_captured.emit(str(path))
