"""WebUI image attachment request parsing tests."""

from __future__ import annotations

import base64

import pytest
from fastapi import HTTPException

from autoverse.frontends.web.server import _image_attachments


def test_image_attachment_parser_accepts_data_urls() -> None:
    encoded = base64.b64encode(b"abc").decode("ascii")

    [image] = _image_attachments(
        [{"media_type": "image/png", "data": f"data:image/png;base64,{encoded}"}]
    )

    assert image.media_type == "image/png"
    assert image.data == encoded
    assert image.source_type == "base64"


def test_image_attachment_parser_rejects_non_images() -> None:
    with pytest.raises(HTTPException) as exc:
        _image_attachments([{"media_type": "text/plain", "data": "YWJj"}])

    assert exc.value.status_code == 400
