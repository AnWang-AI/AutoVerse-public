"""Shared multimodal image clamping (docs/reference/models.md §10/§14).

Oversize base64 images are downscaled to <= MAX_DIMENSION px on the long side
and re-encoded as JPEG so the wire payload stays bounded. Without Pillow the
image is passed through unchanged (the backend then decides), per §14.

Pure and network-free — codecs may import this (P6 codec/transport split holds:
no transport/httpx here).
"""

from __future__ import annotations

import base64
import binascii

MAX_DIMENSION = 1999  # spec §10: clamp to <= 1999px on the long side
_JPEG_QUALITY = 85


def clamp_image_base64(media_type: str, data: str) -> tuple[str, str]:
    """Return a (media_type, base64_data) pair clamped to MAX_DIMENSION.

    On any failure (no Pillow, undecodable, already small) the input is returned
    unchanged so multimodal never hard-fails on a best-effort optimisation.
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 — Pillow optional, pass through
        return media_type, data

    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return media_type, data

    import io
    from typing import Any

    try:
        img: Any = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001 — undecodable, pass through
        return media_type, data

    w, h = img.size
    if max(w, h) <= MAX_DIMENSION:
        return media_type, data

    scale = MAX_DIMENSION / float(max(w, h))
    img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return "image/jpeg", base64.b64encode(buf.getvalue()).decode("ascii")


__all__ = ["clamp_image_base64", "MAX_DIMENSION"]
