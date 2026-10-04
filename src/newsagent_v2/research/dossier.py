"""Research dossier data model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

FULL_SOURCE_MIN_WORDS = 150

ORIGIN_CLUSTER = "cluster"
ORIGIN_SEARCH = "news_search"
ORIGIN_OUTLINK = "outlink"

KIND_PRIMARY = "primary"
KIND_NEWS = "news"


def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().removeprefix("www.")
    except ValueError:
        return ""


def display_publisher(name: str) -> str:
    """Readable outlet name for prose: "finance.biggo.com" -> "Biggo", "CryptoTicker.io" -> "CryptoTicker"."""
    name = (name or "").strip()
    labels = name.lower().removeprefix("www.").split(".")
    if " " in name or len(labels) < 2 or not all(labels):
        return name
    original = name.split(".")
    brand_index = len(labels) - 3 if len(labels) >= 3 and labels[-2] in {"co", "com", "org", "gov"} else len(labels) - 2
    brand = original[max(brand_index, 0)]
    return brand if any(c.isupper() for c in brand) else brand.capitalize()


@dataclass
class SourceDoc:
    url: str
    publisher: str = ""
    title: str = ""
    published_at: str = ""
    author: str = ""
    paragraphs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    summary: str = ""
    origin: str = ORIGIN_CLUSTER
    kind: str = KIND_NEWS
    method: str = ""
    status: int = 0
    relevance: float = 0.0
    rejected_reason: str = ""

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def text(self) -> str:
        return "\n\n".join(self.paragraphs)

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)

    @property
    def is_full(self) -> bool:
        return not self.rejected_reason and self.word_count >= FULL_SOURCE_MIN_WORDS

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["word_count"] = self.word_count
        out["host"] = self.host
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceDoc":
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class ResearchDossier:
    event_id: str
    title: str
    entities: list[str] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    sources: list[SourceDoc] = field(default_factory=list)
    rejected: list[SourceDoc] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def full_sources(self) -> list[SourceDoc]:
        return [s for s in self.sources if s.is_full]

    @property
    def total_words(self) -> int:
        return sum(s.word_count for s in self.full_sources)

    @property
    def publishers(self) -> set[str]:
        return {s.host for s in self.full_sources if s.host}

    @property
    def primary_count(self) -> int:
        return sum(1 for s in self.full_sources if s.kind == KIND_PRIMARY)

    def stats(self) -> dict[str, Any]:
        return {
            "sources_kept": len(self.sources),
            "full_sources": len(self.full_sources),
            "independent_publishers": len(self.publishers),
            "primary_sources": self.primary_count,
            "total_words": self.total_words,
            "rejected": len(self.rejected),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "entities": list(self.entities),
            "queries": list(self.queries),
            "stats": self.stats(),
            "sources": [s.to_dict() for s in self.sources],
            "rejected": [s.to_dict() for s in self.rejected],
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ResearchDossier":
        return cls(
            event_id=str(data.get("event_id") or ""),
            title=str(data.get("title") or ""),
            entities=list(data.get("entities") or []),
            queries=list(data.get("queries") or []),
            sources=[SourceDoc.from_dict(s) for s in data.get("sources") or []],
            rejected=[SourceDoc.from_dict(s) for s in data.get("rejected") or []],
            notes=list(data.get("notes") or []),
        )
