import logging
from datetime import datetime
import shutil


from PIL import Image as PILImage

logger = logging.getLogger(__name__)


class PhotoRepository:
    def __init__(
        self, captures_dir, output_dir, extensions, filename_pattern, frame_path
    ):
        self.captures_dir = captures_dir
        self.output_dir = output_dir
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.extensions = {ext.lower() for ext in extensions}
        self.filename_pattern = filename_pattern
        self._frame_image = None

        if frame_path:
            if not frame_path.exists():
                logger.warning("frame PNG not found: %s", frame_path)
            else:
                self._frame_image = PILImage.open(frame_path).convert("RGBA")
                logger.info(
                    "frame loaded: %s size=%s", frame_path, self._frame_image.size
                )

    @property
    def has_frame(self):
        return self._frame_image is not None

    def save(self, source_path):
        """Move a raw SDK file into captures_dir with a timestamped name."""
        suffix = source_path.suffix.lower()
        if suffix not in self.extensions:
            raise ValueError(f"Unsupported file extension: {source_path.suffix}")
        stem = datetime.now().strftime(self.filename_pattern)
        target = self._unique_path(self.captures_dir, stem, suffix)
        shutil.move(str(source_path), target)
        logger.info("raw saved: %s", target)
        return target

    def frame(self, raw_path):
        """Composite the frame over raw_path and write the result to output_dir."""
        if not self.has_frame:
            raise RuntimeError("No frame loaded")
        output = self._unique_path(self.output_dir, raw_path.stem, ".jpg")
        try:
            photo = PILImage.open(raw_path).convert("RGBA")
            overlay = self._frame_image.resize(photo.size, PILImage.LANCZOS)
            composite = PILImage.alpha_composite(photo, overlay)
            composite.convert("RGB").save(output, quality=95)
        except Exception as exc:
            logger.error("frame error: %s", exc)
            raise
        logger.info("framed saved: %s", output)
        return output

    def recent(self, limit=50):
        """List photos for gallery display (framed if a frame is loaded, else raw)."""
        search_dir = self.output_dir if self.has_frame else self.captures_dir
        photos = [
            p
            for p in search_dir.iterdir()
            if p.is_file() and p.suffix.lower() in self.extensions
        ]
        photos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return photos[:limit]

    def _unique_path(self, directory, stem, suffix):
        target = directory / f"{stem}{suffix}"
        counter = 1
        while target.exists():
            target = directory / f"{stem}_{counter}{suffix}"
            counter += 1
        return target
