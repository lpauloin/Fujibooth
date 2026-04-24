"""
Bluetooth remote control service — Beauty-R1 (BLE HID keyboard, VID=0x0E05 PID=0x0A00).

The remote pairs as a BLE HID device. macOS treats its directional buttons
as mouse scroll/movement events (cursor moves, content scrolls).

We use the Python `hid` library (hidapi) to read raw HID reports directly.
Note: unlike the IOHIDManager seize approach, OS events are NOT suppressed —
the cursor may still move when directional buttons are pressed.

HID reports from Beauty-R1 (decoded from report descriptor):
  Report ID 3 (consumer): 5 bits — VolumeUp, VolumeDown, Power, Menu, extra
  Report ID 4 (mouse):    byte0 = 5 buttons + 3 padding, byte1 = wheel delta
  Report ID 5 (consumer): 2 bytes — 16-bit usage code (0x0000–0x03FF)

With hidapi, d.read() returns [report_id, byte0, byte1, ...].

Press each button and watch the "[HID] report id=X data=..." lines to identify
the mapping, then add entries to BEAUTY_R1_REPORT_MAP.
"""

import ctypes
import hid
import subprocess
import threading

from PySide6.QtCore import QObject, Qt, Signal

from ..models.remote import RemoteButton

# ------------------------------------------------------------------
# Keyboard fallback mapping (used when HID capture is not active)
# ------------------------------------------------------------------

DEFAULT_KEY_MAP = {
    Qt.Key.Key_VolumeUp.value:   RemoteButton.CAMERA,
    Qt.Key.Key_VolumeDown.value: RemoteButton.PHOTO,
    Qt.Key.Key_Up.value:         RemoteButton.UP,
    Qt.Key.Key_Down.value:       RemoteButton.DOWN,
    Qt.Key.Key_Left.value:       RemoteButton.LEFT,
    Qt.Key.Key_Right.value:      RemoteButton.RIGHT,
    Qt.Key.Key_Return.value:     RemoteButton.CENTER,
    Qt.Key.Key_Space.value:      RemoteButton.CENTER,
    Qt.Key.Key_Enter.value:      RemoteButton.CENTER,
}

QT_KEY_NAMES = {v.value: v.name for v in Qt.Key if isinstance(v.value, int)}

# ------------------------------------------------------------------
# Beauty-R1 HID report map
# Fill entries as you discover them by watching the "[HID] report" logs.
#
# Format: (report_id, byte_index, bit_mask) → RemoteButton
#   or    (report_id, "word", usage_code)   → RemoteButton  (16-bit, report ID 5)
# ------------------------------------------------------------------
BEAUTY_R1_REPORT_MAP = {
    # Report ID 3 — consumer control bits
    (3, 0, 0x01): RemoteButton.CAMERA,  # Volume Increment
    (3, 0, 0x02): RemoteButton.PHOTO,  # Volume Decrement
    # Report ID 4 — mouse buttons (byte 0, bits 0-4) and wheel (byte 1)
    # e.g. (4, 0, 0x01): RemoteButton.CENTER,  # mouse button 1
    # Report ID 5 — 16-bit consumer usage
    # e.g. (5, "word", 0x00E9): RemoteButton.CAMERA,
}


# ------------------------------------------------------------------
# HID device capture using the `hid` library (hidapi)
# ------------------------------------------------------------------


class HidDeviceCapture:
    """
    Opens a HID device with the `hid` library and reads raw reports in a thread.
    Automatically reconnects when the device is unplugged/re-paired.
    """

    def __init__(self, vendor_id, product_id, on_report):
        self._vid = vendor_id
        self._pid = product_id
        self._on_report = on_report
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="hid-capture", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        print(
            f"[HID] capture thread started "
            f"VID={self._vid:#06x} PID={self._pid:#06x}"
        )

        while not self._stop.is_set():
            # Wait for the device to appear (polls every 2 s)
            devices = hid.enumerate(self._vid, self._pid)
            if not devices:
                self._stop.wait(timeout=2.0)
                continue

            dev = hid.device()
            try:
                dev.open(self._vid, self._pid)
                dev.set_nonblocking(True)
                print(
                    f"[HID] device opened VID={self._vid:#06x} "
                    f"PID={self._pid:#06x} — reading reports"
                )

                while not self._stop.is_set():
                    # read() with timeout; returns [] on timeout, None on error
                    report = dev.read(64, timeout_ms=100)
                    if report:
                        report_id = report[0]
                        data = bytes(report[1:])
                        self._on_report(report_id, data)

            except OSError as exc:
                print(f"[HID] device error: {exc}")
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
                if not self._stop.is_set():
                    print("[HID] device lost — waiting for reconnect")

        print("[HID] capture stopped")


# ------------------------------------------------------------------
# RemoteControlService
# ------------------------------------------------------------------


