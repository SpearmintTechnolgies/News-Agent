"""Per-website news niches.

Discovery still uses the shared crypto feeds, but the active site's niche
controls live-search queries and which stories pass TRENDING / HIGH TREND /
6 HOURS. Explicit WordPress category taps keep their own category filter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

NICHE_ENV = "NEWSAGENT_ACTIVE_SITE_NICHE"

# Broad crypto sites leave require_terms empty (current behaviour).
CRYPTO = "crypto"
ALTCOINS = "altcoins"


@dataclass(frozen=True)
class SiteNiche:
    key: str
    label: str
    require_terms: tuple[str, ...]
    search_rounds: tuple[tuple[str, ...], ...]


_ALTCOIN_TERMS: tuple[str, ...] = (
    "altcoin",
    "altcoins",
    "solana",
    "sol",
    "xrp",
    "ripple",
    "dogecoin",
    "doge",
    "bnb",
    "binance coin",
    "cardano",
    "ada",
    "avalanche",
    "avax",
    "polygon",
    "matic",
    "polkadot",
    "dot",
    "chainlink",
    "link",
    "litecoin",
    "ltc",
    "tron",
    "trx",
    "shiba",
    "shib",
    "pepe",
    "memecoin",
    "memecoins",
    "toncoin",
    "ton",
    "sui",
    "aptos",
    "near protocol",
    "cosmos",
    "atom",
    "stellar",
    "xlm",
    "hedera",
    "hbar",
    "injective",
    "render token",
    "arbitrum",
    "optimism",
    "base chain",
    "sei network",
    "fantom",
    "ftm",
    "monero",
    "xmr",
    "kaspa",
    "filecoin",
    "fil",
)

_ALTCOIN_SEARCH_ROUNDS: tuple[tuple[str, ...], ...] = (
    (
        "altcoin news today",
        "solana xrp cardano news",
        "memecoin crypto news",
    ),
    (
        "dogecoin pepe shiba news",
        "bnb avalanche polygon news",
        "ripple xrp lawsuit news",
    ),
    (
        "polkadot chainlink news",
        "toncoin sui aptos news",
        "altcoin market rally",
    ),
    (
        "tron stellar hedera news",
        "arbitrum optimism crypto news",
        "litecoin monero news",
    ),
    (
        "solana defi news",
        "cardano ada news today",
        "binance coin bnb news",
    ),
    (
        "kaspa filecoin news",
        "injective render crypto",
        "altcoin breaking news",
    ),
)

NICHES: dict[str, SiteNiche] = {
    CRYPTO: SiteNiche(
        key=CRYPTO,
        label="Crypto",
        require_terms=(),
        search_rounds=(),
    ),
    ALTCOINS: SiteNiche(
        key=ALTCOINS,
        label="Altcoins",
        require_terms=_ALTCOIN_TERMS,
        search_rounds=_ALTCOIN_SEARCH_ROUNDS,
    ),
}

# Hostname → niche key (no secrets; safe to ship).
HOST_NICHES: dict[str, str] = {
    "altcoininformer.com": ALTCOINS,
    "www.altcoininformer.com": ALTCOINS,
}


def niche_by_key(key: str) -> SiteNiche:
    return NICHES.get(str(key or "").strip().lower(), NICHES[CRYPTO])


def niche_for_host(host: str) -> SiteNiche:
    cleaned = (host or "").lower().strip()
    if cleaned.startswith("www."):
        alt = HOST_NICHES.get(cleaned)
        if alt:
            return niche_by_key(alt)
        cleaned = cleaned[4:]
    return niche_by_key(HOST_NICHES.get(cleaned, CRYPTO))


def niche_for_site(site: Any) -> SiteNiche:
    """Resolve niche from hostname, then from the site display name."""
    base = str(getattr(site, "base_url", "") or "")
    host = (urlsplit(base).hostname or "").lower()
    by_host = niche_for_host(host)
    if by_host.key != CRYPTO:
        return by_host
    name = str(getattr(site, "name", "") or "").lower()
    if "altcoin" in name:
        return NICHES[ALTCOINS]
    return NICHES[CRYPTO]


def active_niche(environ: dict[str, str] | None = None) -> SiteNiche:
    """Niche for the website currently selected in the bot."""
    env = environ if environ is not None else os.environ
    explicit = str(env.get(NICHE_ENV) or "").strip().lower()
    if explicit in NICHES:
        return NICHES[explicit]
    site_id = str(env.get("NEWSAGENT_ACTIVE_SITE_ID") or "").strip()
    if not site_id:
        return NICHES[CRYPTO]
    try:
        from newsagent_v2.control.sites import find_site

        site = find_site(site_id, environ=dict(env))
    except Exception:
        return NICHES[CRYPTO]
    if site is None:
        return NICHES[CRYPTO]
    return niche_for_site(site)


def event_matches_niche(event: Any, niche: SiteNiche | None = None) -> bool:
    """True when the story fits the niche (or the niche is unrestricted)."""
    profile = niche if niche is not None else active_niche()
    if not profile.require_terms:
        return True
    from newsagent_v2.control.story_picker import _blob, _contains

    text = _blob(event)
    return any(_contains(text, term) for term in profile.require_terms)


def niche_search_queries(niche: SiteNiche | None = None, *, round: int = 0) -> tuple[str, ...]:
    """Live-search queries for the active niche, or empty for broad crypto."""
    profile = niche if niche is not None else active_niche()
    rounds = profile.search_rounds
    if not rounds:
        return ()
    idx = max(0, int(round)) % len(rounds)
    return rounds[idx]


def category_relevance_score(event: Any, categories: list[dict[str, Any]] | None) -> int:
    """How strongly the story matches the site's WordPress categories (term hits)."""
    if not categories:
        return 0
    from newsagent_v2.control.story_picker import (
        _blob,
        _contains,
        category_terms_from_name,
        related_terms,
    )

    text = _blob(event)
    best = 0
    for row in categories:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        terms = category_terms_from_name(name) + related_terms(name)
        hits = sum(1 for term in terms if _contains(text, term))
        if hits > best:
            best = hits
    return best


