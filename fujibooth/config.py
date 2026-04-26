from dataclasses import asdict, dataclass, field, fields as dc_fields
from pathlib import Path
import os

import yaml
import logging



logger = logging.getLogger(__name__)
@dataclass(slots=True)
class AppConfig:
    window_title: str = "FujiBooth"
    fullscreen: bool = True
    countdown_seconds: int = 5
    freeze_seconds: int = 10
    print_button_seconds: int = 5


@dataclass(slots=True)
class UsbConfig:
    enabled: bool = False
    vendor_id: str = "04cb"
    product_ids: list[str] = field(default_factory=list)
    camera_name_contains: str = "FUJIFILM"


@dataclass(slots=True)
class SdkConfig:
    sdk_root: str = "./sdk"
    library_path: str = ""
    live_view_interval_ms: int = 120


@dataclass(slots=True)
class CameraConfig:
    usb: UsbConfig = field(default_factory=UsbConfig)
    sdk: SdkConfig = field(default_factory=SdkConfig)


@dataclass(slots=True)
class StorageConfig:
    captures_dir: str = "./runtime/captures"   # originals from SDK
    output_dir: str = "./runtime/output"       # framed photos
    accepted_extensions: list[str] = field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".raf", ".heic"]
    )
    filename_pattern: str = "%Y%m%d_%H%M%S"
    frame_path: str = ""


@dataclass(slots=True)
class PrintingConfig:
    enabled: bool = False
    monitor_enabled: bool = True
    scan_interval_ms: int = 15000
    scan_duration_ms: int = 1500
    lost_after_misses: int = 3
    initial_grace_ms: int = 10000
    cooldown_after_busy_ms: int = 30000
    # auto: command if command is set, otherwise local Instax Mini Link BLE
    # command: external command such as lp
    # instax_ble: local minimal simplepyble client for Instax Mini Link
    # stub: development mode, no hardware call
    mode: str = "auto"
    command: str = ""
    device_name: str = ""
    device_address: str = ""
    wait_after_print_seconds: int = 60
    max_queue_size: int = 3
    auto_rotate_landscape: bool = True
    image_fit: str = "cover"
    autocontrast: bool = True
    color_boost: float = 1.25
    contrast_boost: float = 1.15
    brightness_boost: float = 1.05
    sharpness_boost: float = 1.05


@dataclass(slots=True)
class RemoteConfig:
    enabled: bool = False
    hid_device_name: str = ""
    hid_vendor_id: int = 0
    hid_product_id: int = 0


@dataclass(slots=True)
class UiConfig:
    background_color: str = "#111111"
    status_connected_color: str = "#16a34a"
    status_disconnected_color: str = "#dc2626"
    thumbnail_width: int = 220
    thumbnail_height: int = 146
    bottom_gallery_height: int = 210


@dataclass(slots=True)
class Settings:
    app: AppConfig = field(default_factory=AppConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    printing: PrintingConfig = field(default_factory=PrintingConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    config_path: Path | None = None

    @property
    def captures_path(self):
        return Path(os.path.expanduser(self.storage.captures_dir)).resolve()

    @property
    def output_path(self):
        return Path(os.path.expanduser(self.storage.output_dir)).resolve()

    @property
    def frame_path(self):
        if not self.storage.frame_path:
            return None
        return Path(os.path.expanduser(self.storage.frame_path)).resolve()


def _merge_dict(defaults, override):
    merged = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build(cls, data):
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _dataclass_to_dict(settings):
    return asdict(settings) | {"config_path": settings.config_path}


def build_settings(payload, config_path=None):
    defaults = _dataclass_to_dict(Settings())
    merged = _merge_dict(defaults, payload)
    settings = Settings(
        app=_build(AppConfig, merged["app"]),
        camera=CameraConfig(
            usb=_build(UsbConfig, merged["camera"]["usb"]),
            sdk=_build(SdkConfig, merged["camera"]["sdk"]),
        ),
        storage=_build(StorageConfig, merged["storage"]),
        printing=_build(PrintingConfig, merged["printing"]),
        ui=_build(UiConfig, merged["ui"]),
        remote=_build(RemoteConfig, merged["remote"]),
        config_path=config_path,
    )
    settings.captures_path.mkdir(parents=True, exist_ok=True)
    settings.output_path.mkdir(parents=True, exist_ok=True)
    return settings


def default_config_candidates():
    return [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config" / "config.yaml",
        Path.home() / ".config" / "fujibooth" / "config.yaml",
    ]


def load_settings(config_path=None):
    candidates = [Path(config_path)] if config_path else default_config_candidates()
    for candidate in candidates:
        if candidate.exists():
            payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            settings = build_settings(payload, config_path=candidate)
            logger.info("loaded: %s", candidate)
            logger.info("captures_dir: %s", settings.captures_path)
            logger.info("output_dir: %s", settings.output_path)
            return settings
    settings = build_settings({}, config_path=None)
    logger.info("no config file found, using defaults")
    logger.info("captures_dir: %s", settings.captures_path)
    logger.info("output_dir: %s", settings.output_path)
    return settings
