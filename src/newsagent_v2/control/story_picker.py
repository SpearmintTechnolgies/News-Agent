"""The message that asks which stories to fetch before a scan starts."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# The first row covers every category. The rest search one category's
# keywords and list matches from newest to oldest, with no age cutoff.
HIGH_TREND = "high_trend"
HIGH_TREND_TOTAL = 10
# Keep pulling fresh search rounds until the card quota is met (or this many tries).
MAX_SEARCH_ROUNDS = 6

_HIGH_TREND_QUERY_ROUNDS: tuple[tuple[str, ...], ...] = (
    (
        "crypto news today",
        "bitcoin ethereum breaking news",
        "cryptocurrency market latest",
    ),
    (
        "bitcoin etf news",
        "crypto regulation sec",
        "solana defi news",
    ),
    (
        "ethereum news today",
        "crypto exchange news",
        "altcoin market news",
    ),
    (
        "stablecoin news",
        "crypto funding acquisition",
        "bitcoin price news",
    ),
    (
        "ripple xrp lawsuit news",
        "crypto AI news",
        "web3 blockchain news",
    ),
    (
        "crypto hacking exploit news",
        "federal reserve bitcoin",
        "crypto treasury news",
    ),
)

CHOICES: tuple[tuple[str, str], ...] = (
    ("trend", "TRENDING"),
    (HIGH_TREND, "HIGH TREND"),
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
    "HIGH TREND — the 10 freshest high-traffic stories (multi-source momentum + SEO pull).\n"
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
    rows = [buttons[:3]]
    rest = buttons[3:]
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


_GENERIC_CATEGORY_WORDS = frozenset({
    "news", "latest", "crypto", "cryptocurrency", "the", "and", "for", "on", "vs", "of",
})


def category_terms_from_name(name: str) -> tuple[str, ...]:
    """Search words from a website's own category name."""
    words = re.findall(r"[a-z0-9]+", str(name or "").lower())
    return tuple(word for word in words if word not in _GENERIC_CATEGORY_WORDS and len(word) > 1)


