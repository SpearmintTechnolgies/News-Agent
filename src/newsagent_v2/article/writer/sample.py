"""Deterministic article-first native samples for offline adapter tests. No LLM."""

from __future__ import annotations

from typing import Any


def sample_article_first_native(
    *,
    event_id: str,
    evidence_id: str,
    paragraphs: list[str] | None = None,
) -> dict[str, Any]:
    body_parts = paragraphs or [
        "Senate Republicans released revised CLARITY Act text as a final offer to Democrats.",
        "The 635-page proposal includes ethics provisions agreed by President Donald Trump.",
    ]
    claims = [
        {
            "id": "c1",
            "text": body_parts[0],
            "claim_type": "fact",
            "evidence_ids": [evidence_id],
        }
    ]
    if len(body_parts) > 1:
        claims.append(
            {
                "id": "c2",
                "text": body_parts[1],
                "claim_type": "fact",
                "evidence_ids": [evidence_id],
            }
        )
    maps = []
    for index, _text in enumerate(body_parts):
        claim_id = "c1" if index == 0 or len(claims) == 1 else "c2"
        maps.append({"paragraph_index": index, "claim_ids": [claim_id]})
    return {
        "event_id": event_id,
        "headline": "Senate Republicans release revised CLARITY Act text",
        "dek": "A 635-page proposal is offered before a procedural vote.",
        "article_body": "\n\n".join(body_parts),
        "category": "regulatory",
        "seo_title": "Senate Republicans release revised CLARITY Act text",
        "meta_description": "Republicans offered a 635-page CLARITY Act revision before a procedural vote.",
        "slug": "senate-republicans-revised-clarity-act",
        "entities": [{"name": "Cynthia Lummis", "type": "person"}],
        "keywords": ["CLARITY Act"],
        "claims": claims,
        "quotes": [],
        "paragraph_maps": maps,
    }
