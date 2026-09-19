"""
Fact-bounded StoryImageBrief for V4 image generation.

Built from frozen CanonicalArticle + WriterEvidencePacket semantics.
No LLM. Does not dump raw scraped source prose, HTML, or FactBank IDs.
"""

from __future__ import annotations

from typing import Any

from newsagent_v2.image.brief import VisualBrief, build_visual_brief
from newsagent_v2.image.contract import (
    ARTWORK_ONLY_INSTRUCTIONS,
    CARD_HEIGHT,
    CARD_WIDTH,
    DEFAULT_NEGATIVE_PROMPT,
)

FORBIDDEN_ELEMENTS = [
    "generated headline",
    "captions",
    "article text",
    "ticker overlays",
    "watermarks",
    "fake publication logos",
    "CoinNetwork logo",
    "fake UI",
    "fake charts with invented numbers",
    "fake documents with readable invented claims",
    "named politicians",
    "invented dollar figures as on-image text",
    "random floating crypto coins clutter",
    "government seals",
    "official emblems",
    "agency logos",
    "institutional insignia",
    "recognizable specific public figures",
]


def _entity_names(article: dict[str, Any], packet: dict[str, Any] | None) -> list[str]:
    names: list[str] = []
    for row in article.get("entities") or []:
        if isinstance(row, dict):
            name = str(row.get("name") or "").strip()
            if name and name not in names:
                names.append(name)
    if packet:
        for prop in packet.get("authorized_facts") or []:
            if not isinstance(prop, dict):
                continue
            for ent in prop.get("entities") or []:
                text = str(ent or "").strip()
                if text and " " not in text and text not in names and len(text) <= 32:
                    names.append(text)
    return names[:8]


def _facts_from_packet(packet: dict[str, Any] | None, headline: str, dek: str) -> list[str]:
    facts: list[str] = []
    if isinstance(packet, dict):
        for prop in packet.get("authorized_facts") or []:
            if not isinstance(prop, dict):
                continue
            text = str(prop.get("proposition") or prop.get("text") or "").strip()
            if not text:
                continue
            # Keep brief, non-numeric-heavy direction lines (no inventing).
            clipped = " ".join(text.split())
            if len(clipped) > 160:
                clipped = clipped[:157] + "..."
            facts.append(clipped)
            if len(facts) >= 3:
                break
    if not facts and dek:
        facts.append(" ".join(dek.split())[:160])
    if not facts and headline:
        facts.append(headline.strip())
    return facts