def _stem_key(word: str) -> str:
    """animals → animal, memecoins → memecoin. Short words stay as written."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


# Words that already show up in headlines. Their category search stays literal.
_HEADLINE_WORDS = frozenset({
    "bitcoin", "btc", "ethereum", "ether", "eth", "solana", "xrp", "defi", "etf", "nft", "sec", "ai",
})

# Popular headline terms for a category word that outlets rarely print as-is.
_RELATED_TERMS: dict[str, tuple[str, ...]] = {
    "animal": ("dogecoin", "pepe", "shiba", "doge", "floki", "bonk"),
    "memecoin": ("meme coin", "memecoin", "meme", "dogecoin", "pepe", "shiba"),
    "meme": ("meme coin", "memecoin", "dogecoin", "pepe", "shiba"),
    "exploit": ("hack", "hacked", "exploit", "breach", "stolen", "drained"),
    "hack": ("hack", "hacked", "exploit", "breach", "stolen"),
    "etf": ("bitcoin etf", "ethereum etf"),
    "defi": ("uniswap", "aave", "dex"),
    "stablecoin": ("usdt", "usdc", "tether"),
    "nft": ("opensea",),
    "regulation": ("sec", "regulator", "lawsuit"),
    "ai": ("artificial intelligence", "openai"),
}


def related_terms(choice: str) -> tuple[str, ...]:
    """Popular terms for a site category, beyond the words in its name."""
    found: list[str] = []
    seen = {word.casefold() for word in category_terms_from_name(choice)}
    for word in category_terms_from_name(choice):
        for term in _RELATED_TERMS.get(_stem_key(word), ()):
            key = term.casefold()
            if key in seen:
                continue
            seen.add(key)
            found.append(term)
    return tuple(found)


def skips_age_cap(choice: str) -> bool:
    """Category scans keep older feed items. Trend buttons stay on the usual window."""
    return choice not in {"", "trend", HIGH_TREND, "h6"}


def category_queries(choice: str, *, round: int = 0) -> tuple[str, ...]:
    """News-search queries for one category / HIGH TREND / site-niche round.

    ``round`` 0 is the first pass; later rounds rotate alternate queries so a
    short card batch can keep searching until the quota is filled.

    TRENDING / HIGH TREND / 6 HOURS use the active site's niche queries (e.g.
    Altcoin Informer → altcoin terms) or that site's WordPress category catalog.
    Plain TRENDING on a broad crypto site stays RSS-only when both are empty.
    """
    if choice in {"", "trend", "h6", HIGH_TREND}:
        from newsagent_v2.control.site_niche import discovery_search_queries

        niche_or_catalog = discovery_search_queries(round=round)
        if niche_or_catalog:
            return niche_or_catalog
        if choice == HIGH_TREND:
            idx = max(0, int(round)) % len(_HIGH_TREND_QUERY_ROUNDS)
            return _HIGH_TREND_QUERY_ROUNDS[idx]
        return ()
    if choice not in CATEGORY_TERMS:
        terms = category_terms_from_name(choice)
        if not terms:
            base: tuple[str, ...] = (choice,)
        else:
            popular = related_terms(choice)
            literal_is_a_headline = any(
                word in _HEADLINE_WORDS or _stem_key(word) in _HEADLINE_WORDS for word in terms
            )
            if popular and not literal_is_a_headline:
                queries: list[str] = []
                for term in popular:
                    if term not in queries:
                        queries.append(term)
                    if len(queries) >= 6:
                        break
                base = tuple(queries)
            elif len(terms) == 1:
                base = (terms[0], f"{terms[0]} news", f"{terms[0]} crypto")
            else:
                base = (choice, " ".join(terms[:3]), f"{terms[0]} news")
    elif choice == "ai":
        base = ("crypto AI", "bitcoin artificial intelligence", "AI crypto news")
    else:
        lead = CATEGORY_TERMS[choice][0]
        if choice in {"bitcoin", "ethereum"}:
            base = (lead, f"{lead} news today", f"{lead} price")
        else:
            base = (f"{lead} crypto", f"{lead} news", lead)
    if not base:
        return ()
    start = (max(0, int(round)) * 3) % len(base)
    window: list[str] = []
    for offset in range(min(3, len(base))):
        window.append(base[(start + offset) % len(base)])
    return tuple(window)


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


def _intel_score(event: Any, attr: str) -> float:
    intel = getattr(event, "intelligence", None)
    if intel is None:
        return 0.0
    part = getattr(intel, attr, None)
    try:
        return float(getattr(part, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def traffic_seo_score(event: Any) -> float:
    """Higher means fresher multi-outlet stories that tend to earn search traffic.

    Uses coverage momentum (sources + velocity), breaking heat, and a hard
    recency preference. Title length in the SEO sweet spot gets a small bump.
    """
    try:
        sources = int(getattr(event, "source_count", 0) or 0)
    except (TypeError, ValueError):
        sources = 0
    try:
        age = float(getattr(event, "age_hours", 999) or 999)
    except (TypeError, ValueError):
        age = 999.0

    score = 0.0
    score += _intel_score(event, "momentum") * 3.0
    score += _intel_score(event, "breaking") * 2.5
    score += _intel_score(event, "novelty") * 1.0
    score += min(sources, 8) * 0.55

    if age <= 3:
        score += 4.0
    elif age <= 6:
        score += 3.0
    elif age <= 12:
        score += 1.8
    elif age <= 24:
        score += 0.8

    title = str(getattr(event, "canonical_title", "") or "").strip()
    words = len(title.split())
    if 6 <= words <= 14:
        score += 0.6
    elif 4 <= words <= 18:
        score += 0.25
    return score


def order_stories(events: list[Any], choice: str) -> list[Any]:
    """Trend keeps discovery rank. HIGH TREND reorders by category relevance + traffic. Categories are newest first."""
    if choice == HIGH_TREND:
        from newsagent_v2.control.site_niche import category_relevance_score
        import os

        site_id = str(os.environ.get("NEWSAGENT_ACTIVE_SITE_ID") or "").strip()
        categories: list[Any] = []
        if site_id:
            try:
                from newsagent_v2.control.site_flow import cached_categories

                categories = cached_categories(site_id)
            except Exception:
                categories = []

        return sorted(
            events,
            key=lambda event: (
                category_relevance_score(event, categories),
                traffic_seo_score(event),
                _recency(event),
            ),
            reverse=True,
        )
    if not skips_age_cap(choice):
        return list(events)
    return sorted(events, key=_recency, reverse=True)


def story_matches(event: Any, choice: str) -> bool:
    """True when this story belongs on the card list for the button that was pressed."""
    if choice in {"", "trend", HIGH_TREND}:
        from newsagent_v2.control.site_niche import discovery_allows_event

        return discovery_allows_event(event)
    if choice == "h6":
        if not published_within_hours(event, 6):
            return False
        from newsagent_v2.control.site_niche import discovery_allows_event

        return discovery_allows_event(event)
    terms = CATEGORY_TERMS.get(choice)
    if terms is None:
        terms = category_terms_from_name(choice) + related_terms(choice)
        if not terms:
            return True
    text = _blob(event)
    return any(_contains(text, term) for term in terms)
