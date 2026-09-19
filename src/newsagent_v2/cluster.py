from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

from .models import NewsItem
from .dedupe import normalize_title


ENTITY_STOP = {
    "bitcoin", "crypto", "cryptocurrency", "blockchain",
    "market", "markets", "digital", "asset", "assets",
    "token", "tokens", "tokenized", "tokenization",
    "stablecoin", "defi", "etf",
}

WORD_STOP = {
    "says", "said", "report", "reports", "new", "amid",
    "after", "over", "into", "from", "with", "will",
    "could", "would", "may", "more", "than", "this",
    "that", "their", "about",
}


@dataclass
class EventCluster:
    event_id: str
    representative: NewsItem
    members: list[NewsItem] = field(default_factory=list)
    similarity_reason: str = ""
    event_score: float = 0.0

    @property
    def sources(self) -> list[str]:
        return sorted({item.source for item in self.members})

    @property
    def source_count(self) -> int:
        return len(self.sources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "representative": self.representative.to_dict(),
            "members": [item.to_dict() for item in self.members],
            "sources": self.sources,
            "source_count": self.source_count,
            "similarity_reason": self.similarity_reason,
            "event_score": self.event_score,
        }


def title_tokens(title: str) -> set[str]:
    return {
        token
        for token in normalize_title(title).split()
        if len(token) >= 3 and token not in WORD_STOP
    }


def numbers(text: str) -> set[str]:
    """
    Normalize monetary amounts so equivalent forms match:
    $97M == $97 million
    $13B == $13 billion
    """
    signatures: set[str] = set()

    money_pattern = re.compile(
        r"(?P<currency>[$€£])\s*"
        r"(?P<value>\d+(?:\.\d+)?)\s*"
        r"(?P<unit>trillion|billion|million|tn|bn|m|b)?\b",
        re.IGNORECASE,
    )

    currency_names = {
        "$": "USD",
        "€": "EUR",
        "£": "GBP",
    }

    multipliers = {
        "trillion": 1_000_000_000_000,
        "tn": 1_000_000_000_000,
        "billion": 1_000_000_000,
        "bn": 1_000_000_000,
        "b": 1_000_000_000,
        "million": 1_000_000,
        "m": 1_000_000,
    }

    money_spans: list[tuple[int, int]] = []

    for match in money_pattern.finditer(text):
        currency = currency_names[match.group("currency")]
        value = float(match.group("value"))
        unit = (match.group("unit") or "").lower()
        multiplier = multipliers.get(unit, 1)

        normalized = round(value * multiplier)

        signatures.add(f"{currency}:{normalized}")
        money_spans.append(match.span())

    # Preserve other concrete numbers too, but don't duplicate digits
    # that were already part of a money expression.
    for match in re.finditer(r"\b\d+(?:\.\d+)?%?\b", text):
        if any(
            start <= match.start() < end
            for start, end in money_spans
        ):
            continue

        signatures.add(f"NUM:{match.group(0).lower()}")

    return signatures

def probable_entities(title: str) -> set[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]+", title)

    entities: set[str] = set()

    for word in words:
        lower = word.lower()

        if lower in ENTITY_STOP or lower in WORD_STOP:
            continue

        # Acronyms/tickers.
        if word.isupper() and 2 <= len(word) <= 12:
            entities.add(lower)
            continue

        # Named entities. Ignore sentence-leading generic words later
        # through the token-overlap requirements.
        if word[:1].isupper() and len(word) >= 3:
            entities.add(lower)

    return entities


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0

    return len(a & b) / len(a | b)


def should_merge(a: NewsItem, b: NewsItem) -> tuple[bool, str]:
    ta = title_tokens(a.title)
    tb = title_tokens(b.title)

    ea = probable_entities(a.title)
    eb = probable_entities(b.title)

    na = numbers(a.title)
    nb = numbers(b.title)

    token_overlap = jaccard(ta, tb)
    entity_overlap = ea & eb
    number_overlap = na & nb

    fuzzy = token_set_ratio(
        normalize_title(a.title),
        normalize_title(b.title),
    )

    # Concrete conflicting numbers are evidence that two superficially
    # similar headlines may describe different events.
    conflicting_numbers = bool(na and nb and not number_overlap)

    # Very strong textual match.
    if fuzzy >= 88 and not conflicting_numbers:
        return True, f"strong_title:{fuzzy:.0f}"

    # Same named subject + substantial wording overlap.
    if entity_overlap and fuzzy >= 72 and token_overlap >= 0.30:
        if not conflicting_numbers:
            return (
                True,
                f"entity+title:{','.join(sorted(entity_overlap))}:{fuzzy:.0f}",
            )

    # Matching concrete number + named subject is powerful evidence.
    if number_overlap and entity_overlap and fuzzy >= 62:
        return (
            True,
            f"entity+number:{','.join(sorted(number_overlap))}:{fuzzy:.0f}",
        )

    return False, ""


def cluster_events(items: list[NewsItem]) -> list[EventCluster]:
    clusters: list[EventCluster] = []

    for item in items:
        matched: EventCluster | None = None
        reason = ""

        for cluster in clusters:
            # Compare against every member, not only the representative.
            for member in cluster.members:
                merge, why = should_merge(item, member)

                if merge:
                    matched = cluster
                    reason = why
                    break

            if matched is not None:
                break

        if matched is None:
            clusters.append(
                EventCluster(
                    event_id=f"event-{len(clusters) + 1:03d}",
                    representative=item,
                    members=[item],
                    similarity_reason="singleton",
                )
            )
        else:
            matched.members.append(item)

            if matched.similarity_reason == "singleton":
                matched.similarity_reason = reason

    return clusters

