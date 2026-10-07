"""autoverse.infra.llm.codec — protocol layer (two codecs, P6)."""

from __future__ import annotations

from autoverse.infra.llm.codec.anthropic import AnthropicWireCodec
from autoverse.infra.llm.codec.base import Codec, DecodeState
from autoverse.infra.llm.codec.openai import OpenAIWireCodec

__all__ = [
    "Codec",
    "DecodeState",
    "AnthropicWireCodec",
    "OpenAIWireCodec",
]
