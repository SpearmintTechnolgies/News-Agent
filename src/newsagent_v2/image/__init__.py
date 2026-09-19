"""NewsAgent V2 image-generation subsystem. Provider-independent. Phase A offline."""

from newsagent_v2.image.brief import VisualBrief, build_visual_brief
from newsagent_v2.image.contract import (
    ARTWORK_ONLY_INSTRUCTIONS,
    CARD_HEIGHT,
    CARD_WIDTH,
    IMAGE_BRIEF_SCHEMA_VERSION,
)
from newsagent_v2.image.provider import ImageProvider, ProviderResult, canonical_provider_request

__all__ = [
    "ARTWORK_ONLY_INSTRUCTIONS",
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "IMAGE_BRIEF_SCHEMA_VERSION",
    "ImageProvider",
    "ProviderResult",
    "VisualBrief",
    "build_visual_brief",
    "canonical_provider_request",
]
