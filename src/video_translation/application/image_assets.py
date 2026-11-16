"""Decode input images without rewriting the uploaded original."""
from __future__ import annotations

import warnings
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_IMAGE_PIXELS = 40_000_000


def inspect_image(path: Path) -> dict:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if (image.format not in {"PNG", "JPEG", "WEBP", "BMP"}
                        or getattr(image, "n_frames", 1) != 1
                        or image.width * image.height > MAX_IMAGE_PIXELS):
                    raise ValueError("图片需为单帧PNG、JPEG、WebP或BMP，且不超过4000万像素")
                image.verify()
            with Image.open(path) as image:
                orientation = image.getexif().get(274, 1)
                normalized = ImageOps.exif_transpose(image)
                normalized.load()
                return {
                    "image": {
                        "format": image.format, "stored_size": list(image.size),
                        "display_size": list(normalized.size), "orientation": orientation,
                        "mode": image.mode, "animated": False,
                    }
                }
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise ValueError("图片无法完整解码，或超过图片尺寸保护") from exc
