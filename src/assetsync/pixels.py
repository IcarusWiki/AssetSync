"""Content hash of an image's visible pixels.

Exporters disagree about the colour stored under fully transparent pixels (FModel and
Ue4Export produce different bytes for the same icon), so those are zeroed before hashing.
Two images get the same hash exactly when every visible pixel is identical.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image


def pixel_hash(source: Path | bytes) -> str:
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    with Image.open(io.BytesIO(data)) as im:
        rgba = im.convert("RGBA")
    visible = rgba.getchannel("A").point(lambda a: 255 if a else 0)
    clear = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    normalized = Image.composite(rgba, clear, visible)
    width, height = normalized.size
    digest = hashlib.sha256(f"{width}x{height}:".encode())
    digest.update(normalized.tobytes())
    return digest.hexdigest()
