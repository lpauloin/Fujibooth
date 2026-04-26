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

Press each button and watch the "report id=X data=..." lines to identify
the mapping, then add entries to BEAUTY_R1_REPORT_MAP.
"""

import ctypes
import hid
import subprocess
import threading
from enum import Enum, auto
import logging

from PySide6.QtCore import QObject, Qt, Signal

from ..models.remote import RemoteButton

logger = logging.getLogger(__name__)


class BtnState(Enum):
    IDLE = auto()
    PENDING_CENTER = auto()
    BUTTON_FIRED = auto()


# Delay during which a report ID 5 direction can cancel the pending CENTER.
#
# Beauty-R1 seems to emit report ID 4 first, then report ID 5 for directional
# buttons. Therefore CENTER cannot be emitted immediately on report ID 4.
CENTER_GRACE_DELAY = 0.15

# Safety delay used after a button has fired.
#
# Normally the state returns to IDLE on the report ID 4 release edge. This timer
# prevents the state machine from getting stuck if the remote does not send a
# clean release report.
BUTTON_RESET_DELAY = 0.35


# ------------------------------------------------------------------
# Keyboard fallback mapping (used when HID capture is not active)
# ------------------------------------------------------------------

DEFAULT_KEY_MAP = {
    Qt.Key.Key_VolumeDown.value: RemoteButton.PHOTO,
    Qt.Key.Key_Up.value: RemoteButton.UP,
    Qt.Key.Key_Down.value: RemoteButton.DOWN,
    Qt.Key.Key_Left.value: RemoteButton.LEFT,
    Qt.Key.Key_Right.value: RemoteButton.RIGHT,
    Qt.Key.Key_Return.value: RemoteButton.CENTER,
    Qt.Key.Key_Space.value: RemoteButton.CENTER,
    Qt.Key.Key_Enter.value: RemoteButton.CENTER,
}

QT_KEY_NAMES = {v.value: v.name for v in Qt.Key if isinstance(v.value, int)}


# ------------------------------------------------------------------
# Beauty-R1 HID report map
#
# Format:
#   (report_id, byte_index, bit_mask) → RemoteButton
#   (report_id, "word", usage_code)   → RemoteButton
#   (report_id, "wheel_up")           → RemoteButton
#   (report_id, "wheel_down")         → RemoteButton
# ------------------------------------------------------------------

BEAUTY_R1_REPORT_MAP = {
    # Report ID 3 — consumer control bits
    (3, 0, 0x02): RemoteButton.PHOTO,  # Volume Decrement
    # Report ID 5 — directional codes.
    #
    # CENTER is deliberately NOT mapped here.
    # CENTER is inferred from report ID 4 when no direction arrives during the
    # grace window.
    (5, "word", 0xC000): RemoteButton.UP,
    (5, "word", 0xE000): RemoteButton.DOWN,
    (5, "word", 0x0FD8): RemoteButton.RIGHT,
    (5, "word", 0x0027): RemoteButton.LEFT,
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
            target=self._run,
            name="hid-capture",
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _log_all_hid_devices(self):
        all_devices = hid.enumerate(0, 0)
        if not all_devices:
            logger.debug("no HID devices found")
            return

        logger.debug(f"{len(all_devices)} HID device(s) visible:")
        for d in all_devices:
            logger.debug(
                f"  VID={d['vendor_id']:#06x} PID={d['product_id']:#06x}"
                f"  {d.get('manufacturer_string', '')} / {d.get('product_string', '')}"
            )

    def _run(self):
        logger.debug(
            f"capture thread started — looking for "
            f"VID={self._vid:#06x} PID={self._pid:#06x}"
        )
        self._log_all_hid_devices()

        while not self._stop.is_set():
            devices = hid.enumerate(self._vid, self._pid)
            if not devices:
                self._stop.wait(timeout=2.0)
                continue

            try:
                dev = hid.Device(self._vid, self._pid)
            except OSError as exc:
                logger.warning(f"open failed: {exc}")
                self._stop.wait(timeout=2.0)
                continue

            logger.debug(
                f"device opened VID={self._vid:#06x} "
                f"PID={self._pid:#06x} — reading reports"
            )
            logger.debug(
                "Press each button — copy the [HID-MAP] lines into "
                "BEAUTY_R1_REPORT_MAP"
            )

            try:
                while not self._stop.is_set():
                    report = dev.read(64, 100)
                    if report:
                        report_id = report[0]
                        data = bytes(report[1:])
                        self._on_report(report_id, data)

            except OSError as exc:
                logger.warning(f"device error: {exc}")

            finally:
                try:
                    dev.close()
                except Exception:
                    pass

                if not self._stop.is_set():
                    logger.debug("device lost — waiting for reconnect")

        logger.debug("capture stopped")


# ------------------------------------------------------------------
# RemoteControlService
# ------------------------------------------------------------------


class RemoteControlService(QObject):
    """
    Bridges the Beauty-R1 remote to RemoteButton signals.

    Primary mode: `hid` library polling thread, using raw HID reports.
    Fallback: Qt keyPressEvent interception.

    Button state machine for report ID 4 / report ID 5:

        IDLE
          └─ report ID 4 button down
                → PENDING_CENTER

        PENDING_CENTER
          ├─ report ID 5 direction arrives before timeout
          │     → emit direction
          │     → BUTTON_FIRED
          │
          └─ no report ID 5 before timeout
                → emit CENTER
                → BUTTON_FIRED

        BUTTON_FIRED
          ├─ report ID 4 release
          │     → IDLE
          │
          └─ reset timeout
                → IDLE, but if the physical button still appears down,
                  suppress new press detection until a release is observed.

    This guarantees that one physical action emits at most one logical
    RemoteButton.
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

        # Report ID 3 dedup state.
        self._consumer_bits = {}

        # Report ID 4 / 5 state machine.
        self._btn_state = BtnState.IDLE
        self._btn_lock = threading.RLock()
        self._center_timer = None
        self._reset_timer = None

        # Last known physical state of report ID 4 button 1.
        self._mouse_btn1_down = False

        # Used when the reset timer fires while the button still looks pressed.
        # Without this, a repeated "down" report could be interpreted as a new
        # click and emit CENTER repeatedly while the user is holding the button.
        self._suppress_until_release = False

    # ------------------------------------------------------------------
    # Primary: hid library capture
    # ------------------------------------------------------------------

    def start_hid_capture(self, vendor_id, product_id):
        self._hid_capture = HidDeviceCapture(
            vendor_id,
            product_id,
            self._on_hid_report,
        )
        self._hid_capture.start()

    def _on_hid_report(self, report_id, data):
        hex_data = data.hex(" ") if data else "(empty)"
        logger.debug(
            f"report id={report_id}  data={hex_data}  "
            f"bits={' '.join(f'{b:08b}' for b in data)}"
        )

        self._print_mapping_hints(report_id, data)

        btn = self._decode_report(report_id, data)
        if btn is not None:
            logger.debug(f"-> {btn.name}")
            self.button_pressed.emit(btn)

    def _print_mapping_hints(self, report_id, data):
        buttons_label = [b.name for b in RemoteButton]
        hint = f"  # RemoteButton.{'|'.join(buttons_label)}"

        if report_id == 3 and data:
            byte0 = data[0]
            for mask in (0x01, 0x02, 0x04, 0x08, 0x10):
                if byte0 & mask:
                    key = (3, 0, mask)
                    mapped = BEAUTY_R1_REPORT_MAP.get(key)
                    if mapped:
                        logger.debug(
                            f"  (3, 0, {mask:#04x}): "
                            f"RemoteButton.{mapped.name}  ✓ mapped"
                        )
                    else:
                        logger.debug(
                            f"  (3, 0, {mask:#04x}): "
                            f"RemoteButton.???{hint}"
                        )

        elif report_id == 4 and data:
            buttons = data[0] & 0x1F
            wheel = ctypes.c_int8(data[1] if len(data) > 1 else 0).value

            for bit in range(5):
                mask = 1 << bit
                if buttons & mask:
                    key = (4, 0, mask)
                    mapped = BEAUTY_R1_REPORT_MAP.get(key)
                    if mapped:
                        logger.debug(
                            f"  (4, 0, {mask:#04x}): "
                            f"RemoteButton.{mapped.name}  ✓ mapped"
                        )
                    else:
                        logger.debug(
                            f"  (4, 0, {mask:#04x}): "
                            f"RemoteButton.???{hint}"
                        )

            if wheel:
                direction = "wheel_up" if wheel > 0 else "wheel_down"
                key = (4, direction)
                mapped = BEAUTY_R1_REPORT_MAP.get(key)
                if mapped:
                    logger.debug(
                        f'[HID-MAP]   (4, "{direction}"): '
                        f"RemoteButton.{mapped.name}  ✓ mapped"
                    )
                else:
                    logger.debug(
                        f'[HID-MAP]   (4, "{direction}"): '
                        f"RemoteButton.???{hint}  (delta={wheel})"
                    )

        elif report_id == 5 and len(data) >= 2:
            code = data[0] | (data[1] << 8)
            if code:
                key = (5, "word", code)
                mapped = BEAUTY_R1_REPORT_MAP.get(key)
                if mapped:
                    logger.debug(
                        f'[HID-MAP]   (5, "word", {code:#06x}): '
                        f"RemoteButton.{mapped.name}  ✓ mapped"
                    )
                else:
                    logger.debug(
                        f'[HID-MAP]   (5, "word", {code:#06x}): '
                        f"RemoteButton.???{hint}"
                    )

    def _decode_report(self, report_id, data):
        if not data:
            return None

        if report_id == 3:
            return self._decode_consumer_report(data)

        if report_id == 4:
            return self._decode_mouse_report(data)

        if report_id == 5:
            return self._decode_direction_report(data)

        return None

    def _decode_consumer_report(self, data):
        """
        Report ID 3: consumer control bits.

        PHOTO lives here. We deduplicate press/release because this report can
        be repeated while the button is held.
        """
        byte0 = data[0]

        for mask in (0x01, 0x02, 0x04, 0x08, 0x10):
            is_pressed = bool(byte0 & mask)
            was_pressed = self._consumer_bits.get(mask, False)

            if is_pressed and not was_pressed:
                self._consumer_bits[mask] = True
                return BEAUTY_R1_REPORT_MAP.get((3, 0, mask))

            if not is_pressed:
                self._consumer_bits[mask] = False

        return None

    def _decode_mouse_report(self, data):
        """
        Report ID 4: mouse-style report.

        We use button 1 as the trigger for a logical action, but we do not know
        immediately whether the action is CENTER or a direction.

        Directional buttons appear as:
          report ID 4 button event
          then report ID 5 direction usage code

        CENTER appears as:
          report ID 4 button event
          no following report ID 5 direction usage code

        Therefore report ID 4 starts a pending CENTER window.
        """
        buttons = data[0] & 0x1F
        btn1_down = bool(buttons & 0x01)

        self._handle_mouse_button_transition(btn1_down)

        wheel = ctypes.c_int8(data[1] if len(data) > 1 else 0).value
        if wheel:
            key = (4, "wheel_up") if wheel > 0 else (4, "wheel_down")
            return BEAUTY_R1_REPORT_MAP.get(key)

        return None

    def _decode_direction_report(self, data):
        """
        Report ID 5: 16-bit direction usage code.

        A direction is accepted only while CENTER is pending.

        Once a direction has fired, repeated report ID 5 messages are ignored
        until the state machine resets.
        """
        if len(data) < 2:
            return None

        code = data[0] | (data[1] << 8)
        if not code:
            return None

        btn = BEAUTY_R1_REPORT_MAP.get((5, "word", code))
        if btn is None:
            return None

        with self._btn_lock:
            if self._btn_state is not BtnState.PENDING_CENTER:
                logger.debug(
                    f"ignored direction {btn.name} "
                    f"while state={self._btn_state.name}"
                )
                return None

            self._cancel_center_timer_locked()
            self._btn_state = BtnState.BUTTON_FIRED
            logger.debug(f"PENDING_CENTER → BUTTON_FIRED ({btn.name})")

            self._arm_reset_timer_locked()

        return btn

    # ------------------------------------------------------------------
    # Button state machine
    # ------------------------------------------------------------------

    def _handle_mouse_button_transition(self, btn1_down):
        """
        Handles physical button transitions from report ID 4.

        We react to edges, not repeated reports.
        """
        with self._btn_lock:
            previous = self._mouse_btn1_down

            if btn1_down == previous:
                return

            self._mouse_btn1_down = btn1_down

            if btn1_down:
                self._on_mouse_button_down_locked()
            else:
                self._on_mouse_button_up_locked()

    def _on_mouse_button_down_locked(self):
        if self._suppress_until_release:
            logger.debug("button down ignored until release")
            return

        if self._btn_state is not BtnState.IDLE:
            logger.debug(
                f"button down ignored while state={self._btn_state.name}"
            )
            return

        self._btn_state = BtnState.PENDING_CENTER
        logger.debug("IDLE → PENDING_CENTER")

        self._cancel_reset_timer_locked()
        self._arm_center_timer_locked()

    def _on_mouse_button_up_locked(self):
        if self._suppress_until_release:
            self._suppress_until_release = False
            logger.debug("release observed — suppression cleared")

        if self._btn_state is BtnState.PENDING_CENTER:
            # Do not emit CENTER immediately on release.
            #
            # On this device, report ID 5 direction codes may arrive just after
            # the report ID 4 click/release sequence. The center timer remains
            # the arbiter.
            logger.debug("release while PENDING_CENTER — waiting for grace timeout")
            return

        if self._btn_state is BtnState.BUTTON_FIRED:
            self._reset_button_state_locked("release after fired button")
            return

        logger.debug("release ignored while IDLE")

    def _arm_center_timer_locked(self):
        self._cancel_center_timer_locked()

        self._center_timer = threading.Timer(
            CENTER_GRACE_DELAY,
            self._on_center_timeout,
        )
        self._center_timer.daemon = True
        self._center_timer.start()

    def _cancel_center_timer_locked(self):
        if self._center_timer:
            self._center_timer.cancel()
            self._center_timer = None

    def _on_center_timeout(self):
        with self._btn_lock:
            self._center_timer = None

            if self._btn_state is not BtnState.PENDING_CENTER:
                logger.debug(
                    f"center timeout ignored "
                    f"while state={self._btn_state.name}"
                )
                return

            self._btn_state = BtnState.BUTTON_FIRED
            logger.debug("PENDING_CENTER → BUTTON_FIRED (CENTER)")

            self._arm_reset_timer_locked()

        logger.debug("-> CENTER")
        self.button_pressed.emit(RemoteButton.CENTER)

    def _arm_reset_timer_locked(self):
        self._cancel_reset_timer_locked()

        self._reset_timer = threading.Timer(
            BUTTON_RESET_DELAY,
            self._on_reset_timeout,
        )
        self._reset_timer.daemon = True
        self._reset_timer.start()

    def _cancel_reset_timer_locked(self):
        if self._reset_timer:
            self._reset_timer.cancel()
            self._reset_timer = None

    def _on_reset_timeout(self):
        with self._btn_lock:
            if self._btn_state is BtnState.IDLE:
                return

            old_state = self._btn_state
            self._cancel_center_timer_locked()
            self._reset_timer = None
            self._btn_state = BtnState.IDLE

            # Important:
            #
            # If the physical button still appears down, do NOT pretend it is
            # released. Otherwise the next repeated down report could be seen
            # as a new press and emit CENTER again.
            #
            # Instead, suppress new down handling until a real release edge is
            # observed.
            if self._mouse_btn1_down:
                self._suppress_until_release = True

            logger.debug(f"{old_state.name} → IDLE (reset timeout)")

    def _reset_button_state_locked(self, reason):
        self._cancel_center_timer_locked()
        self._cancel_reset_timer_locked()

        old_state = self._btn_state
        self._btn_state = BtnState.IDLE

        logger.debug(f"{old_state.name} → IDLE ({reason})")

    # ------------------------------------------------------------------
    # Fallback: Qt keyboard interception
    # ------------------------------------------------------------------

    def handle_key(self, key):
        key_name = QT_KEY_NAMES.get(key, str(key))
        button = self._key_map.get(key)

        if button is not None:
            logger.debug(f"key={key_name} ({key}) -> {button.name}")
            self.button_pressed.emit(button)
            return True

        logger.debug(f"unmapped key={key_name} ({key})")
        return False

    # ------------------------------------------------------------------
    # HID connection monitor: polls macOS every N seconds
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

        logger.info(f"monitor started for '{device_name}'")

    def _hid_monitor_worker(self, device_name, interval):
        last = None

        while not self._hid_stop.is_set():
            connected = _is_hid_device_connected(device_name)

            if connected != last:
                last = connected

                if connected:
                    logger.info(f"'{device_name}' connected")
                    self.hid_connected.emit(device_name)
                else:
                    logger.info(f"'{device_name}' disconnected")
                    self.hid_disconnected.emit(device_name)

            self._hid_stop.wait(timeout=interval)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def stop(self):
        with self._btn_lock:
            self._cancel_center_timer_locked()
            self._cancel_reset_timer_locked()
            self._btn_state = BtnState.IDLE
            self._mouse_btn1_down = False
            self._suppress_until_release = False

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
        logger.warning(f"connection check failed: {exc}")

    return False
