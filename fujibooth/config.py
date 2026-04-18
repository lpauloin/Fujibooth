from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
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
    capture_dir: str = "./runtime/sdk_captures"
    live_view_interval_ms: int = 120


@dataclass(slots=True)
class CameraConfig:
    usb: UsbConfig = field(default_factory=UsbConfig)
    sdk: SdkConfig = field(default_factory=SdkConfig)


@dataclass(slots=True)
class StorageConfig:
    output_dir: str = "./runtime/output"
    incoming_dir: str = "./runtime/incoming"
    accepted_extensions: list[str] = field(default_factory=lambda: [".jpg", ".jpeg", ".png", ".raf", ".heic"])
    filename_pattern: str = "%Y%m%d_%H%M%S"


@dataclass(slots=True)
class PrintingConfig:
    enabled: bool = False
    command: str = ""


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
    config_path: Path | None = None

    @property
    def output_path(self) -> Path:
        return Path(os.path.expanduser(self.storage.output_dir)).resolve()

    @property
    def incoming_path(self) -> Path:
        return Path(os.path.expanduser(self.storage.incoming_dir)).resolve()

    @property
    def sdk_capture_path(self) -> Path:
        return Path(os.path.expanduser(self.camera.sdk.capture_dir)).resolve()


def _merge_dict(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _dataclass_to_dict(settings: Settings) -> dict[str, Any]:
    return asdict(settings) | {"config_path": settings.config_path}


def build_settings(payload: dict[str, Any], config_path: Path | None = None) -> Settings:
    defaults = _dataclass_to_dict(Settings())
    merged = _merge_dict(defaults, payload)
    settings = Settings(
        app=AppConfig(**merged["app"]),
        camera=CameraConfig(
            usb=UsbConfig(**merged["camera"]["usb"]),
            sdk=SdkConfig(**merged["camera"]["sdk"]),
        ),
        storage=StorageConfig(**merged["storage"]),
        printing=PrintingConfig(**merged["printing"]),
        ui=UiConfig(**merged["ui"]),
        config_path=config_path,
    )
    settings.output_path.mkdir(parents=True, exist_ok=True)
    settings.incoming_path.mkdir(parents=True, exist_ok=True)
    settings.sdk_capture_path.mkdir(parents=True, exist_ok=True)
    return settings


def default_config_candidates() -> list[Path]:
    return [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config" / "config.yaml",
        Path.home() / ".config" / "fujibooth" / "config.yaml",
    ]


def load_settings(config_path: str | Path | None = None) -> Settings:
    candidates = [Path(config_path)] if config_path else default_config_candidates()
    for candidate in candidates:
        if candidate.exists():
            payload = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
            settings = build_settings(payload, config_path=candidate)
            print(f"[CONFIG] fichier charge: {candidate}")
            print("[CONFIG] backend camera: fujifilm_sdk")
            print(f"[CONFIG] output_dir: {settings.output_path}")
            print(f"[CONFIG] incoming_dir: {settings.incoming_path}")
            print(f"[CONFIG] sdk_capture_dir: {settings.sdk_capture_path}")
            return settings
    settings = build_settings({}, config_path=None)
    print("[CONFIG] aucun fichier de configuration trouve, utilisation des valeurs par defaut")
    print("[CONFIG] backend camera: fujifilm_sdk")
    print(f"[CONFIG] output_dir: {settings.output_path}")
    print(f"[CONFIG] incoming_dir: {settings.incoming_path}")
    print(f"[CONFIG] sdk_capture_dir: {settings.sdk_capture_path}")
    return settings
