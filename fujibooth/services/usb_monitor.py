from __future__ import annotations

from dataclasses import dataclass
import platform
import subprocess

from PySide6.QtCore import QObject, QTimer, Signal


@dataclass(slots=True)
class USBMonitorConfig:
    vendor_id: str
    product_ids: list[str]
    camera_name_contains: str


class USBMonitor(QObject):
    connected = Signal(dict)
    disconnected = Signal()

    def __init__(self, config: USBMonitorConfig, poll_interval_ms: int = 1500) -> None:
        super().__init__()
        self.config = config
        self._connected = False
        self.timer = QTimer(self)
        self.timer.setInterval(poll_interval_ms)
        self.timer.timeout.connect(self._poll)

    def start(self) -> None:
        self._poll()
        self.timer.start()

    def stop(self) -> None:
        self.timer.stop()

    def _poll(self) -> None:
        info = self.detect_once()
        if info and not self._connected:
            self._connected = True
            self.connected.emit(info)
        elif not info and self._connected:
            self._connected = False
            self.disconnected.emit()

    def detect_once(self) -> dict | None:
        system = platform.system().lower()
        label = self.config.camera_name_contains.lower()
        vendor = self.config.vendor_id.lower()
        try:
            if system == 'darwin':
                output = subprocess.check_output(['system_profiler', 'SPUSBDataType'], text=True, stderr=subprocess.DEVNULL)
            elif system == 'windows':
                output = subprocess.check_output(['powershell', '-Command', 'Get-PnpDevice -PresentOnly'], text=True, stderr=subprocess.DEVNULL)
            else:
                output = subprocess.check_output(['sh', '-lc', 'lsusb || true'], text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return None
        lowered = output.lower()
        if vendor in lowered or label in lowered:
            return {'label': 'FUJIFILM connecté via USB', 'raw': output[:4000]}
        return None
