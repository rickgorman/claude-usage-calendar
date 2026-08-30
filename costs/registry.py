"""Integration-owned registry for source semantic adapters.

Provider workstreams implement their adapter modules without editing this file.
The integration workstream is the sole owner of populating these slots.
"""

from __future__ import annotations

from .adapters import anthropic, cursor, openai, xai
from .contracts import UsageNormalizer

ANTHROPIC = "anthropic"
OPENAI = "openai"
XAI = "xai"
CURSOR = "cursor"

ADAPTER_SLOT_NAMES = (ANTHROPIC, OPENAI, XAI, CURSOR)
ADAPTER_SLOTS: dict[str, UsageNormalizer | None] = {
    ANTHROPIC: anthropic.normalize,
    OPENAI: openai.normalize,
    XAI: xai.normalize,
    CURSOR: cursor.normalize,
}

__all__ = [
    "ADAPTER_SLOTS",
    "ADAPTER_SLOT_NAMES",
    "ANTHROPIC",
    "CURSOR",
    "OPENAI",
    "XAI",
]