class RemoteControlService(QObject):
    """
    Bridges the Beauty-R1 remote to RemoteButton signals.

    Primary mode: `hid` library polling thread (raw HID reports).
    Fallback: Qt keyPressEvent interception.
    """

    button_pressed = Signal(RemoteButton)
    hid_connected = Signal(str)
    hid_disconnected = Signal(str)

    def __init__(self, key_map=None, parent=None):
        super().__init__(parent)
        self._key_map = key_map or DEFAULT_KEY_MAP
        self._hid_capture = None
        self._hid_monitor_thread = None
        self._hid_stop = threading.Event()
        self._last_report = {}

    # ------------------------------------------------------------------
    # Primary: hid library capture
    # ------------------------------------------------------------------

    def start_hid_capture(self, vendor_id, product_id):
        self._hid_capture = HidDeviceCapture(vendor_id, product_id, self._on_hid_report)
        self._hid_capture.start()

    def _on_hid_report(self, report_id, data):
        hex_data = data.hex(" ") if data else "(empty)"
        print(
            f"[HID] report id={report_id} data={hex_data}  "
            f"bits={' '.join(f'{b:08b}' for b in data)}"
        )

        btn = self._decode_report(report_id, data)
        if btn is not None:
            print(f"[HID] -> {btn.name}")
            self.button_pressed.emit(btn)

    def _decode_report(self, report_id, data):
        if not data:
            return None

        if report_id == 3:
            byte0 = data[0]
            for mask in (0x01, 0x02, 0x04, 0x08, 0x10):
                if byte0 & mask:
                    btn = BEAUTY_R1_REPORT_MAP.get((3, 0, mask))
                    if btn:
                        key = (report_id, mask)
                        if not self._last_report.get(key):
                            self._last_report[key] = True
                            return btn
                else:
                    self._last_report[(report_id, mask)] = False

        elif report_id == 4:
            buttons = data[0] & 0x1F
            wheel = ctypes.c_int8(data[1] if len(data) > 1 else 0).value
            if buttons:
                for bit in range(5):
                    mask = 1 << bit
                    if buttons & mask:
                        btn = BEAUTY_R1_REPORT_MAP.get((4, 0, mask))
                        if btn:
                            key = (report_id, mask)
                            if not self._last_report.get(key):
                                self._last_report[key] = True
                                return btn
                    else:
                        self._last_report[(report_id, 1 << bit)] = False
            if wheel:
                btn = BEAUTY_R1_REPORT_MAP.get(
                    (4, "wheel_up") if wheel > 0 else (4, "wheel_down")
                )
                if btn:
                    return btn

        elif report_id == 5 and len(data) >= 2:
            code = data[0] | (data[1] << 8)
            if code:
                btn = BEAUTY_R1_REPORT_MAP.get((5, "word", code))
                if btn:
                    key = (report_id, code)
                    if not self._last_report.get(key):
                        self._last_report[key] = True
                        return btn
            else:
                for k in list(self._last_report):
                    if isinstance(k, tuple) and k[0] == 5:
                        self._last_report[k] = False

        return None

    # ------------------------------------------------------------------
    # Fallback: Qt keyboard interception
    # ------------------------------------------------------------------

    def handle_key(self, key):
        key_name = QT_KEY_NAMES.get(key, str(key))
        button = self._key_map.get(key)
        if button is not None:
            print(f"[REMOTE] key={key_name} ({key}) -> {button.name}")
            self.button_pressed.emit(button)
            return True
        print(f"[REMOTE] unmapped key={key_name} ({key})")
        return False

    # ------------------------------------------------------------------
    # HID connection monitor (polls macOS every 5 s)
    # ------------------------------------------------------------------

    def start_hid_monitor(self, device_name, poll_interval=5.0):
        if self._hid_monitor_thread and self._hid_monitor_thread.is_alive():
            return
        self._hid_stop.clear()
        self._hid_monitor_thread = threading.Thread(
            target=self._hid_monitor_worker,
            args=(device_name, poll_interval),
            name="remote-hid-monitor",
            daemon=True,
        )
        self._hid_monitor_thread.start()
        print(f"[REMOTE-HID] monitor started for '{device_name}'")

    def _hid_monitor_worker(self, device_name, interval):
        last = None
        while not self._hid_stop.is_set():
            connected = _is_hid_device_connected(device_name)
            if connected != last:
                last = connected
                if connected:
                    print(f"[REMOTE-HID] '{device_name}' connected")
                    self.hid_connected.emit(device_name)
                else:
                    print(f"[REMOTE-HID] '{device_name}' disconnected")
                    self.hid_disconnected.emit(device_name)
            self._hid_stop.wait(timeout=interval)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self):
        self._hid_stop.set()
        if self._hid_capture:
            self._hid_capture.stop()


# ------------------------------------------------------------------
# macOS helpers
# ------------------------------------------------------------------


def _is_hid_device_connected(device_name):
    try:
        out = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-k", "Product"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        return f'"Product" = "{device_name}"' in out
    except Exception as exc:
        print(f"[REMOTE-HID] connection check failed: {exc}")
    return False
