"""Configurable source registry with metadata and health tracking."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class SourceType(Enum):
    """Classification of news sources."""
    CRYPTO_PUBLICATION = "crypto_publication"
    OFFICIAL_REGULATOR = "official_regulator"
    EXCHANGE = "exchange"
    CRYPTO_COMPANY = "crypto_company"
    RSS_FEED = "rss_feed"
    API = "api"


class SourceRole(Enum):
    """Role of source in evidence gathering."""
    DISCOVERY = "discovery"
    PRIMARY_EVIDENCE = "primary_evidence"
    SECONDARY_EVIDENCE = "secondary_evidence"


@dataclass
class SourceMetadata:
    """Metadata for a news source.

    Attributes:
        source_id: Unique identifier for the source
        name: Human-readable name
        source_type: Type classification (crypto publication, regulator, etc.)
        url: Feed endpoint or API URL
        enabled: Whether source is active
        authority: Reliability score 0.0-1.0
        role: Evidence role
        last_success: Timestamp of last successful fetch
        last_failure: Timestamp of last failure
        failure_count: Consecutive failure counter
        error_reason: Last error message
        timeout_seconds: Request timeout
        retry_limit: Max retries on failure
    """
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
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class SourceRegistry:
    """Registry of configured news sources with health tracking.

    Supports crypto publications, regulators, exchanges, and official sources.
    """

    # Default sources that are known to work
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
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        """Initialize registry.

        Args:
            config_path: Path to JSON config file. Uses defaults if not found.
        """
        self.config_path = config_path
        self._sources: dict[str, SourceMetadata] = {}
        self._load()

    def _load(self) -> None:
        """Load sources from config or use defaults."""
        if self.config_path and self.config_path.is_file():
            try:
                data = json.loads(self.config_path.read_text(encoding="utf-8"))
                for src_data in data.get("feeds", []):
                    source = SourceMetadata.from_dict(src_data)
                    self._sources[source.source_id] = source
                return
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                # Fall through to defaults
                pass

        # Use defaults
        for src_data in self.DEFAULT_SOURCES:
            source = SourceMetadata.from_dict(src_data)
            self._sources[source.source_id] = source

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
        """Get all sources."""
        return list(self._sources.values())

    def get_enabled(self) -> list[SourceMetadata]:
        """Get only enabled sources."""
        return [s for s in self._sources.values() if s.enabled]

    def get_by_type(self, source_type: str | SourceType) -> list[SourceMetadata]:
        """Get sources by type."""
        type_val = source_type.value if isinstance(source_type, SourceType) else source_type
        return [s for s in self._sources.values() if s.source_type == type_val]

    def get(self, source_id: str) -> SourceMetadata | None:
        """Get a specific source by ID."""
        return self._sources.get(source_id)

    def add(self, source: SourceMetadata) -> None:
        """Add or update a source."""
        self._sources[source.source_id] = source

    def record_success(self, source_id: str) -> None:
        """Record successful fetch for a source."""
        source = self._sources.get(source_id)
        if source:
            source.last_success = datetime.utcnow().isoformat()
            source.failure_count = 0
            source.error_reason = None

    def record_failure(self, source_id: str, reason: str) -> None:
        """Record failed fetch for a source."""
        source = self._sources.get(source_id)
        if source:
            source.last_failure = datetime.utcnow().isoformat()
            source.failure_count += 1
            source.error_reason = reason

    def get_health_summary(self) -> dict[str, Any]:
        """Get health summary of all sources."""
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
        }
