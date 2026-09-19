"""WordPress Publisher - fake implementation for E2E testing.

No real WordPress calls - just returns simulated URLs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Fake WordPress state storage
_FAKE_PUBLICATIONS: dict[str, dict[str, Any]] = {}


@dataclass
class PublicationResult:
    """Result of WordPress publication."""
    ok: bool
    url: str = ""
    post_id: str = ""
    error: str = ""
    new: bool = True  # Was this a new post or returning existing


class WordPressPublisher:
    """Fake WordPress publisher for testing.
    
    - Does NOT make real HTTP calls
    - Counts publication attempts
    - Returns consistent URLs for idempotency
    """
    
    _call_count: int = 0
    _create_post_count: int = 0
    
    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = environ or {}
    
    @classmethod
    def get_stats(cls) -> dict[str, int]:
        """Get publication statistics."""
        return {
            "total_calls": cls._call_count,
            "create_post_count": cls._create_post_count,
        }
    
    @classmethod
    def reset_stats(cls) -> None:
        """Reset statistics for testing."""
        cls._call_count = 0
        cls._create_post_count = 0
        global _FAKE_PUBLICATIONS
        _FAKE_PUBLICATIONS = {}
    
    def publish_article(
        self,
        title: str,
        content: str,
        featured_image: Path | str | None = None,
    ) -> dict[str, Any]:
        """Publish article to WordPress (FAKE - no real calls).
        
        Idempotent: first publish creates, duplicates return existing.
        """
        # Count this call
        WordPressPublisher._call_count += 1
        
        # Generate post ID from title hash for consistency
        post_id = f"fake_{hash(title) % 100000:05d}"
        
        # Check if already published
        if post_id in _FAKE_PUBLICATIONS:
            existing = _FAKE_PUBLICATIONS[post_id]
            return {
                "ok": True,
                "url": existing["url"],
                "post_id": existing["post_id"],
                "new": False,
            }
        
        # Simulate "create post" - count this
        WordPressPublisher._create_post_count += 1
        
        # Generate fake URL
        slug = title.lower().replace(" ", "-")[:30] if title else "untitled"
        url = f"https://fake-wordpress.example.com/2024/01/01/{slug}/"
        
        # Save publication record
        _FAKE_PUBLICATIONS[post_id] = {
            "post_id": post_id,
            "url": url,
            "title": title,
            "published_at": datetime.now(timezone.utc).isoformat(),
            "has_image": featured_image is not None,
        }
        
        return {
            "ok": True,
            "url": url,
            "post_id": post_id,
            "new": True,
        }
