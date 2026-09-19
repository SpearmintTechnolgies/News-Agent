from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .models import NewsItem


GENERIC_ROUNDUPS = (
    "what happened in crypto today",
    "daily crypto roundup",
    "daily crypto recap",
)

BLOCKED_CONTENT = (
    "sponsored",
    "press release",
    "giveaway",
    "casino",
)

# Clear crypto/blockchain relevance.
STRONG_CRYPTO_SIGNALS = (
    "bitcoin", "btc",
    "ethereum", "ether", "eth",
    "crypto", "cryptocurrency",
    "blockchain",
    "stablecoin",
    "defi",
    "onchain", "on-chain",
    "coinbase", "binance",
    "ripple", "rlusd",
    "solana", "xrp", "dogecoin",
    "metaplanet", "hyperliquid",
    "trezor",
    "blockstream", "liquid network",
    "anchorage digital",
    "zodia custody",
    "bitcoin suisse",
    "bitwise",
    "sbf", "sam bankman-fried",
    "clarity act",
)

# Technologies/topics that are highly relevant to CoinNetwork,
# but can appear in traditional-finance stories too.
ADJACENT_TECH_SIGNALS = (
    "tokenize", "tokenizes", "tokenized", "tokenizing",
    "tokenise", "tokenises", "tokenised", "tokenising",
    "tokenization", "tokenisation",
    "stock token", "stock tokens",
    "digital asset", "digital assets",
    "digital rupee",
    "central bank digital currency",
    "cbdc",
    "wallet",
    "custody",
)

# Finance/policy/security context that strengthens an adjacent story.
CONTEXT_SIGNALS = (
    "sec",
    "regulator", "regulators",
    "regulation", "regulatory",
    "securities",
    "treasury",
    "exchange",
    "etf",
    "bond", "bonds",
    "settlement",
    "infrastructure",
    "institutional",
    "hack", "hacked",
    "exploit",
    "breach",
    "phishing",
    "data exposed",
    "ransom",
)

# Named companies/entities where an otherwise ambiguous headline may
# still deserve conditional review when crypto context exists in the
# title/summary.
CRYPTO_LINKED_ENTITIES = (
    "robinhood",
    "revolut",
    "moneygram",
    "kalshi",
    "uniswap",
    "tether",
    "circle",
    "strategy",
    "microstrategy",
)


def published_datetime(item: NewsItem):
    if not item.published:
        return None

    try:
        dt = parsedate_to_datetime(item.published)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc)

    except (TypeError, ValueError, OverflowError):
        return None


def age_hours(item: NewsItem, now=None):
    dt = published_datetime(item)

    if dt is None:
        return None

    current = now or datetime.now(timezone.utc)

    return max(
        0.0,
        (current - dt).total_seconds() / 3600,
    )


def freshness_bucket(hours):
    if hours is None:
        return "unknown"
    if hours <= 6:
        return "0-6h"
    if hours <= 12:
        return "6-12h"
    if hours <= 24:
        return "12-24h"
    if hours <= 48:
        return "24-48h"
    return "48h+"


def contains_signal(
    text: str,
    signals: tuple[str, ...],
) -> bool:

    lowered = text.lower()

    for signal in signals:
        if re.search(
            r"(?<![a-z0-9])"
            + re.escape(signal)
            + r"(?![a-z0-9])",
            lowered,
        ):
            return True

    return False


