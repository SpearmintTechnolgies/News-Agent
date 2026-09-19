"""
Deterministic visual-brief construction from structured event evidence.

No LLM is used. Facts and visual metaphors are stored as separate layers
so a metaphor cannot be persisted as a factual claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.image.contract import (
    ARTWORK_ONLY_INSTRUCTIONS,
    CARD_ASPECT_RATIO,
    CARD_HEIGHT,
    CARD_WIDTH,
    DEFAULT_NEGATIVE_PROMPT,
    IMAGE_BRIEF_SCHEMA_VERSION,
    REQUIRED_BRIEF_FIELDS,
)


class VisualBriefError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _copy_str_list(values: Any) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise VisualBriefError("invalid_string_list", "expected a list of strings")
    out: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise VisualBriefError("invalid_string_list", "list items must be non-empty strings")
        out.append(item.strip())
    return out


def _copy_fact_rows(values: Any, *, layer: str) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list):
        raise VisualBriefError("invalid_layer", f"{layer} must be a list")
    rows: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            raise VisualBriefError("invalid_layer", f"{layer} items must be objects")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise VisualBriefError("invalid_layer", f"{layer} item missing text")
        kind = item.get("kind", layer.rstrip("s"))
        if kind in {"fact", "visual_metaphor"} and kind != (
            "fact" if layer == "facts" else "visual_metaphor"
        ):
            raise VisualBriefError(
                "layer_mismatch",
                f"{layer} cannot contain kind={kind!r}",
            )
        row = {
            "text": text.strip(),
            "kind": "fact" if layer == "facts" else "visual_metaphor",
            "not_a_factual_claim": layer != "facts",
        }
        if item.get("entity") is not None:
            row["entity"] = item["entity"]
        if item.get("direction") is not None:
            row["direction"] = item["direction"]
        if item.get("amount") is not None:
            row["amount"] = item["amount"]
        if item.get("evidence_ref") is not None:
            row["evidence_ref"] = item["evidence_ref"]
        rows.append(row)
    return rows


@dataclass(frozen=True)
class VisualBrief:
    event_id: str
    editorial_subject: str
    visual_concept: str
    schema_version: str = IMAGE_BRIEF_SCHEMA_VERSION
    story_category: str | None = None
    primary_entities: list[str] = field(default_factory=list)
    secondary_entities: list[str] = field(default_factory=list)
    event_action: str | None = None
    financial_direction: list[dict[str, Any]] | None = None
    facts: list[dict[str, Any]] = field(default_factory=list)
    visual_metaphors: list[dict[str, Any]] = field(default_factory=list)
    scene: str | None = None
    composition: str | None = None
    mood: str | None = None
    lighting: str | None = None
    palette_guidance: str | None = None
    symbol_guidance: str | None = None
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    aspect_ratio: str = CARD_ASPECT_RATIO
    width: int = CARD_WIDTH
    height: int = CARD_HEIGHT
    artwork_only: tuple[str, ...] = ARTWORK_ONLY_INSTRUCTIONS
    avoid: list[str] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provider_visual_prompt: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "story_category": self.story_category,
            "editorial_subject": self.editorial_subject,
            "primary_entities": list(self.primary_entities),
            "secondary_entities": list(self.secondary_entities),
            "event_action": self.event_action,
            "financial_direction": (
                None
                if self.financial_direction is None
                else [dict(row) for row in self.financial_direction]
            ),
            "facts": [dict(row) for row in self.facts],
            "visual_metaphors": [dict(row) for row in self.visual_metaphors],
            "visual_concept": self.visual_concept,
            "scene": self.scene,
            "composition": self.composition,
            "mood": self.mood,
            "lighting": self.lighting,
            "palette_guidance": self.palette_guidance,
            "symbol_guidance": self.symbol_guidance,
            "negative_prompt": self.negative_prompt,
            "aspect_ratio": self.aspect_ratio,
            "width": self.width,
            "height": self.height,
            "artwork_only": list(self.artwork_only),
            "avoid": list(self.avoid),
            "evidence": [dict(row) for row in self.evidence],
            "provider_visual_prompt": self.provider_visual_prompt,
        }

    def artwork_prompt(self) -> str:
        """Prompt sent to image backends. Facts stay on the brief; they are not dumped here."""
        if self.provider_visual_prompt and self.provider_visual_prompt.strip():
            return " ".join(self.provider_visual_prompt.split())
        fact_lines = "; ".join(row["text"] for row in self.facts)
        metaphor_lines = "; ".join(row["text"] for row in self.visual_metaphors)
        parts = [
            "Textless cinematic institutional-finance photograph, artwork only.",
            "Do not write, type, or inscribe anything in the frame.",
        ]
        if self.visual_concept:
            parts.append(f"Visual concept: {self.visual_concept}.")
        if self.event_action:
            parts.append(f"Action: {self.event_action}.")
        if self.scene:
            parts.append(f"Scene: {self.scene}.")
        if self.composition:
            parts.append(f"Composition: {self.composition}.")
        if self.mood:
            parts.append(f"Mood: {self.mood}.")
        if self.lighting:
            parts.append(f"Lighting: {self.lighting}.")
        if self.palette_guidance:
            parts.append(f"Palette: {self.palette_guidance}.")
        if self.symbol_guidance:
            parts.append(f"Symbols: {self.symbol_guidance}.")
        if fact_lines:
            parts.append(
                "Metadata facts must not appear as text, numerals, charts, or tickers "
                "in the artwork. Do not paint any dollar amounts, dates, headlines, "
                "or article copy."
            )
        if metaphor_lines:
            parts.append(
                "Visual metaphors (atmosphere only, not new facts): "
                f"{metaphor_lines}."
            )
        if self.avoid:
            parts.append("Avoid: " + "; ".join(self.avoid) + ".")
        parts.extend(self.artwork_only)
        return " ".join(parts)


def validate_brief_dict(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise VisualBriefError("invalid_brief", "brief must be an object")
    for name in REQUIRED_BRIEF_FIELDS:
        if name not in payload:
            raise VisualBriefError("missing_field", f"brief missing {name}")
    if payload.get("schema_version") != IMAGE_BRIEF_SCHEMA_VERSION:
        raise VisualBriefError("schema_version", "unexpected image brief schema_version")
    if not payload.get("artwork_only"):
        raise VisualBriefError("artwork_only", "artwork-only instructions are required")
    forbidden_hits = (
        "headline",
        "captions",
        "article text",
        "coinnetwork logo",
        "publication logo",
        "watermark",
        "random letters",
        "fake ui",
        "illegible pseudo-text",
        "source-publication branding",
    )
    blob = " ".join(str(item).lower() for item in payload["artwork_only"])
    for needle in forbidden_hits:
        if needle not in blob:
            raise VisualBriefError(
                "artwork_only",
                f"artwork-only instructions must mention {needle!r}",
            )
    for row in payload.get("visual_metaphors") or []:
        if not isinstance(row, dict) or row.get("not_a_factual_claim") is not True:
            raise VisualBriefError(
                "metaphor_as_fact",
                "visual metaphors must set not_a_factual_claim true",
            )
        if row.get("kind") == "fact":
            raise VisualBriefError("metaphor_as_fact", "metaphor cannot be kind=fact")


def build_visual_brief(source: dict[str, Any]) -> VisualBrief:
    """Map structured event/evidence input onto VisualBrief. No LLM."""
    if not isinstance(source, dict):
        raise VisualBriefError("invalid_source", "brief source must be an object")
    event_id = source.get("event_id")
    subject = source.get("editorial_subject")
    concept = source.get("visual_concept")
    if not isinstance(event_id, str) or not event_id.strip():
        raise VisualBriefError("missing_event_id", "event_id is required")
    if not isinstance(subject, str) or not subject.strip():
        raise VisualBriefError("missing_subject", "editorial_subject is required")
    if not isinstance(concept, str) or not concept.strip():
        raise VisualBriefError("missing_concept", "visual_concept is required")

    width = int(source.get("width") or CARD_WIDTH)
    height = int(source.get("height") or CARD_HEIGHT)
    if width < 1 or height < 1:
        raise VisualBriefError("invalid_dimensions", "width and height must be positive")

    facts = _copy_fact_rows(source.get("facts"), layer="facts")
    metaphors = _copy_fact_rows(source.get("visual_metaphors"), layer="visual_metaphors")
    financial = source.get("financial_direction")
    financial_rows = None
    if financial is not None:
        financial_rows = _copy_fact_rows(financial, layer="facts")
        for row in financial_rows:
            row["kind"] = "fact"

    evidence = source.get("evidence") or []
    if evidence and not isinstance(evidence, list):
        raise VisualBriefError("invalid_evidence", "evidence must be a list")

    brief = VisualBrief(
        event_id=event_id.strip(),
        editorial_subject=subject.strip(),
        visual_concept=concept.strip(),
        story_category=source.get("story_category"),
        primary_entities=_copy_str_list(source.get("primary_entities")),
        secondary_entities=_copy_str_list(source.get("secondary_entities")),
        event_action=source.get("event_action"),
        financial_direction=financial_rows,
        facts=facts,
        visual_metaphors=metaphors,
        scene=source.get("scene"),
        composition=source.get("composition"),
        mood=source.get("mood"),
        lighting=source.get("lighting"),
        palette_guidance=source.get("palette_guidance"),
        symbol_guidance=source.get("symbol_guidance"),
        negative_prompt=source.get("negative_prompt") or DEFAULT_NEGATIVE_PROMPT,
        aspect_ratio=source.get("aspect_ratio") or CARD_ASPECT_RATIO,
        width=width,
        height=height,
        avoid=_copy_str_list(source.get("avoid")),
        evidence=[dict(row) for row in evidence] if evidence else [],
        provider_visual_prompt=(
            source["provider_visual_prompt"].strip()
            if isinstance(source.get("provider_visual_prompt"), str)
            and source.get("provider_visual_prompt").strip()
            else None
        ),
    )
    validate_brief_dict(brief.as_dict())
    return brief
