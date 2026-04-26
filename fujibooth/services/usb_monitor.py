import ctypes
import ctypes.util
import threading
import time
from dataclasses import dataclass
import logging

import usb.core
import usb.backend.libusb1
from PySide6.QtCore import QObject, QTimer, Signal

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class USBMonitorConfig:
    vendor_id: str
    product_ids: list[str]
    camera_name_contains: str


class USBMonitor(QObject):
    connected = Signal(dict)
    disconnected = Signal()

    LIBUSB_HOTPLUG_EVENT_DEVICE_ARRIVED = 0x01
    LIBUSB_HOTPLUG_EVENT_DEVICE_LEFT = 0x02
    LIBUSB_HOTPLUG_ENUMERATE = 0x01
    LIBUSB_HOTPLUG_MATCH_ANY = -1
    LIBUSB_CAP_HAS_HOTPLUG = 1

    def __init__(self, config, poll_interval_ms=1500):
        super().__init__()
        self.config = config

        self._connected = False
        self._backend = usb.backend.libusb1.get_backend()
        self._libusb = None
        self._ctx = None
        self._callback_handle = ctypes.c_int()
        self._event_thread = None
        self._event_thread_stop = threading.Event()
        self._hotplug_active = False

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self._poll_once)

        logger.info(
            "init "
            f"vendor_id={config.vendor_id} "
            f"product_ids={config.product_ids} "
            f"camera_name_contains={config.camera_name_contains}"
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        logger.info("start()")

        if self._try_start_libusb_hotplug():
            logger.info("hotplug mode enabled")
            self._emit_initial_state()
            return

        logger.info("hotplug unavailable -> fallback polling")
        self._poll_once()
        self._poll_timer.start()

    def stop(self):
        logger.info("stop()")

        self._poll_timer.stop()
        self._stop_libusb_hotplug()

    # ------------------------------------------------------------------
    # normalization
    # ------------------------------------------------------------------

    def _normalize_hex(self, value):
        if value is None:
            return None
        if isinstance(value, int):
            return value

        text = str(value).strip().lower()
        if text.startswith("0x"):
            text = text[2:]
        return int(text, 16)

    @property
    def _vendor_id(self):
        return self._normalize_hex(self.config.vendor_id)

    @property
    def _product_ids(self):
        return {
            value
            for value in (self._normalize_hex(p) for p in self.config.product_ids)
            if value is not None
        }

    @property
    def _name_hint(self):
        return (self.config.camera_name_contains or "").strip().lower()

    # ------------------------------------------------------------------
    # polling mode
    # ------------------------------------------------------------------

    def _poll_once(self):
        logger.debug("_poll_once()")
        payload = self._find_camera_payload()
        self._update_connection_state(payload)

    def _emit_initial_state(self):
        logger.debug("_emit_initial_state()")
        payload = self._find_camera_payload()
        self._update_connection_state(payload)

    # ------------------------------------------------------------------
    # hotplug mode
    # ------------------------------------------------------------------

    def _try_start_libusb_hotplug(self):
        try:
            libusb_path = ctypes.util.find_library("usb-1.0")
            logger.debug(f"libusb path={libusb_path}")
            if not libusb_path:
                return False

            self._libusb = ctypes.CDLL(libusb_path)

            if self._backend is None:
                logger.debug("no PyUSB libusb backend")
                return False

            self._libusb.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            self._libusb.libusb_init.restype = ctypes.c_int

            self._libusb.libusb_exit.argtypes = [ctypes.c_void_p]
            self._libusb.libusb_exit.restype = None

            self._libusb.libusb_has_capability.argtypes = [ctypes.c_uint32]
            self._libusb.libusb_has_capability.restype = ctypes.c_int

            class TimeVal(ctypes.Structure):
                _fields_ = [
                    ("tv_sec", ctypes.c_long),
                    ("tv_usec", ctypes.c_long),
                ]

            self._TimeVal = TimeVal
            self._libusb.libusb_handle_events_timeout_completed.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(TimeVal),
                ctypes.POINTER(ctypes.c_int),
            ]
            self._libusb.libusb_handle_events_timeout_completed.restype = ctypes.c_int

            self._HotplugCallback = ctypes.CFUNCTYPE(
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
            )

            self._libusb.libusb_hotplug_register_callback.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                self._HotplugCallback,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_int),
            ]
            self._libusb.libusb_hotplug_register_callback.restype = ctypes.c_int

            self._libusb.libusb_hotplug_deregister_callback.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            self._libusb.libusb_hotplug_deregister_callback.restype = None

            self._ctx = ctypes.c_void_p()
            rc = self._libusb.libusb_init(ctypes.byref(self._ctx))
            logger.debug(f"libusb_init rc={rc}")
            if rc != 0:
                self._ctx = None
                return False

            has_hotplug = self._libusb.libusb_has_capability(
                self.LIBUSB_CAP_HAS_HOTPLUG
            )
            logger.debug(f"libusb_has_capability(HOTPLUG)={has_hotplug}")
            if not has_hotplug:
                self._libusb.libusb_exit(self._ctx)
                self._ctx = None
                return False

            self._hotplug_callback = self._HotplugCallback(
                self._on_libusb_hotplug_event
            )

            vendor_id = (
                self._vendor_id
                if self._vendor_id is not None
                else self.LIBUSB_HOTPLUG_MATCH_ANY
            )
            product_id = self.LIBUSB_HOTPLUG_MATCH_ANY
            dev_class = self.LIBUSB_HOTPLUG_MATCH_ANY

            rc = self._libusb.libusb_hotplug_register_callback(
                self._ctx,
                self.LIBUSB_HOTPLUG_EVENT_DEVICE_ARRIVED
                | self.LIBUSB_HOTPLUG_EVENT_DEVICE_LEFT,
                self.LIBUSB_HOTPLUG_ENUMERATE,
                vendor_id,
                product_id,
                dev_class,
                self._hotplug_callback,
                None,
                ctypes.byref(self._callback_handle),
            )
            logger.debug(f"libusb_hotplug_register_callback rc={rc}")
            if rc != 0:
                self._libusb.libusb_exit(self._ctx)
                self._ctx = None
                return False

            self._event_thread_stop.clear()
            self._event_thread = threading.Thread(
                target=self._event_loop,
                name="usb-monitor-libusb",
                daemon=True,
            )
            self._event_thread.start()
            self._hotplug_active = True
            return True

        except Exception as exc:
            logger.error(f"_try_start_libusb_hotplug failed: {exc}")
            self._stop_libusb_hotplug()
            return False

    def _stop_libusb_hotplug(self):
        if not self._hotplug_active and self._ctx is None:
            return

        logger.debug("_stop_libusb_hotplug()")
        self._event_thread_stop.set()

        if self._event_thread is not None:
            self._event_thread.join(timeout=2.0)
            self._event_thread = None

        try:
            if self._libusb is not None and self._ctx is not None:
                try:
                    self._libusb.libusb_hotplug_deregister_callback(
                        self._ctx,
                        self._callback_handle.value,
                    )
                except Exception as exc:
                    logger.warning(f"hotplug deregister ignore: {exc}")

                self._libusb.libusb_exit(self._ctx)
        except Exception as exc:
            logger.warning(f"libusb shutdown ignore: {exc}")
        finally:
            self._ctx = None
            self._libusb = None
            self._hotplug_active = False

    def _event_loop(self):
        logger.debug("_event_loop start")
        completed = ctypes.c_int(0)

        while not self._event_thread_stop.is_set():
            try:
                timeout = self._TimeVal(0, 500_000)
                rc = self._libusb.libusb_handle_events_timeout_completed(
                    self._ctx,
                    ctypes.byref(timeout),
                    ctypes.byref(completed),
                )
                if rc != 0:
                    logger.warning(f"libusb_handle_events rc={rc}")
                    time.sleep(0.2)
            except Exception as exc:
                logger.warning(f"event loop error: {exc}")
                time.sleep(0.2)

        logger.debug("_event_loop end")

    def _on_libusb_hotplug_event(self, ctx, device, event, user_data):
        logger.debug(f"hotplug event={event}")
        payload = self._find_camera_payload()
        self._update_connection_state(payload)
        return 0

    # ------------------------------------------------------------------
    # device matching
    # ------------------------------------------------------------------

    def _find_camera_payload(self):
        logger.debug("_find_camera_payload()")

        backend = self._backend
        if backend is None:
            logger.debug("no backend available")
            return None

        expected_vendor = self._vendor_id
        expected_products = self._product_ids
        expected_name = self._name_hint

        try:
            devices = usb.core.find(find_all=True, backend=backend)
        except Exception as exc:
            logger.warning(f"usb.core.find error: {exc}")
            return None

        for dev in devices:
            try:
                manufacturer = self._safe_get_string(
                    dev, getattr(dev, "iManufacturer", 0)
                )
                product = self._safe_get_string(dev, getattr(dev, "iProduct", 0))
                serial = self._safe_get_string(dev, getattr(dev, "iSerialNumber", 0))

                combined_name = " ".join(
                    part for part in [manufacturer, product] if part
                ).strip()
                lowered_name = combined_name.lower()

                vendor_ok = expected_vendor is None or dev.idVendor == expected_vendor
                product_ok = not expected_products or dev.idProduct in expected_products
                name_ok = not expected_name or expected_name in lowered_name

                logger.debug(
                    "candidate "
                    f"vid=0x{dev.idVendor:04x} pid=0x{dev.idProduct:04x} "
                    f"manufacturer={manufacturer!r} product={product!r} serial={serial!r} "
                    f"vendor_ok={vendor_ok} product_ok={product_ok} name_ok={name_ok}"
                )

                # 1) strongest match: vendor + product + name
                if vendor_ok and product_ok and name_ok:
                    label = manufacturer or product or "FUJIFILM"
                    logger.info("accepting device on full match")
                    return {
                        "label": f"{label} connecté",
                        "serial": serial or "UNKNOWN",
                        "vendor_id": f"{dev.idVendor:04x}",
                        "product_id": f"{dev.idProduct:04x}",
                    }

                # 2) practical match: vendor + product
                # Important for macOS where the camera often appears as "USB PTP Camera"
                if vendor_ok and product_ok:
                    label = manufacturer or product or "FUJIFILM"
                    logger.info("accepting device on vendor+product match")
                    return {
                        "label": f"{label} connecté",
                        "serial": serial or "UNKNOWN",
                        "vendor_id": f"{dev.idVendor:04x}",
                        "product_id": f"{dev.idProduct:04x}",
                    }

                # 3) fallback: vendor only, only if there is no configured product filter
                if vendor_ok and not expected_products:
                    label = manufacturer or product or "FUJIFILM"
                    logger.info("accepting device on vendor-only fallback")
                    return {
                        "label": f"{label} connecté",
                        "serial": serial or "UNKNOWN",
                        "vendor_id": f"{dev.idVendor:04x}",
                        "product_id": f"{dev.idProduct:04x}",
                    }

            except Exception as exc:
                logger.warning(f"candidate parse error: {exc}")

        return None

    def _safe_get_string(self, dev, index):
        if not index:
            return ""
        try:
            value = usb.util.get_string(dev, index)
            return value or ""
        except Exception as exc:
            logger.warning(f"get_string failed index={index}: {exc}")
            return ""

    # ------------------------------------------------------------------
    # state transition
    # ------------------------------------------------------------------

    def _update_connection_state(self, payload):
        now_connected = payload is not None

        logger.debug(
            f"update_connection_state "
            f"now_connected={now_connected} previous={self._connected} payload={payload}"
        )

        if now_connected and not self._connected:
            self._connected = True
            self.connected.emit(payload)

        elif not now_connected and self._connected:
            self._connected = False
            self.disconnected.emit()
