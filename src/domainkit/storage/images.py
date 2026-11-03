"""Thumbnails of stored images (``domainkit[images]``, Pillow).

::

    stored = await save_upload(file, types=IMAGES, max_size=5_000_000)
    small = await thumbnail(storages["default"], stored.name, size=(256, 256))

``thumbnail`` reads a stored image, scales it to fit ``size`` (keeping its
proportions, never enlarging it), and stores the result as a new file in
the same storage. The work runs in a thread. An image of more than
``MAX_PIXELS`` pixels, or one Pillow cannot read, is refused before it
is decoded: a small file that would decode to gigabytes (a decompression
bomb) costs nothing. Metadata (EXIF, with its location) is not copied.
"""

import asyncio
import io
from typing import Final

from domainkit.storage.base import Storage, StorageError

__all__ = ["NotAnImage", "thumbnail"]

MAX_PIXELS: Final = 50_000_000
"""The largest image read: 50 megapixels (an 8,000 by 6,000 photo is 48)."""
MAX_SOURCE_BYTES: Final = 50 * 1024 * 1024
"""The largest stored file read to make a thumbnail."""
FORMATS: Final = {"webp": ".webp", "png": ".png", "jpeg": ".jpg"}


class NotAnImage(StorageError):
    """The file is not an image Pillow reads, or is larger than ``MAX_PIXELS``."""


async def thumbnail(
    storage: Storage,
    name: str,
    *,
    size: tuple[int, int] = (256, 256),
    format: str = "webp",
    directory: str = "thumbnails",
) -> str:
    """Store a thumbnail of the image ``name`` that fits ``size``; its name.

    Raises:
        FileMissing: No file has the name.
        NotAnImage: It is not an image, or too large to read.
        ValueError: ``size`` or ``format`` is not one taken.
    """
    if format not in FORMATS:
        raise ValueError(f"A thumbnail is one of {sorted(FORMATS)}, not {format!r}.")
    if not all(0 < side <= 4096 for side in size):
        raise ValueError("A thumbnail's sides are 1 to 4096 pixels.")
    if await storage.size(name) > MAX_SOURCE_BYTES:
        raise NotAnImage("The image is too large to make a thumbnail of.")
    async with await storage.open(name) as file:
        source = await file.read()
    data = await asyncio.to_thread(_scaled, source, size, format)
    return await storage.save(data, directory=directory, extension=FORMATS[format])


def _scaled(source: bytes, size: tuple[int, int], format: str) -> bytes:
    """``source`` decoded, scaled to fit ``size``, and encoded as ``format``.

    Raises:
        NotAnImage: Pillow cannot read it, or it has too many pixels.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(source)) as image:
            width, height = image.size  # Read from the header, nothing decoded yet.
            if width * height > MAX_PIXELS:
                raise NotAnImage(
                    "The image has too many pixels to make a thumbnail of."
                )
            image.thumbnail(size)
            converted = image.convert("RGBA" if format != "jpeg" else "RGB")
            out = io.BytesIO()
            converted.save(out, format=format.upper())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise NotAnImage("That file is not an image that can be read.") from exc
    return out.getvalue()
