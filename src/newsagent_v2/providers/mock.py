from __future__ import annotations

from .base import EditorialProvider


class MockEditorialProvider(EditorialProvider):
    """
    Zero-cost local placeholder.

    It deliberately mirrors the representative article rather than
    pretending to perform multi-source editorial synthesis.
    """

    def generate(self, evidence_pack: dict) -> dict:
        stories = []

        for event in evidence_pack.get("stories", []):
            representative = event.get("representative", {})

            title = representative.get("title", "")
            summary = representative.get("summary", "")

            stories.append(
                {
                    "event_id": event.get("event_id"),
                    "headline": title,
                    "summary": summary[:500],
                    "source_url": representative.get("url", ""),
                    "source": representative.get("source", ""),
                    "source_count": event.get("source_count", 0),
                    "has_primary_evidence": event.get(
                        "has_primary_evidence",
                        False,
                    ),
                    "visual_prompt": (
                        f"Editorial crypto news visual about: {title}"
                    ),
                }
            )

        return {
            "stories": stories,
            "provider": "mock",
            "cost_inr": 0.0,
        }