def build_story_image_brief(
    *,
    article: dict[str, Any],
    evidence_packet: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """
    Compact StoryImageBrief dict (persisted) + compatible VisualBrief fields.

    Fact-bounded from the frozen article + authorized packet. Metaphors are
    explicitly marked not_a_factual_claim.
    """
    eid = str(event_id or article.get("event_id") or "").strip()
    headline = str(article.get("headline") or "").strip()
    dek = str(article.get("dek") or "").strip()
    topic = ""
    if isinstance(evidence_packet, dict):
        topic = str(evidence_packet.get("story_topic") or "").strip()

    event_line = topic or headline
    primary = headline[:140] if headline else (topic[:140] if topic else "crypto / markets news event")
    secondary: list[str] = []
    if dek:
        secondary = [" ".join(dek.split())[:140]]

    support_blob = " ".join([headline, dek, topic]).lower()
    location = None
    if "washington" in support_blob or "senate" in support_blob or "congress" in support_blob:
        location = "Washington, D.C. government / markets context"
    elif "new york" in support_blob or "wall street" in support_blob:
        location = "New York / markets context"

    important_objects = [
        "clear story-specific focal subject drawn from the headline",
        "premium financial-news editorial atmosphere",
        "clean institutional composition with depth",
    ]
    if any(k in support_blob for k in ("bitcoin", "btc", "crypto", "etf", "token")):
        important_objects.append("restrained digital-asset visual motif (not meme coin clutter)")
    if any(k in support_blob for k in ("senate", "bill", "legislation", "regulator", "sec", "mica")):
        important_objects.append("generic government architecture / regulatory context (no seals)")

    facts = _facts_from_packet(evidence_packet, headline, dek)

    visual_context = (
        "Premium financial-news editorial illustration or polished editorial composite, "
        f"story-specific to: {event_line}. Clear focal subject, clean composition, strong depth, "
        "professional newsroom quality. Generic unnamed people only when needed; never imply a "
        "specific real politician/executive. Editorial composite acceptable."
    )

    story_brief = {
        "schema_version": "story-image-brief-v1",
        "event_id": eid,
        "story_type": "crypto_business_news",
        "primary_subject": primary,
        "secondary_subjects": secondary,
        "event": event_line,
        "location_if_supported": location,
        "visual_context": visual_context,
        "important_objects": important_objects,
        "mood": "serious, restrained, institutional reporting",
        "editorial_style": (
            "professional crypto/business newsroom hero image; realistic or polished "
            "editorial illustration; clear focal subject; clean composition; strong depth; "
            "not generic AI slop; not overdramatic; not clickbait; not meme-like"
        ),
        "forbidden_elements": list(FORBIDDEN_ELEMENTS),
        "facts_used_for_direction": facts,
        "source_headline": headline,
        "source_dek_truncated": dek[:220] if dek else None,
    }

    entities = _entity_names(article, evidence_packet)
    primary_entities = entities[:3]
    secondary_entities = entities[3:6]

    provider_prompt = (
        "Professional crypto/business newsroom hero image, artwork only, no writing of any kind. "
        "Premium financial-news editorial visual, 16:9 landscape. "
        f"Story-specific subject: {primary}. "
        f"Event: {event_line}. "
        "Clear focal subject, clean composition, strong depth, professional newsroom quality. "
        "Not generic AI slop, not overdramatic, not clickbait, not meme-like. "
        "Do not render headlines, captions, article text, ticker overlays, watermarks, fake "
        "publication logos, CoinNetwork logo, fake UI, fake charts with invented numbers, fake "
        "documents, named politicians, readable dollar figures, government seals, official emblems, "
        "agency logos, or institutional insignia. "
        "Unnamed people must remain generic — do not imply a specific real public figure. "
        "Avoid random floating crypto coins. Do not invent unsupported people or events."
    )

    visual = build_visual_brief(
        {
            "event_id": eid,
            "editorial_subject": headline or primary,
            "story_category": str(article.get("category") or "markets"),
            "visual_concept": visual_context,
            "primary_entities": primary_entities,
            "secondary_entities": secondary_entities,
            "event_action": event_line[:160] if event_line else None,
            "facts": [{"text": row, "kind": "fact"} for row in facts],
            "visual_metaphors": [
                {
                    "text": "restrained institutional financial-news editorial composition",
                    "kind": "visual_metaphor",
                    "not_a_factual_claim": True,
                }
            ],
            "scene": visual_context,
            "composition": "wide 16:9, one clear focal scene, clean negative space for later logo",
            "mood": story_brief["mood"],
            "lighting": "professional news-feature lighting with strong depth",
            "avoid": list(FORBIDDEN_ELEMENTS),
            "negative_prompt": (
                DEFAULT_NEGATIVE_PROMPT
                + ", headlines, captions, logos, CoinNetwork branding, readable text, "
                "tickers, fake charts, fake documents, politicians, dollar amounts, "
                "government seals, emblems, agency logos"
            ),
            "width": CARD_WIDTH,
            "height": CARD_HEIGHT,
            "artwork_only": list(ARTWORK_ONLY_INSTRUCTIONS),
            "provider_visual_prompt": provider_prompt,
        }
    )

    return {
        "story_image_brief": story_brief,
        "visual_brief": visual,
    }


def visual_brief_from_bundle(bundle: dict[str, Any]) -> VisualBrief:
    vb = bundle.get("visual_brief")
    if isinstance(vb, VisualBrief):
        return vb
    raise TypeError("bundle missing VisualBrief")