def event_matches_site_categories(
    event: Any,
    categories: list[dict[str, Any]] | None,
    *,
    min_hits: int = 1,
) -> bool:
    """True when the story hits at least one site category (or no catalog yet)."""
    if not categories:
        return True
    return category_relevance_score(event, categories) >= max(1, int(min_hits))


def catalog_search_queries(
    categories: list[dict[str, Any]] | None,
    *,
    round: int = 0,
) -> tuple[str, ...]:
    """Build live-search queries from the site's WordPress category names."""
    if not categories:
        return ()
    from newsagent_v2.control.story_picker import category_queries

    collected: list[str] = []
    seen: set[str] = set()
    for row in categories:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        # Per-category queries (not HIGH TREND / trend) — niche-agnostic terms.
        for query in category_queries(name, round=0):
            key = query.casefold()
            if key in seen:
                continue
            seen.add(key)
            collected.append(query)
    if not collected:
        return ()
    start = (max(0, int(round)) * 3) % len(collected)
    window: list[str] = []
    for offset in range(min(3, len(collected))):
        window.append(collected[(start + offset) % len(collected)])
    return tuple(window)


def discovery_search_queries(*, round: int = 0) -> tuple[str, ...]:
    """Queries for TRENDING / HIGH TREND: niche first, then site categories."""
    niche_q = niche_search_queries(round=round)
    if niche_q:
        return niche_q
    site_id = str(os.environ.get("NEWSAGENT_ACTIVE_SITE_ID") or "").strip()
    if not site_id:
        return ()
    try:
        from newsagent_v2.control.site_flow import cached_categories

        return catalog_search_queries(cached_categories(site_id), round=round)
    except Exception:
        return ()


def discovery_allows_event(event: Any) -> bool:
    """Niche gate for TRENDING / HIGH TREND / 6 HOURS.

    Category relevance is applied when ranking (HIGH TREND) and when filing
    under a WordPress category (no match → skip and keep searching).
    """
    return event_matches_niche(event)
