"""WriterProvider boundary. Adapters own provider-native schema and parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class FrozenStoryPackage:
    event_id: str
    article_input: dict[str, Any]
    other_article_inputs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_story(cls, story: dict[str, Any]) -> "FrozenStoryPackage":
        event_id = str(story.get("event_id") or "")
        article_input = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
        if not event_id:
            event_id = str(article_input.get("event_id") or "")
        others = story.get("other_article_inputs") if isinstance(story.get("other_article_inputs"), list) else []
        return cls(
            event_id=event_id,
            article_input=article_input,
            other_article_inputs=[row for row in others if isinstance(row, dict)],
        )


@dataclass
class ProviderWriterResult:
    ok: bool
    provider: str
    model: str
    native: dict[str, Any] | None = None
    http_status: int | None = None
    error: str | None = None


class WriterProvider(Protocol):
    provider_name: str
    model_name: str

    def build_request(self, story: FrozenStoryPackage) -> dict[str, Any]:
        """Provider-native HTTP JSON body. Must not include secrets."""

    def parse_response(self, payload: dict[str, Any]) -> ProviderWriterResult:
        """Parse provider-native JSON. No QA. No invented facts."""