def classify_candidate(item: NewsItem) -> tuple[str, str]:
    """
    Returns:
        ("keep", reason)
        ("conditional", reason)
        ("reject", reason)

    Conditional candidates continue into clustering/ranking.
    """

    title = item.title.lower()
    text = f"{item.title} {item.summary}".lower()

    for pattern in GENERIC_ROUNDUPS:
        if pattern in title:
            return "reject", "generic_roundup"

    for pattern in BLOCKED_CONTENT:
        if pattern in text:
            return "reject", f"blocked:{pattern}"

    title_strong = contains_signal(
        title,
        STRONG_CRYPTO_SIGNALS,
    )

    if title_strong:
        return "keep", "strong_crypto_title"

    body_strong = contains_signal(
        text,
        STRONG_CRYPTO_SIGNALS,
    )

    adjacent = contains_signal(
        text,
        ADJACENT_TECH_SIGNALS,
    )

    context = contains_signal(
        text,
        CONTEXT_SIGNALS,
    )

    linked_entity = contains_signal(
        text,
        CRYPTO_LINKED_ENTITIES,
    )

    # Explicit tokenization/digital-asset infrastructure is relevant
    # enough to survive deterministic filtering even when the headline
    # does not literally say "crypto".
    if adjacent and context:
        return "conditional", "adjacent_tech+context"

    # Crypto-linked company plus crypto/blockchain context in the
    # article should survive for ranking/editorial judgment.
    if linked_entity and (body_strong or adjacent or context):
        return "conditional", "linked_entity+crypto_context"

    # Less-explicit headline, but body clearly places it inside the
    # crypto ecosystem.
    if body_strong and context:
        return "conditional", "body_crypto+context"

    return "reject", "low_crypto_relevance"


def rejection_reason(item: NewsItem):
    """
    Compatibility helper for older callers.

    Conditional candidates are NOT rejected.
    """
    status, reason = classify_candidate(item)

    if status == "reject":
        return reason

    return None


def filter_candidates(items: list[NewsItem]):
    kept: list[NewsItem] = []
    rejected: list[dict] = []

    # Stored on the function so build_diagnostics can report the
    # classification without changing main.py's existing interface.
    decisions: list[dict] = []

    for item in items:
        status, reason = classify_candidate(item)

        decisions.append({
            "source": item.source,
            "title": item.title,
            "status": status,
            "reason": reason,
        })

        if status == "reject":
            rejected.append({
                "source": item.source,
                "title": item.title,
                "reason": reason,
            })
        else:
            kept.append(item)

    filter_candidates.last_decisions = decisions

    return kept, rejected


filter_candidates.last_decisions = []


def build_diagnostics(
    items: list[NewsItem],
    source_health: dict,
    rejected: list[dict],
):
    source_counts = Counter(
        item.source
        for item in items
    )

    freshness = Counter()
    ages = []

    for item in items:
        hours = age_hours(item)

        freshness[freshness_bucket(hours)] += 1

        if hours is not None:
            ages.append((hours, item))

    newest = None
    oldest = None

    if ages:
        newest_item = min(
            ages,
            key=lambda pair: pair[0],
        )[1]

        oldest_item = max(
            ages,
            key=lambda pair: pair[0],
        )[1]

        newest = {
            "source": newest_item.source,
            "title": newest_item.title,
            "published": newest_item.published,
        }

        oldest = {
            "source": oldest_item.source,
            "title": oldest_item.title,
            "published": oldest_item.published,
        }

    rejection_counts = Counter(
        entry["reason"]
        for entry in rejected
    )

    decisions = list(
        getattr(
            filter_candidates,
            "last_decisions",
            [],
        )
    )

    classification_counts = Counter(
        entry["status"]
        for entry in decisions
    )

    conditional = [
        entry
        for entry in decisions
        if entry["status"] == "conditional"
    ]

    return {
        "collected": len(items),

        "source_counts": dict(source_counts),

        "source_health": source_health,

        "freshness": {
            "0-6h": freshness["0-6h"],
            "6-12h": freshness["6-12h"],
            "12-24h": freshness["12-24h"],
            "24-48h": freshness["24-48h"],
            "48h+": freshness["48h+"],
            "unknown": freshness["unknown"],
        },

        "newest": newest,
        "oldest": oldest,

        "classification_counts": {
            "keep": classification_counts["keep"],
            "conditional": classification_counts["conditional"],
            "reject": classification_counts["reject"],
        },

        "conditional_count": len(conditional),
        "conditional": conditional,

        "rejected_count": len(rejected),
        "rejection_counts": dict(rejection_counts),
        "rejected": rejected,
    }

