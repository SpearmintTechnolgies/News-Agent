from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import NewsItem


# Source authority belongs to evidence quality, not event newsworthiness.
# Keep only a small neutral newsroom baseline in deterministic ranking.
NEWSROOM_BASELINE = 0.35

SPECULATIVE = (
    "could", "may", "might", "potential", "opportunity",
    "forecast", "forecasts", "prediction", "predicts",
    "target", "expects", "considers", "considering",
    "plans to", "seeking", "seeks", "should be seen",
)

SECURITY = (
    "hack", "hacked", "hacker", "hackers",
    "exploit", "exploited", "breach",
    "phishing", "ransom", "stolen",
    "vulnerability", "flaw", "attack", "attacks",
)

REGULATORY = (
    "sec",
    "regulator", "regulators",
    "regulation", "regulations", "regulatory",
    "court", "lawsuit", "bill",
    "clarity act",
    "esma",
    "house of lords",
    "supreme court",
    "policy",
)

MARKET = (
    "etf",
    "outflow", "outflows",
    "inflow", "inflows",
    "liquidation", "liquidations",
    "volume",
    "bond yield", "bond yields",
    "cpi", "ppi",
)

ADOPTION = (
    "launch", "launches", "launched",
    "adoption",
    "institutional",
    "custody",
    "stablecoin card",
    "visa stablecoin",
    "tokenized bond",
    "tokenized securities",
    "digital rupee",
    "infrastructure partner",
    "corporate treasury",
)

REALIZED = (
    "issued",
    "raised",
    "donated", "donation", "donations",
    "outflow", "outflows",
    "inflow", "inflows",
    "stolen", "lost",
    "hacked",
    "exploit", "exploited",
    "acquired",
    "bought", "sold",
    "invested",
    "launch", "launches", "launched",
    "resumes", "resumed",
    "cuts", "cut",
    "increases", "increased",
    "decreases", "decreased",
    "pull", "pulls", "pulled",
)


def age_hours(
    item: NewsItem,
    now: datetime | None = None,
) -> float | None:

    if not item.published:
        return None

    try:
        dt = parsedate_to_datetime(item.published)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        current = now or datetime.now(timezone.utc)

        return max(
            0.0,
            (current - dt.astimezone(timezone.utc)).total_seconds()
            / 3600,
        )

    except (TypeError, ValueError, OverflowError):
        return None


def freshness_score(hours: float | None) -> float:
    if hours is None:
        return -1.0
    if hours <= 6:
        return 4.0
    if hours <= 12:
        return 3.6
    if hours <= 24:
        return 3.0
    if hours <= 36:
        return 2.1
    if hours <= 48:
        return 1.2
    if hours <= 72:
        return 0.0
    return -2.0


def contains_term(text: str, term: str) -> bool:
    """
    Phrase/word-safe matching.

    Prevents examples such as:
      act -> activity
      sec -> second
      hack -> hackathon
    """
    pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(contains_term(text, term) for term in terms)


def money_magnitude(title: str) -> float:
    """
    Rank only money explicitly present in the headline.

    Source summaries may contain context, related-story text,
    or other numbers and must not inflate event magnitude.
    """
    text = title.lower()

    pattern = re.compile(
        r"(?:\$|€|£)\s*"
        r"(\d+(?:\.\d+)?)\s*"
        r"(trillion|billion|million|tn|bn|m|b)?",
        re.IGNORECASE,
    )

    best = 0.0

    for match in pattern.finditer(text):
        value = float(match.group(1))
        unit = (match.group(2) or "").lower()

        if unit in {"trillion", "tn"}:
            raw = 2.0
        elif unit in {"billion", "bn", "b"}:
            raw = 1.7
        elif unit in {"million", "m"}:
            if value >= 100:
                raw = 1.4
            elif value >= 10:
                raw = 1.0
            else:
                raw = 0.5
        else:
            raw = 0.2

        left = max(0, match.start() - 90)
        right = min(len(text), match.end() + 90)
        context = text[left:right]

        speculative = has_any(context, SPECULATIVE)
        realized = has_any(context, REALIZED)

        if realized:
            adjusted = raw
        elif speculative:
            adjusted = raw * 0.10
        else:
            adjusted = raw * 0.35

        best = max(best, adjusted)

    return min(best, 2.0)


def dimension_breakdown(item: NewsItem) -> dict[str, float]:
    # IMPORTANT:
    # Event classification is headline-first.
    # Summary remains evidence, not a ranking keyword bag.
    title = item.title.lower()

    security = 2.6 if has_any(title, SECURITY) else 0.0
    regulatory = 2.2 if has_any(title, REGULATORY) else 0.0
    market = 2.0 if has_any(title, MARKET) else 0.0
    adoption = 1.8 if has_any(title, ADOPTION) else 0.0

    speculative = has_any(title, SPECULATIVE)
    realized = has_any(title, REALIZED)

    speculation_penalty = (
        -1.4
        if speculative and not realized
        else 0.0
    )

    concrete_bonus = 0.6 if realized else 0.0

    return {
        "freshness": round(
            freshness_score(age_hours(item)),
            3,
        ),
        "security": security,
        "regulatory": regulatory,
        "market": market,
        "adoption": adoption,
        "magnitude": round(
            money_magnitude(item.title),
            3,
        ),
        "concrete": concrete_bonus,
        "source": (
            NEWSROOM_BASELINE
            if item.source_type == "newsroom"
            else 0.0
        ),
        "speculation": speculation_penalty,
    }


def deterministic_score(item: NewsItem) -> float:
    parts = dimension_breakdown(item)

    item.score = round(
        sum(parts.values()),
        3,
    )

    return item.score


def rank(items: list[NewsItem]) -> list[NewsItem]:
    for item in items:
        deterministic_score(item)

    return sorted(
        items,
        key=lambda x: x.score,
        reverse=True,
    )


def rank_clusters(clusters):
    for cluster in clusters:
        for member in cluster.members:
            deterministic_score(member)

        representative = max(
            cluster.members,
            key=lambda item: item.score,
        )

        cluster.representative = representative

        corroboration = min(
            1.6,
            max(0, cluster.source_count - 1) * 0.8,
        )

        cluster.event_score = round(
            representative.score + corroboration,
            3,
        )



    return sorted(
        clusters,
        key=lambda cluster: cluster.event_score,
        reverse=True,
    )
