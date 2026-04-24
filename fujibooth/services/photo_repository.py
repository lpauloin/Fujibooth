from datetime import datetime
from pathlib import Path
import shutil

from PIL import Image as PILImage

FRAMED_SUFFIX = "_framed"


class PhotoRepository:
    def __init__(self, output_dir, extensions, filename_pattern, frame_path):
        self.output_dir = output_dir
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

    def store(self, source_path):
        suffix = source_path.suffix.lower()
        if suffix not in self.extensions:
            raise ValueError(f"Unsupported file extension: {source_path.suffix}")
        stem = datetime.now().strftime(self.filename_pattern)
        target = self.output_dir / f"{stem}{suffix}"
        counter = 1
        while target.exists():
            target = self.output_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        shutil.copy2(source_path, target)
        print(f"[REPO] original saved: {target}")

        if self._has_frame:
            framed_target = self.output_dir / f"{target.stem}{FRAMED_SUFFIX}.jpg"
            if self._apply_frame(target, framed_target):
                print(f"[REPO] framed version saved: {framed_target}")
                return framed_target
            print("[REPO] frame apply failed, returning original")

        return target

    def recent(self, limit=50):
        photos = [
            path
            for path in self.output_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() in self.extensions
            and self._is_displayable(path)
        ]
        photos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return photos[:limit]

    def _is_displayable(self, path):
        if self._has_frame:
            return path.stem.endswith(FRAMED_SUFFIX)
        return not path.stem.endswith(FRAMED_SUFFIX)