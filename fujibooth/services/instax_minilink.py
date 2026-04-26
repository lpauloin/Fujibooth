from dataclasses import dataclass
from enum import Enum
import logging
from math import ceil
from struct import pack, unpack_from
from time import sleep, monotonic
from io import BytesIO

import simplepyble
from PIL import Image, ImageEnhance, ImageOps

logger = logging.getLogger(__name__)


class InstaxEvent(Enum):
    SUPPORT_FUNCTION_INFO = (0, 2)
    PRINT_IMAGE_DOWNLOAD_START = (16, 0)
    PRINT_IMAGE_DOWNLOAD_DATA = (16, 1)
    PRINT_IMAGE_DOWNLOAD_END = (16, 2)
    PRINT_IMAGE_DOWNLOAD_CANCEL = (16, 3)
    PRINT_IMAGE = (16, 128)


class InstaxInfo(Enum):
    IMAGE_SUPPORT_INFO = 0
    BATTERY_INFO = 1
    PRINTER_FUNCTION_INFO = 2


@dataclass(slots=True)
class InstaxPrinterStatus:
    photos_left: int = -1
    battery_percentage: int = -1
    is_charging: bool = False


class InstaxMiniLinkClient:
    """Minimal Instax Mini Link BLE client."""

    SERVICE_UUID = "70954782-2d83-473d-9e5f-81e1d02d5273"
    WRITE_CHAR_UUID = "70954783-2d83-473d-9e5f-81e1d02d5273"
    NOTIFY_CHAR_UUID = "70954784-2d83-473d-9e5f-81e1d02d5273"

    IMAGE_SIZE = (600, 800)
    CHUNK_SIZE = 900
    MAX_JPEG_KB = 105
    BLE_WRITE_SIZE = 182

    def __init__(
        self,
        device_name="",
        device_address="",
        auto_rotate_landscape=True,
        image_fit="cover",
        autocontrast=True,
        color_boost=1.25,
        contrast_boost=1.15,
        brightness_boost=1.05,
        sharpness_boost=1.05,
    ):
        self.device_name = (device_name or "").strip().upper()
        self.device_address = (device_address or "").strip().upper()
        self.auto_rotate_landscape = bool(auto_rotate_landscape)
        self.image_fit = (image_fit or "cover").strip().lower()
        self.autocontrast = bool(autocontrast)
        self.color_boost = float(color_boost or 1.0)
        self.contrast_boost = float(contrast_boost or 1.0)
        self.brightness_boost = float(brightness_boost or 1.0)
        self.sharpness_boost = float(sharpness_boost or 1.0)
        self.peripheral = None
        self.status = InstaxPrinterStatus()
        self._waiting_for_response = False
        self._cancelled = False
        self._packets_for_printing = []

        adapters = simplepyble.Adapter.get_adapters()
        if not adapters:
            raise RuntimeError("No Bluetooth adapter found")
        if len(adapters) > 1:
            logger.debug(
                "found multiple Bluetooth adapters, using first: %s",
                adapters[0].identifier(),
            )
        self.adapter = adapters[0]

    def connect(self, timeout_seconds=10):
        self.peripheral = self._find_printer(timeout_seconds=timeout_seconds)
        if self.peripheral is None:
            raise RuntimeError("Instax Mini Link printer not found")

        logger.debug(
            "connecting to %s [%s]",
            self.peripheral.identifier(),
            self.peripheral.address(),
        )
        self.peripheral.connect()
        if not self.peripheral.is_connected():
            raise RuntimeError("Bluetooth connection failed")

        self.peripheral.notify(
            self.SERVICE_UUID,
            self.NOTIFY_CHAR_UUID,
            self._notification_handler,
        )
        self._read_printer_status()
        sleep(0.5)

    def disconnect(self):
        if self.peripheral and self.peripheral.is_connected():
            self.peripheral.disconnect()

    def print_image(self, image_path):
        if self.status.photos_left == 0:
            raise RuntimeError("Instax printer has no film left")

        img_data = self._image_to_jpeg_bytes(image_path)
        self._packets_for_printing = [
            self._create_packet(
                InstaxEvent.PRINT_IMAGE_DOWNLOAD_START,
                b"\x02\x00\x00\x00" + pack(">I", len(img_data)),
            )
        ]

        chunks = [
            img_data[i : i + self.CHUNK_SIZE]
            for i in range(0, len(img_data), self.CHUNK_SIZE)
        ]
        if chunks and len(chunks[-1]) < self.CHUNK_SIZE:
            chunks[-1] += bytes(self.CHUNK_SIZE - len(chunks[-1]))

        for index, chunk in enumerate(chunks):
            self._packets_for_printing.append(
                self._create_packet(
                    InstaxEvent.PRINT_IMAGE_DOWNLOAD_DATA,
                    pack(">I", index) + chunk,
                )
            )

        self._packets_for_printing.append(
            self._create_packet(InstaxEvent.PRINT_IMAGE_DOWNLOAD_END)
        )
        self._packets_for_printing.append(self._create_packet(InstaxEvent.PRINT_IMAGE))
        self._packets_for_printing.append(self._create_packet((0, 2), b"\x02"))

        first_packet = self._packets_for_printing.pop(0)
        self._send_packet(first_packet)
        self._wait_for_queue_empty(timeout_seconds=45)

    def _find_printer(self, timeout_seconds=10):
        deadline = monotonic() + timeout_seconds if timeout_seconds else None
        while True:
            self.adapter.scan_for(2000)
            for peripheral in self.adapter.scan_get_results():
                name = peripheral.identifier() or ""
                address = (peripheral.address() or "").upper()
                if not self._matches_printer(name, address):
                    continue
                if peripheral.is_connectable():
                    return peripheral
                logger.debug(
                    "found Instax printer but it is not connectable: %s [%s]",
                    name,
                    address,
                )

            if deadline is not None and monotonic() >= deadline:
                return None

    def _matches_printer(self, name, address):
        upper_name = name.upper()
        if self.device_name and upper_name.startswith(self.device_name):
            return True
        if self.device_address and address == self.device_address:
            return True
        if not self.device_name and not self.device_address:
            return upper_name.startswith("INSTAX-") and upper_name.endswith("(IOS)")
        return False

    def _notification_handler(self, packet):
        if len(packet) < 8 or not self._validate_checksum(packet):
            return
        _header, _length, op1, op2 = unpack_from(">HHBB", packet)
        try:
            event = InstaxEvent((op1, op2))
        except ValueError:
            self._waiting_for_response = False
            return

        self._waiting_for_response = False
        if event in {
            InstaxEvent.PRINT_IMAGE_DOWNLOAD_START,
            InstaxEvent.PRINT_IMAGE_DOWNLOAD_DATA,
            InstaxEvent.PRINT_IMAGE_DOWNLOAD_END,
            InstaxEvent.PRINT_IMAGE,
        }:
            self._send_next_print_packet()
        elif event == InstaxEvent.SUPPORT_FUNCTION_INFO and len(packet) >= 9:
            self._parse_support_function_info(packet)

    def _parse_support_function_info(self, packet):
        info_type = packet[6]
        if info_type == InstaxInfo.BATTERY_INFO.value and len(packet) >= 10:
            _battery_state, battery_pct = unpack_from(">BB", packet[8:10])
            self.status.battery_percentage = battery_pct
        elif info_type == InstaxInfo.PRINTER_FUNCTION_INFO.value and len(packet) >= 9:
            data = packet[8]
            self.status.photos_left = data & 15
            self.status.is_charging = bool(data & (1 << 7))

    def _send_next_print_packet(self):
        if self._packets_for_printing and not self._cancelled:
            self._send_packet(self._packets_for_printing.pop(0))

    def _read_printer_status(self):
        self._send_packet(
            self._create_packet(
                InstaxEvent.SUPPORT_FUNCTION_INFO,
                pack(">B", InstaxInfo.BATTERY_INFO.value),
            )
        )
        self._send_packet(
            self._create_packet(
                InstaxEvent.SUPPORT_FUNCTION_INFO,
                pack(">B", InstaxInfo.PRINTER_FUNCTION_INFO.value),
            )
        )

    def _wait_for_queue_empty(self, timeout_seconds):
        deadline = monotonic() + timeout_seconds
        while (
            self._packets_for_printing or self._waiting_for_response
        ) and monotonic() < deadline:
            sleep(0.05)
        if self._packets_for_printing:
            raise RuntimeError("Timeout while sending print data to Instax printer")

    def _send_packet(self, packet):
        if not self.peripheral or not self.peripheral.is_connected():
            raise RuntimeError("Instax printer is not connected")

        deadline = monotonic() + 10
        while self._waiting_for_response and not self._cancelled:
            if monotonic() >= deadline:
                raise RuntimeError("Timeout waiting for Instax printer response")
            sleep(0.05)

        self._waiting_for_response = True
        parts = ceil(len(packet) / self.BLE_WRITE_SIZE)
        for part_index in range(parts):
            sub_packet = packet[
                part_index
                * self.BLE_WRITE_SIZE : (part_index + 1)
                * self.BLE_WRITE_SIZE
            ]
            self.peripheral.write_command(
                self.SERVICE_UUID,
                self.WRITE_CHAR_UUID,
                sub_packet,
            )

    def _create_packet(self, event, payload=b""):
        op1, op2 = event.value if isinstance(event, InstaxEvent) else event
        packet = (
            b"\x41\x62" + pack(">H", 7 + len(payload)) + bytes([op1, op2]) + payload
        )
        packet += pack("B", self._checksum(packet))
        return packet

    @staticmethod
    def _checksum(data):
        return (255 - (sum(data) & 255)) & 255

    @staticmethod
    def _validate_checksum(packet):
        return (sum(packet) & 255) == 255

    def _image_to_jpeg_bytes(self, image_path):
        if isinstance(image_path, BytesIO):
            image_path.seek(0)
            image = Image.open(image_path)
        else:
            image = Image.open(image_path)

        # Respect EXIF orientation first, otherwise portrait photos can arrive
        # sideways before we even resize them.
        image = ImageOps.exif_transpose(image)

        if image.mode == "RGBA":
            background = Image.new("RGB", image.size, (255, 255, 255))
            background.paste(image, mask=image.getchannel("A"))
            image = background
        elif image.mode != "RGB":
            image = image.convert("RGB")

        # Instax Mini Link expects a portrait JPEG: 600 x 800 px.
        # If the booth output is landscape, rotate it before fitting so the
        # printed picture uses the portrait Mini frame instead of being squeezed.
        if self.auto_rotate_landscape and image.width > image.height:
            image = image.rotate(90, expand=True)

        if self.image_fit == "contain":
            image.thumbnail(self.IMAGE_SIZE, Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", self.IMAGE_SIZE, (255, 255, 255))
            left = (self.IMAGE_SIZE[0] - image.width) // 2
            top = (self.IMAGE_SIZE[1] - image.height) // 2
            canvas.paste(image, (left, top))
            image = canvas
        else:
            image = ImageOps.fit(
                image,
                self.IMAGE_SIZE,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )

        # Instax film/printers tend to look less punchy than the screen.
        # Apply a light configurable boost before JPEG compression.
        if self.autocontrast:
            image = ImageOps.autocontrast(image, cutoff=1)
        if self.color_boost != 1.0:
            image = ImageEnhance.Color(image).enhance(self.color_boost)
        if self.contrast_boost != 1.0:
            image = ImageEnhance.Contrast(image).enhance(self.contrast_boost)
        if self.brightness_boost != 1.0:
            image = ImageEnhance.Brightness(image).enhance(self.brightness_boost)
        if self.sharpness_boost != 1.0:
            image = ImageEnhance.Sharpness(image).enhance(self.sharpness_boost)

        buffer = BytesIO()
        low_quality = 1
        high_quality = 100
        quality = 85
        closest_quality = quality
        max_size_kb = self.MAX_JPEG_KB
        min_target_size_kb = max_size_kb * 0.88

        while low_quality <= high_quality:
            buffer.seek(0)
            buffer.truncate(0)
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            size_kb = buffer.tell() / 1024

            if min_target_size_kb <= size_kb <= max_size_kb:
                closest_quality = quality
                break
            if size_kb > max_size_kb:
                high_quality = quality - 1
            else:
                low_quality = quality + 1
            quality = (low_quality + high_quality) // 2
            closest_quality = max(1, min(100, quality))

        buffer.seek(0)
        buffer.truncate(0)
        image.save(buffer, format="JPEG", quality=closest_quality, optimize=True)
        return bytearray(buffer.getvalue())
