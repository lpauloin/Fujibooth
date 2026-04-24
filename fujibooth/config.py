from dataclasses import asdict, dataclass, field, fields as dc_fields
from pathlib import Path
import os

import yaml


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
    command: str = ""


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
            print(f"[CONFIG] loaded: {candidate}")
            print(f"[CONFIG] captures_dir: {settings.captures_path}")
            print(f"[CONFIG] output_dir:   {settings.output_path}")
            return settings
    settings = build_settings({}, config_path=None)
    print("[CONFIG] no config file found, using defaults")
    print(f"[CONFIG] captures_dir: {settings.captures_path}")
    print(f"[CONFIG] output_dir:   {settings.output_path}")
    return settings
