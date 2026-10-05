"""The message that asks which stories to fetch before a scan starts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# The first two buttons cover every category. The rest search one category's
# keywords and list matches from newest to oldest, with no age cutoff.
CHOICES: tuple[tuple[str, str], ...] = (
    ("trend", "TRENDING"),
    ("h6", "6 HOURS"),
    ("bitcoin", "Bitcoin"),
    ("ethereum", "Ethereum"),
    ("altcoins", "Altcoins"),
    ("markets", "Markets"),
    ("regulation", "Regulation"),
    ("policy", "Policy"),
    ("business", "Business"),
    ("defi", "DeFi"),
    ("stablecoins", "Stablecoins"),
    ("ai", "AI"),
)

CATEGORY_TERMS: dict[str, tuple[str, ...]] = {
    "bitcoin": ("bitcoin", "btc"),
    "ethereum": ("ethereum", "ether", "eth"),
    "altcoins": ("altcoin", "solana", "xrp", "ripple", "dogecoin", "doge", "bnb", "cardano", "ada"),
    "markets": ("etf", "nasdaq", "s&p", "rally", "selloff"),
    "regulation": ("sec", "cftc", "regulation", "regulator", "lawsuit"),
    "policy": ("policy", "congress", "senate", "legislation", "white house"),
    "business": ("funding", "acquisition", "ipo", "revenue", "treasury", "earnings"),
    "defi": ("defi", "uniswap", "aave", "dex"),
    "stablecoins": ("stablecoin", "usdt", "usdc", "tether"),
    "ai": ("ai", "artificial intelligence", "openai"),
}

MENU_TEXT = (
    "What should I cover?\n\n"
    "TRENDING — the top stories across every category.\n"
    "6 HOURS — those top stories, kept only when the earliest publisher is inside the last 6 hours.\n"
    "A category lists only that topic, newest first, as far back as the feeds go.\n"
    "RUN STORY still writes only the card you tap."
)


def choice_label(choice: str) -> str:
    for key, label in CHOICES:
        if key == choice:
            return label
    return choice


def menu_keyboard() -> dict[str, Any]:
    buttons = [{"text": label, "callback_data": f"pick:{key}"} for key, label in CHOICES]
    rows = [buttons[:2]]
    rest = buttons[2:]
    rows.extend(rest[i:i + 2] for i in range(0, len(rest), 2))
    return {"inline_keyboard": rows}


def _blob(event: Any) -> str:
    parts = [
        str(getattr(event, "canonical_title", "") or ""),
        str(getattr(event, "topic", "") or ""),
    ]
    for report in getattr(event, "reports", []) or []:
        parts.append(str(getattr(report, "title", "") or ""))
        topics = getattr(report, "topics", None) or []
        parts.extend(str(topic) for topic in topics)
    return " ".join(parts).lower()


def _contains(text: str, term: str) -> bool:
    if " " in term:
        return term in text
    return re.search(rf"\b{re.escape(term)}\b", text) is not None


def skips_age_cap(choice: str) -> bool:
    """Category scans keep older feed items. Trend and 6 hours stay on the usual window."""
    return choice in CATEGORY_TERMS


def category_queries(choice: str) -> tuple[str, ...]:
    """News-search queries for one category. Empty for the time-window buttons."""
    if choice not in CATEGORY_TERMS:
        return ()
    if choice == "ai":
        return ("crypto AI", "bitcoin artificial intelligence")
    lead = CATEGORY_TERMS[choice][0]
    if choice in {"bitcoin", "ethereum"}:
        return (lead,)
    return (f"{lead} crypto",)


def _parse_time(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def earliest_published(event: Any) -> datetime | None:
    """When the first outlet published this story."""
    earliest: datetime | None = None
    for report in getattr(event, "reports", None) or []:
        published = _parse_time(getattr(report, "published_at", None))
        if published is not None and (earliest is None or published < earliest):
            earliest = published
    return earliest


def published_within_hours(event: Any, hours: float) -> bool:
    """True when the earliest publisher is inside the window.

    Falls back to age_hours, which is that same earliest time, when the
    reports themselves are not attached.
    """
    earliest = earliest_published(event)
    if earliest is not None:
        return (datetime.now(timezone.utc) - earliest).total_seconds() <= hours * 3600
    try:
        return float(getattr(event, "age_hours", 999)) <= hours
    except (TypeError, ValueError):
        return False


def _recency(event: Any) -> float:
    """Larger means newer. Missing times sort last."""
    earliest = earliest_published(event)
    if earliest is not None:
        return earliest.timestamp()
    try:
        age = float(getattr(event, "age_hours"))
    except (TypeError, ValueError):
        return float("-inf")
    return -age


def order_stories(events: list[Any], choice: str) -> list[Any]:
    """Trend and 6 hours keep the trending rank. A category is newest first."""
    if not skips_age_cap(choice):
        return list(events)
    return sorted(events, key=_recency, reverse=True)


def story_matches(event: Any, choice: str) -> bool:
    """True when this story belongs on the card list for the button that was pressed."""
    if choice in {"", "trend"}:
        return True
    if choice == "h6":
        return published_within_hours(event, 6)
    terms = CATEGORY_TERMS.get(choice)
    if not terms:
        return False
    text = _blob(event)
    return any(_contains(text, term) for term in terms)
