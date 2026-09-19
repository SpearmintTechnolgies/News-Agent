"""
Provider-independent image-generation contracts (image-brief-v1).

The image model renders artwork only. Headlines, logos, and publication
branding are applied later by the Python compositor. No field here is a
quality score or a billed cost.
"""

from __future__ import annotations

IMAGE_BRIEF_SCHEMA_VERSION = "image-brief-v1"
IMAGE_PROVIDER_RESULT_SCHEMA_VERSION = "image-provider-result-v1"
IMAGE_TELEMETRY_SCHEMA_VERSION = "image-telemetry-v1"
IMAGE_SCORECARD_SCHEMA_VERSION = "image-scorecard-v1"
IMAGE_COMPOSITION_SCHEMA_VERSION = "image-composition-v1"
IMAGE_RUN_SCHEMA_VERSION = "image-run-v1"
COMPOSITOR_VERSION = "image-compositor-v1"

CARD_ASPECT_RATIO = "16:9"
CARD_WIDTH = 1280
CARD_HEIGHT = 720

SUPPORTED_RAW_FORMATS = frozenset({"png", "jpeg", "jpg", "webp"})
REFERENCE_IMAGE_ROLES = frozenset({"story_reference", "style_reference"})
DEFAULT_REFERENCE_IMAGE_ROLE = "story_reference"


def normalize_reference_role(role: str | None) -> str:
    value = (role or DEFAULT_REFERENCE_IMAGE_ROLE).strip()
    if value not in REFERENCE_IMAGE_ROLES:
        raise ValueError(
            f"reference_image_role must be one of {sorted(REFERENCE_IMAGE_ROLES)}"
        )
    return value

REFERENCE_IMAGE_INSTRUCTIONS = (
    "The attached image is factual/visual context only.",
    "Create a new original editorial image.",
    "Use the source image only to understand the real subject and event.",
    "Do not copy the exact composition or layout.",
    "Do not reproduce logos, wordmarks, or text from the source image.",
    "Do not render readable text.",
    "Do not render a headline.",
    "Do not render a watermark.",
    "Do not render fake UI text.",
    "Do not render pseudotext.",
    "Do not render source branding.",
)

BACKEND_TYPES = frozenset(
    {
        "local_python",
        "local_http",
        "self_hosted_gpu",
        "remote_open_model",
        "remote_hosted",
        "synthetic_offline",
    }
)

ARTWORK_ONLY_INSTRUCTIONS = (
    "ARTWORK ONLY.",
    "The image must contain no writing of any kind.",
    "Do not render a headline.",
    "Do not render captions.",
    "Do not render paragraphs.",
    "Do not render article text.",
    "Do not render typography.",
    "Do not render the CoinNetwork logo.",
    "Do not render a publication logo.",
    "Do not render a watermark.",
    "Do not render random letters.",
    "Do not render words, readable text, numbers, labels, or percentages.",
    "Do not reserve space by generating fake text blocks.",
    "Do not render fake UI.",
    "Do not render fake trading dashboards, ticker values, or price labels.",
    "Do not render illegible pseudo-text.",
    "Do not render source-publication branding.",
    "Do not render invented chart values or numeric callouts.",
    "Do not render documents, newspapers, or reports with visible writing.",
    "Do not render malformed cryptocurrency glyphs or many floating Bitcoin logos.",
    "If screens appear, they must be strongly blurred non-semantic light only.",
    "If papers appear, they must be blank, generic, or out of focus.",
    "Leave clean uncluttered compositional space for later Python headline overlay; do not fill it with generated text.",
)

DEFAULT_NEGATIVE_PROMPT = (
    "headline, captions, article text, typography, letters, words, numbers, labels, "
    "logos, CoinNetwork, watermark, fake UI, illegible pseudo-text, source branding, "
    "publication masthead, readable documents, newspapers with text, fake trading "
    "dashboard, fake chart values, percentages, ticker values, price labels, "
    "company names, malformed bitcoin glyphs, floating bitcoin logos, cartoon meme, "
    "casino, rockets, moon cliches, piles of random coins, generic cyberpunk clutter, "
    "invented chart numbers"
)

# Historical comparison metadata from prior analysis. Not a measurement of
# this machine, not billing truth, and not a V2 benchmark result.
HISTORICAL_AADI_PIXEL_METADATA = {
    "label": "Nano Banana 2 Lite (Gemini 3.1 Flash Lite Image)",
    "observed_generation_seconds_examples": [9, 13],
    "cost_display_is_ground_truth_billing": False,
    "reconfirmed_from_aadi": False,
    "aspirational_v2_warm_generation_seconds": 15,
    "note": (
        "Treat as historical benchmark metadata only. Do not copy the "
        "Aadi implementation. Do not invent V2 timings from these figures."
    ),
}

REQUIRED_BRIEF_FIELDS = (
    "schema_version",
    "event_id",
    "editorial_subject",
    "visual_concept",
    "artwork_only",
    "facts",
    "visual_metaphors",
    "aspect_ratio",
    "width",
    "height",
)
