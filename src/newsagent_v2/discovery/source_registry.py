"""Configurable source registry with metadata and health tracking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from newsagent_v2.discovery.provenance import (
    domain_key,
    normalize_source_role,
    normalize_source_type,
    stable_source_id,
)


def default_sources_config_path() -> Path:
    """Repo-root config/sources.json (discovery/ -> newsagent_v2 -> src -> root)."""
    return Path(__file__).resolve().parents[3] / "config" / "sources.json"


class SourceType(Enum):
    """Classification of news sources."""
    CRYPTO_PUBLICATION = "crypto_publication"
    OFFICIAL_REGULATOR = "official_regulator"
    EXCHANGE = "exchange"
    CRYPTO_COMPANY = "crypto_company"
    RSS_FEED = "rss_feed"
    API = "api"
    FINANCIAL_NEWS = "financial_news"


class SourceRole(Enum):
    """Role of source in evidence gathering."""
    DISCOVERY = "discovery"
    PRIMARY_EVIDENCE = "primary_evidence"
    SECONDARY_EVIDENCE = "secondary_evidence"


@dataclass
class SourceMetadata:
    """Metadata for a news source."""
    source_id: str
    name: str
    source_type: str  # SourceType.value for JSON serialization
    url: str
    enabled: bool = True
    authority: float = 0.5
    role: str = SourceRole.DISCOVERY.value
    timeout_seconds: int = 30
    retry_limit: int = 2
    last_success: str | None = None
    last_failure: str | None = None
    failure_count: int = 0
    error_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceMetadata:
        raw = dict(data or {})
        stype = normalize_source_type(raw.get("source_type"))
        role = normalize_source_role(raw.get("role"), source_type=stype)
        sid = stable_source_id(raw.get("name"), raw.get("url"), raw.get("source_id"))
        raw["source_id"] = sid
        raw["source_type"] = stype
        raw["role"] = role
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})


class SourceRegistry:
    """Registry of configured news sources with health tracking."""

    # Fallback defaults when config is missing (subset; prefer config/sources.json).
    DEFAULT_SOURCES: list[dict[str, Any]] = [
        {
            "source_id": "coindesk",
            "name": "CoinDesk",
            "source_type": SourceType.CRYPTO_PUBLICATION.value,
            "url": "https://www.coindesk.com/arc/outboundfeeds/rss/",
            "authority": 0.80,
            "role": SourceRole.DISCOVERY.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
        {
            "source_id": "cointelegraph",
            "name": "Cointelegraph",
            "source_type": SourceType.CRYPTO_PUBLICATION.value,
            "url": "https://cointelegraph.com/rss",
            "authority": 0.70,
            "role": SourceRole.DISCOVERY.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
        {
            "source_id": "theblock",
            "name": "The Block",
            "source_type": SourceType.CRYPTO_PUBLICATION.value,
            "url": "https://www.theblock.co/rss.xml",
            "authority": 0.80,
            "role": SourceRole.DISCOVERY.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
        {
            "source_id": "cftc_general",
            "name": "CFTC General",
            "source_type": SourceType.OFFICIAL_REGULATOR.value,
            "url": "https://www.cftc.gov/RSS/RSSGP/rssgp.xml",
            "authority": 1.00,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 3,
        },
        {
            "source_id": "cftc_enforcement",
            "name": "CFTC Enforcement",
            "source_type": SourceType.OFFICIAL_REGULATOR.value,
            "url": "https://www.cftc.gov/RSS/RSSENF/rssenf.xml",
            "authority": 1.00,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 3,
        },
        {
            "source_id": "fca_news",
            "name": "FCA News",
            "source_type": SourceType.OFFICIAL_REGULATOR.value,
            "url": "https://www.fca.org.uk/news/rss.xml",
            "authority": 1.00,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 3,
        },
        {
            "source_id": "sec_press",
            "name": "SEC Press Releases",
            "source_type": SourceType.OFFICIAL_REGULATOR.value,
            "url": "https://www.sec.gov/news/pressreleases.rss",
            "authority": 1.00,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 3,
        },
        {
            "source_id": "bbc_business",
            "name": "BBC Business",
            "source_type": SourceType.FINANCIAL_NEWS.value,
            "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
            "authority": 0.85,
            "role": SourceRole.SECONDARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
        {
            "source_id": "coinbase_blog",
            "name": "Coinbase Blog",
            "source_type": SourceType.EXCHANGE.value,
            "url": "https://www.coinbase.com/blog/rss.xml",
            "authority": 0.90,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
        {
            "source_id": "ethereum_blog",
            "name": "Ethereum Foundation Blog",
            "source_type": SourceType.CRYPTO_COMPANY.value,
            "url": "https://blog.ethereum.org/en/feed.xml",
            "authority": 0.95,
            "role": SourceRole.PRIMARY_EVIDENCE.value,
            "enabled": True,
            "timeout_seconds": 30,
            "retry_limit": 2,
        },
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialize registry.

        Args:
            config_path: Path to JSON config. Defaults to repo config/sources.json.
        """
        if config_path is None:
            config_path = default_sources_config_path()
        self.config_path = Path(config_path) if config_path else None
        self._sources: dict[str, SourceMetadata] = {}
        self._load()

    def _load(self) -> None:
        """Load sources from config or use defaults. Dedupes by source_id and URL/domain."""
        loaded: list[SourceMetadata] = []
        if self.config_path and self.config_path.is_file():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                for src_data in data.get("feeds", []):
                    loaded.append(SourceMetadata.from_dict(src_data))
            except (json.JSONDecodeError, KeyError, TypeError, OSError):
                loaded = []

        if not loaded:
            loaded = [SourceMetadata.from_dict(src) for src in self.DEFAULT_SOURCES]

        self._sources = {}
        seen_urls: set[str] = set()
        # Prefer primary/official first when colliding on URL/domain.
        rank = {"primary_evidence": 0, "secondary_evidence": 1, "discovery": 2}
        loaded.sort(key=lambda s: (rank.get(s.role, 9), -float(s.authority or 0.0)))
        for source in loaded:
            url_key = str(source.url or "").strip().lower().rstrip("/")
            dom = domain_key(source.url)
            if url_key and url_key in seen_urls:
                continue
            if source.source_id in self._sources:
                continue
            # Domain dedupe: keep first (higher-priority) source per domain for same role class.
            if dom and any(domain_key(s.url) == dom for s in self._sources.values()):
                # Allow multiple official feeds on same regulator domain; skip duplicate publications.
                existing = next(s for s in self._sources.values() if domain_key(s.url) == dom)
                if existing.source_type == source.source_type == "crypto_publication":
                    continue
                if existing.source_type == source.source_type == "financial_news":
                    continue
            self._sources[source.source_id] = source
            if url_key:
                seen_urls.add(url_key)

    def save(self) -> None:
        """Persist current sources to config file."""
        if self.config_path:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "feeds": [s.to_dict() for s in self._sources.values()],
                "saved_at": datetime.utcnow().isoformat(),
            }
            self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_all(self) -> list[SourceMetadata]:
        return list(self._sources.values())

    def get_enabled(self) -> list[SourceMetadata]:
        return [s for s in self._sources.values() if s.enabled]

    def get_by_type(self, source_type: str | SourceType) -> list[SourceMetadata]:
        type_val = source_type.value if isinstance(source_type, SourceType) else normalize_source_type(source_type)
        return [s for s in self._sources.values() if s.source_type == type_val]

    def get(self, source_id: str) -> SourceMetadata | None:
        return self._sources.get(source_id)

    def add(self, source: SourceMetadata) -> None:
        self._sources[source.source_id] = source

    def record_success(self, source_id: str) -> None:
        source = self._sources.get(source_id)
        if source:
            source.last_success = datetime.utcnow().isoformat()
            source.failure_count = 0
            source.error_reason = None

    def record_failure(self, source_id: str, reason: str) -> None:
        source = self._sources.get(source_id)
        if source:
            source.last_failure = datetime.utcnow().isoformat()
            source.failure_count += 1
            source.error_reason = reason

    def get_health_summary(self) -> dict[str, Any]:
        total = len(self._sources)
        enabled = len(self.get_enabled())
        failed_recently = sum(1 for s in self._sources.values() if s.failure_count > 0)
        by_type: dict[str, int] = {}
        for s in self._sources.values():
            by_type[s.source_type] = by_type.get(s.source_type, 0) + 1
        return {
            "total": total,
            "enabled": enabled,
            "disabled": total - enabled,
            "failed_recently": failed_recently,
            "by_type": by_type,
            "config_path": str(self.config_path) if self.config_path else None,
        }
