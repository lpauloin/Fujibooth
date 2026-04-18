from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil


class PhotoRepository:
    def __init__(self, output_dir: Path, extensions: list[str], filename_pattern: str) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.extensions = {ext.lower() for ext in extensions}
        self.filename_pattern = filename_pattern

    def store(self, source_path: Path) -> Path:
        suffix = source_path.suffix.lower()
        if suffix not in self.extensions:
            raise ValueError(f'Unsupported file extension: {source_path.suffix}')
        stem = datetime.now().strftime(self.filename_pattern)
        target = self.output_dir / f'{stem}{suffix}'
        counter = 1
        while target.exists():
            target = self.output_dir / f'{stem}_{counter}{suffix}'
            counter += 1
        shutil.copy2(source_path, target)
        return target

    def recent(self, limit: int = 50) -> list[Path]:
        photos = [
            path for path in self.output_dir.iterdir()
            if path.is_file() and path.suffix.lower() in self.extensions
        ]
        photos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return photos[:limit]
