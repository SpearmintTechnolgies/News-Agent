#!/usr/bin/env python3
"""detect_placement.py — infer placement type from parsed page signals."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class PlacementResult:
    placement_type: str
    placement_allowed: bool
    confidence: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


GUEST_POST_PHRASES = (
    "write for us",
    "guest post",
    "submit a post",
    "become a contributor",
)

URL_PATH_HINTS = (
    "write-for-us",
    "guest-post",
    "guest_post",
    "contribute",
    "submit-a-guest",
    "become-a-contributor",
)


def _url_title_hints(title: str | None, url: str | None) -> dict[str, bool]:
    haystack = f"{title or ''} {url or ''}".lower().replace("_", "-")
    guest = any(p in haystack for p in GUEST_POST_PHRASES)
    guest = guest or any(h in haystack for h in URL_PATH_HINTS)
    return {
        "guest_post_language": guest,
        "has_form": guest,
        "has_textarea_form": guest,
    }


def detect_placement(
    signals: dict[str, Any] | None,
    *,
    title: str | None = None,
    url: str | None = None,
) -> PlacementResult:
    """Map parser signals to a placement strategy."""
    signals = dict(signals or {})
    if not any(signals.values()):
        signals.update(_url_title_hints(title, url))

    if signals.get("guest_post_language") and signals.get("has_textarea_form"):
        return PlacementResult(
            placement_type="guest_post",
            placement_allowed=True,
            confidence=0.9,
            rationale="Guest-post language with textarea submission form",
        )

    if signals.get("guest_post_language") and signals.get("has_form"):
        return PlacementResult(
            placement_type="guest_post",
            placement_allowed=True,
            confidence=0.75,
            rationale="Guest-post language with form",
        )

    if signals.get("comment_language") and signals.get("has_form"):
        return PlacementResult(
            placement_type="comment",
            placement_allowed=True,
            confidence=0.7,
            rationale="Comment language with form",
        )

    if signals.get("has_textarea_form"):
        return PlacementResult(
            placement_type="form_submission",
            placement_allowed=True,
            confidence=0.55,
            rationale="Generic form with textarea",
        )

    if signals.get("has_form"):
        return PlacementResult(
            placement_type="form_submission",
            placement_allowed=True,
            confidence=0.45,
            rationale="Generic form detected",
        )

    if signals.get("guest_post_language"):
        return PlacementResult(
            placement_type="guest_post",
            placement_allowed=True,
            confidence=0.5,
            rationale="Guest-post language without confirmed form",
        )

    return PlacementResult(
        placement_type="unknown",
        placement_allowed=False,
        confidence=0.2,
        rationale="No placement signals detected",
    )
