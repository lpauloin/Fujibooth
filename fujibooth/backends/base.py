from PySide6.QtCore import QObject, Signal

from ..models.state import BackendState


class CameraBackend(QObject):
    live_view_updated = Signal(object)
    photo_captured = Signal(str)
    camera_connected = Signal(dict)
    camera_disconnected = Signal()
    state_changed = Signal(object)
    error = Signal(str)
    exposure_data_ready = Signal(object)
    exposure_data_failed = Signal(str)

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def is_connected(self):
        raise NotImplementedError

    def connect_camera(self):
        raise NotImplementedError

    def disconnect_camera(self):
        raise NotImplementedError

    def start_live_view(self):
        raise NotImplementedError

    def stop_live_view(self):
        raise NotImplementedError

    def trigger_capture(self):
        raise NotImplementedError

    def emit_backend_state(self, state):
        self.state_changed.emit(state)

    def _emit_photo(self, path):
        self.photo_captured.emit(str(path))