import hashlib
import logging
from datetime import datetime
import shutil

from PIL import Image as PILImage, ImageOps

logger = logging.getLogger(__name__)


class PhotoRepository:
    def __init__(
        self,
        captures_dir,
        output_dir,
        extensions,
        filename_pattern,
        frame_path,
        thumbnails_dir=None,
        thumbnail_size=None,
    ):
        self.captures_dir = captures_dir
        self.output_dir = output_dir
        self.thumbnails_dir = thumbnails_dir or (output_dir.parent / "thumbnails")
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.thumbnails_dir.mkdir(parents=True, exist_ok=True)
        self.extensions = {ext.lower() for ext in extensions}
        self.filename_pattern = filename_pattern
        self.thumbnail_size = thumbnail_size or (220, 146)
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
        self.ensure_thumbnail(output)
        return output

    def photo_dir(self):
        return self.output_dir if self.has_frame else self.captures_dir

    def all_photos(self):
        search_dir = self.photo_dir()
        photos = [
            p
            for p in search_dir.iterdir()
            if p.is_file() and p.suffix.lower() in self.extensions
        ]
        photos.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return photos

    def recent(self, limit=50):
        """List photos for full-size display only when the user selects one."""
        return self.all_photos()[:limit]

    def ensure_all_thumbnails(self):
        photos = self.all_photos()
        created = 0
        for photo in photos:
            thumb = self.ensure_thumbnail(photo)
            if thumb is not None:
                created += 1
        logger.info(
            "thumbnails checked photos=%s ensured=%s dir=%s",
            len(photos),
            created,
            self.thumbnails_dir,
        )
        return created

    def recent_thumbnails(self, limit=50):
        records = []
        for photo in self.recent(limit=limit):
            thumb = self.ensure_thumbnail(photo) or self.thumbnail_path_for(photo)
            if thumb.exists():
                records.append({"photo_path": photo, "thumbnail_path": thumb})
        return records

    def thumbnail_path_for(self, photo_path):
        key = hashlib.sha1(str(photo_path.resolve()).encode("utf-8")).hexdigest()
        return self.thumbnails_dir / f"{key}.jpg"

    def ensure_thumbnail(self, photo_path):
        thumb_path = self.thumbnail_path_for(photo_path)
        try:
            if (
                thumb_path.exists()
                and thumb_path.stat().st_mtime_ns >= photo_path.stat().st_mtime_ns
            ):
                return thumb_path
        except OSError:
            return None

        try:
            with PILImage.open(photo_path) as img:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img = ImageOps.fit(
                    img,
                    self.thumbnail_size,
                    method=PILImage.LANCZOS,
                    centering=(0.5, 0.5),
                )
                thumb_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = thumb_path.with_suffix(".tmp.jpg")
                img.save(tmp_path, format="JPEG", quality=85, optimize=True)
                tmp_path.replace(thumb_path)
        except Exception as exc:
            logger.warning("thumbnail failed photo=%s error=%s", photo_path, exc)
            return None

        return thumb_path

    def _unique_path(self, directory, stem, suffix):
        target = directory / f"{stem}{suffix}"
        counter = 1
        while target.exists():
            target = directory / f"{stem}_{counter}{suffix}"
            counter += 1
        return target
