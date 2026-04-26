from dataclasses import dataclass
import logging
import threading
import time

import simplepyble
from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BluetoothPrinterMonitorConfig:
    enabled: bool = False
    device_name: str = ""
    device_address: str = ""
    scan_interval_ms: int = 15000
    scan_duration_ms: int = 1500
    lost_after_misses: int = 3
    initial_grace_ms: int = 10000
    cooldown_after_busy_ms: int = 30000


class BluetoothPrinterMonitor(QObject):
    """Passive BLE presence monitor for an Instax printer.

    Important: this class never calls peripheral.connect(). It only scans BLE
    advertisements. During printing, Mini Link printers may stop advertising or
    report as non-connectable; that must not be interpreted as a lost printer.
    """

    connected = Signal(dict)
    disconnected = Signal()

    def __init__(self, config, is_busy_callback=None):
        super().__init__()
        self.config = config
        self.is_busy_callback = is_busy_callback
        self._connected = False
        self._adapter = None
        self._scan_running = False
        self._lock = threading.Lock()
        self._misses = 0
        self._started_at = time.monotonic()
        self._busy_until = 0
        self._last_payload = None

        self._timer = QTimer(self)
        self._timer.setInterval(max(1000, int(config.scan_interval_ms or 15000)))
        self._timer.timeout.connect(self._poll_once)

        logger.info(
            "init enabled=%s device_name=%s device_address=%s "
            "scan_interval_ms=%s scan_duration_ms=%s lost_after_misses=%s cooldown_after_busy_ms=%s",
            config.enabled, config.device_name, config.device_address,
            config.scan_interval_ms, config.scan_duration_ms,
            config.lost_after_misses, config.cooldown_after_busy_ms,
        )

    def start(self):
        logger.info("start()")
        self._poll_once()
        self._timer.start()

    def stop(self):
        logger.info("stop()")
        self._timer.stop()

    def _printer_is_busy(self):
        if not self.is_busy_callback:
            return False
        try:
            return bool(self.is_busy_callback())
        except Exception as exc:
            logger.warning("busy callback failed: %s", exc)
            return False

    def _poll_once(self):
        now = time.monotonic()

        if self._printer_is_busy():
            self._busy_until = now + max(
                0, int(self.config.cooldown_after_busy_ms or 0)
            ) / 1000.0
            self._misses = 0
            logger.debug("scan skipped: printer is busy")
            return

        if now < self._busy_until:
            self._misses = 0
            logger.debug("scan skipped: post-print cooldown")
            return

        if self._scan_running:
            logger.debug("scan skipped: already running")
            return

        thread = threading.Thread(
            target=self._scan_worker,
            name="BluetoothPrinterMonitorScan",
            daemon=True,
        )
        thread.start()

    def _scan_worker(self):
        with self._lock:
            self._scan_running = True
            payload = None
            try:
                payload = self._find_printer_payload()
            except Exception as exc:
                logger.error("scan failed: %s", exc)
            finally:
                self._scan_running = False

        self._update_connection_state(payload)

    def _find_printer_payload(self):
        adapters = simplepyble.Adapter.get_adapters()
        if not adapters:
            logger.warning("no Bluetooth adapters found")
            return None

        if self._adapter is None:
            self._adapter = adapters[0]
            logger.info("using adapter %s", self._adapter.identifier())

        duration_ms = max(500, int(self.config.scan_duration_ms or 1500))
        self._adapter.scan_for(duration_ms)

        for peripheral in self._adapter.scan_get_results():
            identifier = (peripheral.identifier() or "").strip()
            address = (peripheral.address() or "").strip()

            if not self._matches_printer(identifier, address):
                continue

            # While idle we prefer connectable advertisements, but after a
            # printer has already been seen, a matching non-connectable advert
            # is still proof that the printer is present.
            if not peripheral.is_connectable() and not self._connected:
                logger.debug("found non-connectable printer %s [%s]", identifier, address)
                continue

            logger.info("found printer %s [%s]", identifier, address)
            return {
                "label": identifier or "Instax printer",
                "name": identifier,
                "address": address,
            }

        return None

    def _matches_printer(self, identifier, address):
        target_name = (self.config.device_name or "").strip().upper()
        target_address = (self.config.device_address or "").strip().upper()
        found_name = identifier.upper()
        found_address = address.upper()

        if target_name and found_name.startswith(target_name):
            return True
        if target_address and found_address == target_address:
            return True
        if not target_name and not target_address:
            return found_name.startswith("INSTAX-") and found_name.endswith("(IOS)")
        return False

    def _inside_initial_grace(self):
        grace_seconds = max(0, int(self.config.initial_grace_ms or 0)) / 1000.0
        return (time.monotonic() - self._started_at) < grace_seconds

    def _update_connection_state(self, payload):
        if payload:
            self._misses = 0
            self._last_payload = payload
            if not self._connected:
                self._connected = True
                self.connected.emit(payload)
            return

        if not self._connected:
            return

        if self._printer_is_busy() or time.monotonic() < self._busy_until:
            self._misses = 0
            return

        if self._inside_initial_grace():
            self._misses = 0
            return

        self._misses += 1
        lost_after = max(1, int(self.config.lost_after_misses or 3))
        logger.info("printer not seen miss=%d/%d", self._misses, lost_after)

        if self._misses >= lost_after:
            self._connected = False
            self._misses = 0
            self.disconnected.emit()
