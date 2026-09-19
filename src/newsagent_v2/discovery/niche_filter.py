"""Crypto Niche Filter - CODE-FIRST relevance filtering.

Deterministic filtering for crypto/blockchain news.
Zero GPT/LLM calls in normal path.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .raw_news_item import RawNewsItem


class RelevanceDecision(Enum):
    """Relevance classification."""
    KEEP = "keep"
    CONDITIONAL = "conditional"  # Keep but flag for review
    REJECT = "reject"


@dataclass
class NicheConfig:
    """Configuration for niche filtering.

    Attributes:
        strict_mode: Reject ambiguous items (default: keep with flag)
        min_confidence: Minimum confidence threshold for auto-accept
    """
    strict_mode: bool = False
    min_confidence: float = 0.5


# Strong crypto signals (headline = strong match)
STRONG_CRYPTO_SIGNALS = frozenset([
    "bitcoin", "btc",
    "ethereum", "ether", "eth",
    "crypto", "cryptocurrency",
    "blockchain",
    "stablecoin", "stablecoins",
    "defi",
    "web3",
    "coinbase", "binance",
    "ripple", "rlusd", "xrp",
    "solana", "dogecoin", "cardano",
    "metaplanet", "hyperliquid",
    "trezor", "ledger",
    "blockstream", "liquid network",
    "anchorage digital",
    "zodia custody",
    "bitcoin suisse",
    "bitwise",
    "sbf", "sam bankman-fried",
    "clarity act",
    "mt. gox", "mtgox",
    "etf approval", "etf rejection",
    "halving",
])

# Secondary crypto context (good for supporting evidence)
SECONDARY_SIGNALS = frozenset([
    "tokenized", "tokenization", "tokenise",
    "digital asset", "digital assets",
    "digital rupee",
    "cbdc", "central bank digital currency",
    "wallet", "custody",
    "seed phrase",
    "private key",
    "public key",
    "consensus",
    "validator", "validators",
    "node", "nodes",
])

# Finance/policy context
CONTEXT_SIGNALS = frozenset([
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
    "corporate treasury",
])

# Institutional entities
INSTITUTIONAL_SIGNALS = frozenset([
    "blackrock", "ishares",
    "fidelity", "fidelity investments",
    "ark invest",
    "vaneck",
    "wisdomtree",
    "invesco",
    "state street",
    "jp morgan", "jpmorgan",
    "goldman sachs",
    "morgan stanley",
])

# Trading/market
MARKET_SIGNALS = frozenset([
    "bull run", "bear market",
    "all time high", "ath",
    "support level", "resistance",
    "liquidation", "liquidations",
    "long squeeze", "short squeeze",
])

# Exclusions - generic uses of crypto terms
EXCLUSION_PATTERNS = [
    # Generic token uses
    (r"\btoken\s+of\s+appreciation", False),
    (r"\btoken\s+gesture", False),
    (r"\bblockchain\s+as\s+a\s+buzzword", False),
    (r"\bcrypto\s+means\s+hidden", False),  # cryptography not crypto
    (r"\bmining\s+for\s+(coal|gold|iron|diamonds?)\b", False),  # literal mining
]

# Generic roundups to exclude
GENERIC_ROUNDUPS = [
    "what happened in crypto today",
    "daily crypto roundup",
    "daily crypto recap",
    "crypto markets today",
    "bitcoin price today",
]

# Blocked content patterns
BLOCKED_CONTENT = [
    "sponsored",
    "press release",
    "giveaway",
    "casino",
    "airdrop",
    "promotion",
    "affiliate",
    "advertisement",
]


class NicheFilter:
    """Deterministic crypto relevance filter.

    CODE-FIRST approach:
    - Uses keywords, entity rules, topic rules
    - Zero LLM calls in normal path
    - Ambiguous items flagged for observation, not silently processed
    """

    def __init__(self, config: NicheConfig | None = None):
        self.config = config or NicheConfig()
        self.stats = {
            "checked": 0,
            "keep": 0,
            "conditional": 0,
            "reject": 0,
            "reasons": {},
        }

    def _contains_signal(self, text: str, signals: frozenset[str]) -> bool:
        """Check if text contains any signal words."""
        text_lower = text.lower()
        for signal in signals:
            # Word boundary check
            pattern = r"(?<![a-z0-9])" + re.escape(signal) + r"(?![a-z0-9])"
            if re.search(pattern, text_lower, re.IGNORECASE):
                return True
        return False

    def _check_exclusions(self, text: str) -> tuple[bool, str | None]:
        """Check if item matches exclusion patterns."""
        for pattern, allowed in EXCLUSION_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True, f"exclusion:{pattern}"
        return False, None

    def _check_generic_roundups(self, title: str) -> bool:
        """Check if item is a generic roundup."""
        title_lower = title.lower()
        for pattern in GENERIC_ROUNDUPS:
            if pattern in title_lower:
                return True
        return False

    def _check_blocked_content(self, text: str) -> tuple[bool, str | None]:
        """Check if item contains blocked content."""
        text_lower = text.lower()
        for pattern in BLOCKED_CONTENT:
            if pattern in text_lower:
                return True, f"blocked:{pattern}"
        return False, None

    def classify(self, item: RawNewsItem) -> tuple[RelevanceDecision, str, float]:
        """Classify item relevance.

        Returns:
            Tuple of (decision, reason, confidence)
        """
        self.stats["checked"] += 1

        title = item.headline.lower()
        text = f"{item.headline} {item.description}".lower()

        # Check blocked content
        is_blocked, block_reason = self._check_blocked_content(text)
        if is_blocked:
            self.stats["reject"] += 1
            self._record_reason(block_reason or "blocked")
            return RelevanceDecision.REJECT, block_reason or "blocked", 0.0

        # Check generic roundups
        if self._check_generic_roundups(title):
            self.stats["reject"] += 1
            self._record_reason("generic_roundup")
            return RelevanceDecision.REJECT, "generic_roundup", 0.0

        # Check exclusions
        is_excluded, exclusion_reason = self._check_exclusions(text)
        if is_excluded:
            self.stats["reject"] += 1
            self._record_reason(exclusion_reason or "excluded")
            return RelevanceDecision.REJECT, exclusion_reason or "excluded", 0.1

        # Strong crypto signal in headline = keep immediately
        if self._contains_signal(title, STRONG_CRYPTO_SIGNALS):
            self.stats["keep"] += 1
            self._record_reason("strong_crypto_title")
            return RelevanceDecision.KEEP, "strong_crypto_title", 0.95

        # Strong crypto signal in body
        if self._contains_signal(text, STRONG_CRYPTO_SIGNALS):
            # Check for context signals
            if self._contains_signal(text, CONTEXT_SIGNALS):
                self.stats["keep"] += 1
                self._record_reason("strong_crypto_body+context")
                return RelevanceDecision.KEEP, "strong_crypto_body+context", 0.85

            self.stats["conditional"] += 1
            self._record_reason("strong_crypto_body_no_context")
            return (RelevanceDecision.CONDITIONAL,
                    "strong_crypto_body_no_context", 0.65)

        # Secondary signals + context = conditional
        has_secondary = self._contains_signal(text, SECONDARY_SIGNALS)
        has_context = self._contains_signal(text, CONTEXT_SIGNALS)
        has_institutional = self._contains_signal(text, INSTITUTIONAL_SIGNALS)
        has_market = self._contains_signal(text, MARKET_SIGNALS)

        if has_secondary and has_context:
            self.stats["conditional"] += 1
            self._record_reason("secondary+context")
            return RelevanceDecision.CONDITIONAL, "secondary+context", 0.60

        # Institutional crypto
        if has_institutional and (has_secondary or has_context):
            self.stats["conditional"] += 1
            self._record_reason("institutional+crypto_context")
            return RelevanceDecision.CONDITIONAL, "institutional+crypto_context", 0.55

        # Market signals + entities
        if has_market and self._contains_signal(text, STRONG_CRYPTO_SIGNALS):
            self.stats["conditional"] += 1
            self._record_reason("market+crypto")
            return RelevanceDecision.CONDITIONAL, "market+crypto", 0.50

        # Low relevance
        self.stats["reject"] += 1
        self._record_reason("low_crypto_relevance")
        return RelevanceDecision.REJECT, "low_crypto_relevance", 0.15

    def filter_batch(
        self,
        items: list[RawNewsItem],
    ) -> tuple[list[RawNewsItem], list[RawNewsItem], list[tuple[RawNewsItem, str]]]:
        """Filter a batch of items.

        Returns:
            Tuple of (kept_items, conditional_items, rejected_items_with_reasons)
        """
        kept: list[RawNewsItem] = []
        conditional: list[RawNewsItem] = []
        rejected: list[tuple[RawNewsItem, str]] = []

        for item in items:
            decision, reason, confidence = self.classify(item)

            if decision == RelevanceDecision.KEEP:
                kept.append(item)
            elif decision == RelevanceDecision.CONDITIONAL:
                if self.config.strict_mode:
                    rejected.append((item, f"conditional_rejected:{reason}"))
                else:
                    conditional.append(item)
            else:
                rejected.append((item, reason))

        return kept, conditional, rejected

    def _record_reason(self, reason: str) -> None:
        """Record rejection reason for diagnostics."""
        self.stats["reasons"][reason] = self.stats["reasons"].get(reason, 0) + 1

    def get_stats(self) -> dict[str, Any]:
        """Get filter statistics."""
        return {
            "checked": self.stats["checked"],
            "keep": self.stats["keep"],
            "conditional": self.stats["conditional"],
            "reject": self.stats["reject"],
            "by_reason": dict(self.stats["reasons"]),
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self.stats = {
            "checked": 0,
            "keep": 0,
            "conditional": 0,
            "reject": 0,
            "reasons": {},
        }
