"""Publication Package - immutable package for WordPress publishing.

Contains exact approved frozen artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import hashlib

from newsagent_v2.benchmark.input import write_json_utf8


@dataclass
class PublicationPackage:
    """Immutable publication package for WordPress.

    Requirements:
    - Exact approved article version
    - Exact approved article hash
    - Exact approved image version
    - Exact approved image hash
    - No regeneration or reformatting
    - Exact artifacts only
    """
    # Identification
    event_id: str
    package_id: str = field(default_factory=lambda: f"pub-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")

    # Approved artifacts
    article_version: str = ""
    article_hash: str = ""
    article_content: str = ""
    article_path: str = ""

    image_version: str = ""
    image_hash: str = ""
    image_path: str = ""

    # Metadata
    headline: str = ""
    title: str = ""
    dek: str = ""
    slug: str = ""
    excerpt: str = ""
    meta_description: str = ""

    # QA/approval
    qa_status: dict[str, Any] = field(default_factory=dict)
    approved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_by: str = ""

    # Idempotency
    idempotency_key: str = ""

    def __post_init__(self):
        # Generate idempotency key from event + versions + hashes
        key_data = f"{self.event_id}:{self.article_version}:{self.article_hash}:{self.image_version}:{self.image_hash}"
        self.idempotency_key = hashlib.sha256(key_data.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "event_id": self.event_id,
            "package_id": self.package_id,
            "article_version": self.article_version,
            "article_hash": self.article_hash,
            "article_path": self.article_path,
            "image_version": self.image_version,
            "image_hash": self.image_hash,
            "image_path": self.image_path,
            "headline": self.headline,
            "title": self.title,
            "dek": self.dek,
            "slug": self.slug,
            "excerpt": self.excerpt,
            "meta_description": self.meta_description,
            "qa_status": self.qa_status,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "idempotency_key": self.idempotency_key,
        }

    def save(self, output_root: Path) -> Path:
        """Save publication package."""
        output_root.mkdir(parents=True, exist_ok=True)
        path = output_root / f"{self.event_id}-publication-{self.package_id}.json"
        write_json_utf8(path, self.to_dict())
        return path

    def verify_integrity(self) -> bool:
        """Verify package contents against stored hashes."""
        # Verify article hash
        article_hash = hashlib.sha256(self.article_content.encode()).hexdigest()
        if article_hash != self.article_hash:
            return False

        # Verify image file if exists
        if self.image_path and Path(self.image_path).is_file():
            image_hash = hashlib.sha256(Path(self.image_path).read_bytes()).hexdigest()
            if image_hash != self.image_hash:
                return False

        return True


class PublicationBuilder:
    """Builder for creating PublicationPackage."""

    def build_from_approval(
        self,
        event_id: str,
        article_version: str,
        image_version: str,
        version_store: Any,
    ) -> PublicationPackage | None:
        """Build package from approved versions."""
        # Get article
        article_data = version_store.get_article(event_id, article_version)
        if not article_data:
            return None

        article = article_data.get("article", {})
        article_content = article.get("article_body", "")
        article_hash = article_data.get("article_hash") or hashlib.sha256(
            article_content.encode()
        ).hexdigest()

        # Get image
        image_path = version_store.get_image_path(event_id, image_version)
        image_hash = version_store.get_image_hash(event_id, image_version) or ""

        return PublicationPackage(
            event_id=event_id,
            article_version=article_version,
            article_hash=article_hash,
            article_content=article_content,
            article_path=str(version_store.get_article_path(event_id, article_version)) if hasattr(version_store, 'get_article_path') else "",
            image_version=image_version,
            image_hash=image_hash,
            image_path=str(image_path) if image_path else "",
            headline=article.get("headline", ""),
            title=article.get("seo_title", article.get("headline", "")),
            dek=article.get("dek", ""),
            slug=article.get("slug", ""),
            excerpt=article.get("meta_description", ""),
            meta_description=article.get("meta_description", ""),
            qa_status=article_data.get("qa_result", {}),
        )
