from dataclasses import dataclass, asdict
from typing import Any


@dataclass
class NewsItem:
    source: str
    title: str
    url: str
    published: str | None
    summary: str

    source_type: str = "newsroom"
    source_role: str = "discovery"
    source_authority: float = 0.5

    score: float = 0.0
    fingerprint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)