"""Shared provenance / source-type normalization for registry, expansion, research, factbank."""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Canonical SourceType values (match SourceRegistry enum).
CANONICAL_SOURCE_TYPES = frozenset({
    "crypto_publication",
    "official_regulator",
    "exchange",
    "crypto_company",
    "rss_feed",
    "api",
    "financial_news",
})

# Aliases seen in config / legacy code → canonical registry types.
SOURCE_TYPE_ALIASES: dict[str, str] = {
    "newsroom": "crypto_publication",
    "news": "crypto_publication",
    "press": "crypto_publication",
    "crypto": "crypto_publication",
    "publication": "crypto_publication",
    "regulator": "official_regulator",
    "official_regulator": "official_regulator",
    "government": "official_regulator",
    "official": "official_regulator",
    "gov": "official_regulator",
    "exchange": "exchange",
    "crypto_exchange": "exchange",
    "company": "crypto_company",
    "crypto_company": "crypto_company",
    "project": "crypto_company",
    "rss": "rss_feed",
    "rss_feed": "rss_feed",
    "feed": "rss_feed",
    "api": "api",
    "financial": "financial_news",
    "financial_news": "financial_news",
    "general_news": "financial_news",
    "wire": "financial_news",
}

# Types treated as primary / official evidence across research + factbank.
PRIMARY_SOURCE_TYPES = frozenset({
    "official_regulator",
    "regulator",
    "company",
    "crypto_company",
    "official",
    "government",
    "exchange",
})

PRIMARY_ROLES = frozenset({"primary_evidence"})

ROLE_ALIASES: dict[str, str] = {
    "discovery": "discovery",
    "primary": "primary_evidence",
    "primary_evidence": "primary_evidence",
    "secondary": "secondary_evidence",
    "secondary_evidence": "secondary_evidence",
    "newsroom": "discovery",
    "search_hit": "secondary_evidence",
}


def normalize_source_type(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return "rss_feed"
    return SOURCE_TYPE_ALIASES.get(raw, raw if raw in CANONICAL_SOURCE_TYPES else "rss_feed")


def normalize_source_role(value: str | None, *, source_type: str | None = None) -> str:
    raw = str(value or "").strip().lower()
    if raw in ROLE_ALIASES:
        return ROLE_ALIASES[raw]
    st = normalize_source_type(source_type) if source_type else ""
    if st in {"official_regulator", "crypto_company", "exchange"}:
        return "primary_evidence"
    return "discovery"


def is_primary_provenance(source_type: str | None, source_role: str | None = None) -> bool:
    role = normalize_source_role(source_role, source_type=source_type)
    stype = normalize_source_type(source_type)
    return role in PRIMARY_ROLES or stype in PRIMARY_SOURCE_TYPES or str(source_type or "").lower() in PRIMARY_SOURCE_TYPES


def stable_source_id(name: str | None, url: str | None, existing: str | None = None) -> str:
    if existing and str(existing).strip():
        return re.sub(r"[^a-z0-9_]+", "_", str(existing).strip().lower()).strip("_") or "source"
    domain = ""
    try:
        domain = urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    except Exception:
        domain = ""
    base = domain.split(":")[0] if domain else str(name or "source")
    slug = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return slug or "source"


def domain_key(url: str | None) -> str:
    try:
        return urlparse(str(url or "")).netloc.lower().removeprefix("www.")
    except Exception:
        return ""
