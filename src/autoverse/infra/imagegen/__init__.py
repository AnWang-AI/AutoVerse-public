"""Image-generation backends (docs/reference/tools.md §11.7).

The default :class:`OpenRouterImageBackend` is the concrete implementation the
``generate_image`` tool shell delegates to via ``AppContext.image_backend``.
Kept out of ``core`` (like the browser backend, P6): only ``app``/``infra`` wire
it, so the tool stays a thin, network-free shell.
"""

from __future__ import annotations

from autoverse.infra.imagegen.openrouter import (
    ImageGenError,
    ImageResult,
    OpenRouterImageBackend,
)

__all__ = ["OpenRouterImageBackend", "ImageResult", "ImageGenError"]
