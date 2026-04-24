from datetime import datetime
from pathlib import Path
import shutil

from PIL import Image as PILImage


class PhotoRepository:
    def __init__(self, captures_dir, output_dir, extensions, filename_pattern, frame_path):
        self.captures_dir = captures_dir
        self.output_dir = output_dir
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.extensions = {ext.lower() for ext in extensions}
        self.filename_pattern = filename_pattern
        self._frame_image = None

        if frame_path:
            if not frame_path.exists():
                print(f"[REPO] frame PNG not found: {frame_path}")
            else:
                self._frame_image = PILImage.open(frame_path).convert("RGBA")
                print(
                    f"[REPO] frame loaded: {frame_path} size={self._frame_image.size}"
                )

    @property
    def _has_frame(self):
        return self._frame_image is not None

    def _apply_frame(self, original_path, framed_path):
        try:
            photo = PILImage.open(original_path).convert("RGBA")
            frame = self._frame_image.resize(photo.size, PILImage.LANCZOS)
            composite = PILImage.alpha_composite(photo, frame)
            composite.convert("RGB").save(framed_path, quality=95)
            return True
        except Exception as exc:
            print(f"[REPO] _apply_frame error: {exc}")
            return False

    def _unique_path(self, directory, stem, suffix):
        target = directory / f"{stem}{suffix}"
        counter = 1
        while target.exists():
            target = directory / f"{stem}_{counter}{suffix}"
            counter += 1
        return target

    def store(self, source_path):
        suffix = source_path.suffix.lower()
        if suffix not in self.extensions:
            raise ValueError(f"Unsupported file extension: {source_path.suffix}")
        stem = datetime.now().strftime(self.filename_pattern)

        capture_target = self._unique_path(self.captures_dir, stem, suffix)
        shutil.move(str(source_path), capture_target)
        print(f"[REPO] capture saved: {capture_target}")

        if self._has_frame:
            output_target = self._unique_path(self.output_dir, stem, ".jpg")
            if self._apply_frame(capture_target, output_target):
                print(f"[REPO] framed output saved: {output_target}")
                return output_target
            print("[REPO] frame apply failed, returning capture")

        return capture_target

    def recent(self, limit=50):
        search_dir = self.output_dir if self._has_frame else self.captures_dir
        photos = [
            path
            for path in search_dir.iterdir()
            if path.is_file() and path.suffix.lower() in self.extensions
        ]
        photos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return photos[:limit]