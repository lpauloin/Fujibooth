from dataclasses import dataclass
import logging
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import time

from .instax_minilink import InstaxMiniLinkClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PrintJob:
    photo_path: Path
    created_at: float


@dataclass(slots=True)
class PrintResult:
    ok: bool
    message: str
    photo_path: Path | None = None


class PrintService:
    """Small single-worker print service."""

    MODE_STUB = "stub"
    MODE_COMMAND = "command"
    MODE_INSTAX_BLE = "instax_ble"

    def __init__(
        self,
        enabled,
        command="",
        mode="auto",
        device_name="",
        device_address="",
        wait_after_print_seconds=60,
        max_queue_size=3,
        on_result=None,
        auto_rotate_landscape=True,
        image_fit="cover",
        autocontrast=True,
        color_boost=1.25,
        contrast_boost=1.15,
        brightness_boost=1.05,
        sharpness_boost=1.05,
    ):
        self.enabled = bool(enabled)
        self.command = (command or "").strip()
        self.mode = (mode or "auto").strip().lower()
        self.device_name = (device_name or "").strip()
        self.device_address = (device_address or "").strip()
        self.wait_after_print_seconds = max(0, int(wait_after_print_seconds or 0))
        self.on_result = on_result
        self.auto_rotate_landscape = bool(auto_rotate_landscape)
        self.image_fit = (image_fit or "cover").strip().lower()
        self.autocontrast = bool(autocontrast)
        self.color_boost = float(color_boost or 1.0)
        self.contrast_boost = float(contrast_boost or 1.0)
        self.brightness_boost = float(brightness_boost or 1.0)
        self.sharpness_boost = float(sharpness_boost or 1.0)

        self._queue = queue.Queue(maxsize=max(1, int(max_queue_size or 1)))
        self._worker = None
        self._stop_event = threading.Event()
        self._worker_lock = threading.Lock()
        self._busy = False
        self._busy_lock = threading.Lock()

    def print_photo(self, photo_path, wait=False):
        photo_path = Path(photo_path)
        if not photo_path.exists():
            return False, f"Photo introuvable: {photo_path}"

        if not self.enabled:
            return True, f"PRINT stub: {photo_path.name}"

        mode = self._resolved_mode()
        if mode == self.MODE_COMMAND and not self.command:
            return False, "Printing enabled but no command configured"

        if wait:
            result = self._print_now(
                PrintJob(photo_path=photo_path, created_at=time.time())
            )
            return result.ok, result.message

        self._ensure_worker()
        try:
            self._queue.put_nowait(
                PrintJob(photo_path=photo_path, created_at=time.time())
            )
        except queue.Full:
            return (
                False,
                "File d'impression pleine: attends la fin de l'impression en cours",
            )

        return True, f"Impression ajoutee a la file: {photo_path.name}"

    def is_busy(self):
        with self._busy_lock:
            busy = self._busy
        return busy or not self._queue.empty()

    def stop(self):
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._worker.join(timeout=2.0)

    def _ensure_worker(self):
        with self._worker_lock:
            if self._worker and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="PrintServiceWorker",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                job = self._queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if job is None:
                self._queue.task_done()
                break
            result = self._print_now(job)
            self._emit_result(result)
            self._queue.task_done()

    def _emit_result(self, result):
        logger.info("result ok=%s message=%s", result.ok, result.message)
        if self.on_result:
            try:
                self.on_result(result)
            except Exception as exc:
                logger.error("on_result callback failed: %s", exc)

    def _print_now(self, job):
        with self._busy_lock:
            self._busy = True
        try:
            try:
                mode = self._resolved_mode()
                if mode == self.MODE_COMMAND:
                    self._print_with_command(job.photo_path)
                elif mode == self.MODE_INSTAX_BLE:
                    self._print_with_instax_ble(job.photo_path)
                else:
                    return PrintResult(
                        True, f"PRINT stub: {job.photo_path.name}", job.photo_path
                    )
                return PrintResult(
                    True,
                    f"Photo envoyee a l impression: {job.photo_path.name}",
                    job.photo_path,
                )
            except Exception as exc:
                return PrintResult(False, f"Erreur impression: {exc}", job.photo_path)
        finally:
            with self._busy_lock:
                self._busy = False
    def _resolved_mode(self):
        if self.mode == "auto":
            if self.command:
                return self.MODE_COMMAND
            return self.MODE_INSTAX_BLE
        if self.mode in {self.MODE_STUB, self.MODE_COMMAND, self.MODE_INSTAX_BLE}:
            return self.mode
        raise ValueError(f"Mode impression inconnu: {self.mode}")

    def _build_command(self, photo_path):
        if "{path}" in self.command or "{photo_path}" in self.command:
            command = self.command.format(
                path=str(photo_path), photo_path=str(photo_path)
            )
            return shlex.split(command)
        return shlex.split(self.command) + [str(photo_path)]

    def _print_with_command(self, photo_path):
        cmd = self._build_command(photo_path)
        logger.info("command=%s", " ".join(shlex.quote(p) for p in cmd))
        subprocess.run(cmd, check=True)

    def _print_with_instax_ble(self, photo_path):
        instax = InstaxMiniLinkClient(
            device_name=self.device_name,
            device_address=self.device_address,
            auto_rotate_landscape=self.auto_rotate_landscape,
            image_fit=self.image_fit,
            autocontrast=self.autocontrast,
            color_boost=self.color_boost,
            contrast_boost=self.contrast_boost,
            brightness_boost=self.brightness_boost,
            sharpness_boost=self.sharpness_boost,
        )
        try:
            logger.info(
                "connecting to Instax Mini Link%s%s",
                f" name={self.device_name}" if self.device_name else "",
                f" address={self.device_address}" if self.device_address else "",
            )
            instax.connect(timeout_seconds=10)
            instax.print_image(photo_path)

            if self.wait_after_print_seconds > 0:
                time.sleep(self.wait_after_print_seconds)
        finally:
            try:
                instax.disconnect()
            except Exception as exc:
                logger.warning("disconnect failed: %s", exc)
