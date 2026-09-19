"""Version Store - persistent storage for frozen artifacts with SHA-256 integrity.

Structure:
STORY-<id>/
    event.json
    sources.json
    research/
        research-v1.json
    factbank/
        factbank-v1.json
    articles/
        article-v1.json
        article-v1.sha256
        article-v2.json
        article-v2.sha256
    images/
        image-v1.png
        image-v1.sha256
        image-v2.png
        image-v2.sha256
    reviews/
        article-v1.json
        image-v1.json
    generation/
        jobs/
            <job-id>.json
    publication/
        publication.json
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.benchmark.input import write_json_utf8

DEFAULT_STORE_ROOT = Path(__file__).resolve().parents[3] / "output" / "v5_stories"


class VersionStore:
    """Persistent versioned artifact storage with SHA-256 integrity.

    Requirements:
    - Immutable historical versions
    - SHA-256 hash for frozen artifacts
    - Current selected versions tracked
    - Creation timestamps
    - Generation metadata
    """

    def __init__(self, root: Path | None = None):
        self.root = Path(root or DEFAULT_STORE_ROOT)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _story_dir(self, event_id: str) -> Path:
        """Get story directory."""
        # Sanitize event_id for filesystem
        safe_id = "".join(c for c in event_id if c.isalnum() or c in "-_")
        return self.root / f"STORY-{safe_id}"

    def _ensure_dirs(self, event_id: str) -> dict[str, Path]:
        """Ensure all subdirectories exist."""
        story_dir = self._story_dir(event_id)

        dirs = {
            "root": story_dir,
            "research": story_dir / "research",
            "factbank": story_dir / "factbank",
            "articles": story_dir / "articles",
            "images": story_dir / "images",
            "reviews": story_dir / "reviews",
            "generation": story_dir / "generation" / "jobs",
            "publication": story_dir / "publication",
        }

        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)

        return dirs

    def _hash_file(self, content: str | bytes) -> str:
        """Calculate SHA-256 hash."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    # Event / Sources Storage

    def save_event(self, event_id: str, event_data: dict[str, Any]) -> Path:
        """Save event metadata."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["root"] / "event.json"
        write_json_utf8(path, event_data)
        return path

    def save_sources(self, event_id: str, sources: list[dict[str, Any]]) -> Path:
        """Save source information."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["root"] / "sources.json"
        write_json_utf8(path, {"sources": sources})
        return path

    # Research Storage

    def save_research(
        self,
        event_id: str,
        version: str,
        research: dict[str, Any],
    ) -> Path:
        """Save research data."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["research"] / f"research-{version}.json"
        write_json_utf8(path, research)
        return path

    # FactBank Storage

    def save_factbank(
        self,
        event_id: str,
        version: str,
        factbank: dict[str, Any],
    ) -> Path:
        """Save fact bank."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["factbank"] / f"factbank-{version}.json"
        write_json_utf8(path, factbank)
        return path

    # Article Storage with SHA-256

    def save_article(
        self,
        event_id: str,
        version: str,
        article: dict[str, Any],
        article_hash: str,
        qa_result: dict[str, Any] | None,
        metadata: dict[str, Any] | None,
    ) -> tuple[Path, Path]:
        """Save article with SHA-256 hash.

        Returns:
            Tuple of (article_path, hash_path)
        """
        dirs = self._ensure_dirs(event_id)

        # Build article record
        record = {
            "event_id": event_id,
            "version": version,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "article": article,
            "qa_result": qa_result or {},
            "metadata": metadata or {},
            "frozen": True,
        }

        # Save article
        article_path = dirs["articles"] / f"article-{version}.json"
        write_json_utf8(article_path, record)

        # Save hash separately
        hash_path = dirs["articles"] / f"article-{version}.sha256"
        hash_data = {
            "event_id": event_id,
            "version": version,
            "article_hash": article_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json_utf8(hash_path, hash_data)

        # Update current selection
        self._set_current_version(event_id, "article", version)

        return article_path, hash_path

    def get_article(
        self,
        event_id: str,
        version: str,
    ) -> dict[str, Any] | None:
        """Get specific article version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["articles"] / f"article-{version}.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return None

    def get_article_hash(
        self,
        event_id: str,
        version: str,
    ) -> str | None:
        """Get SHA-256 hash for article version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["articles"] / f"article-{version}.sha256"

        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("article_hash")
        except (json.JSONDecodeError, TypeError):
            return None

    def list_article_versions(self, event_id: str) -> list[str]:
        """List all article versions."""
        dirs = self._ensure_dirs(event_id)
        versions = []

        for path in dirs["articles"].glob("article-*.json"):
            version = path.stem.replace("article-", "")
            versions.append(version)

        return sorted(versions)

    # Image Storage with SHA-256

    def save_image(
        self,
        event_id: str,
        version: str,
        image_path: str,
        image_hash: str,
        metadata: dict[str, Any] | None,
    ) -> tuple[Path, Path]:
        """Save image with SHA-256 hash.

        Returns:
            Tuple of (image_path, hash_path)
        """
        dirs = self._ensure_dirs(event_id)

        # Copy image to version store
        src = Path(image_path)
        ext = src.suffix if src.suffix else ".png"
        dest = dirs["images"] / f"image-{version}{ext}"

        if src.is_file():
            dest.write_bytes(src.read_bytes())

        # Build hash record
        record = {
            "event_id": event_id,
            "version": version,
            "image_hash": image_hash,
            "original_path": str(image_path),
            "stored_path": str(dest),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
            "frozen": True,
        }

        # Save hash
        hash_path = dirs["images"] / f"image-{version}.sha256"
        write_json_utf8(hash_path, record)

        # Update current selection
        self._set_current_version(event_id, "image", version)

        return dest, hash_path

    def get_image_path(
        self,
        event_id: str,
        version: str,
    ) -> Path | None:
        """Get path to stored image."""
        dirs = self._ensure_dirs(event_id)

        # Try common extensions
        for ext in [".png", ".jpg", ".jpeg"]:
            path = dirs["images"] / f"image-{version}{ext}"
            if path.is_file():
                return path

        return None

    def get_image_hash(
        self,
        event_id: str,
        version: str,
    ) -> str | None:
        """Get SHA-256 hash for image version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["images"] / f"image-{version}.sha256"

        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("image_hash")
        except (json.JSONDecodeError, TypeError):
            return None

    def list_image_versions(self, event_id: str) -> list[str]:
        """List all image versions."""
        dirs = self._ensure_dirs(event_id)
        versions = []

        for path in dirs["images"].glob("image-*.sha256"):
            version = path.stem.replace("image-", "")
            versions.append(version)

        return sorted(versions)

    # Version Tracking

    def _set_current_version(
        self,
        event_id: str,
        artifact_type: str,
        version: str,
    ) -> None:
        """Set current selected version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["root"] / "current_version.json"

        current = {}
        if path.is_file():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, TypeError):
                pass

        current[artifact_type] = version
        current["updated_at"] = datetime.now(timezone.utc).isoformat()

        write_json_utf8(path, current)

    def get_current_version(
        self,
        event_id: str,
        artifact_type: str,
    ) -> str | None:
        """Get current selected version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["root"] / "current_version.json"

        if not path.is_file():
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get(artifact_type)
        except (json.JSONDecodeError, TypeError):
            return None

    # Review Storage

    def save_review(
        self,
        event_id: str,
        artifact_type: str,  # "article" or "image"
        version: str,
        review: dict[str, Any],
    ) -> Path:
        """Save review for an artifact version."""
        dirs = self._ensure_dirs(event_id)

        record = {
            "event_id": event_id,
            "artifact_type": artifact_type,
            "version": version,
            "review": review,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        path = dirs["reviews"] / f"{artifact_type}-{version}.json"
        write_json_utf8(path, record)
        return path

    def get_review(
        self,
        event_id: str,
        artifact_type: str,
        version: str,
    ) -> dict[str, Any] | None:
        """Get review for an artifact version."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["reviews"] / f"{artifact_type}-{version}.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return None

    # Generation Job Storage

    def save_generation_job(self, job: Any) -> Path:
        """Save generation job state."""
        if hasattr(job, "event_id"):
            event_id = job.event_id
        else:
            event_id = job.get("event_id", "unknown")

        dirs = self._ensure_dirs(event_id)

        # Convert dataclass to dict
        if hasattr(job, "__dataclass_fields__"):
            data = asdict(job)
        else:
            data = dict(job)

        path = dirs["generation"] / f"{data.get('job_id', 'job')}.json"
        write_json_utf8(path, data)
        return path

    def get_generation_job(self, event_id: str, job_id: str) -> dict[str, Any] | None:
        """Get generation job state."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["generation"] / f"{job_id}.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return None

    # Publication Storage

    def save_publication(self, event_id: str, publication: dict[str, Any]) -> Path:
        """Save publication record."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["publication"] / "publication.json"
        write_json_utf8(path, publication)
        return path

    def get_publication(self, event_id: str) -> dict[str, Any] | None:
        """Get publication record."""
        dirs = self._ensure_dirs(event_id)
        path = dirs["publication"] / "publication.json"

        if not path.is_file():
            return None

        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            return None

    # Integrity Verification

    def verify_article_integrity(
        self,
        event_id: str,
        version: str,
    ) -> tuple[bool, str | None]:
        """Verify article integrity against stored hash.

        Returns:
            Tuple of (is_valid, computed_hash)
        """
        article = self.get_article(event_id, version)
        stored_hash = self.get_article_hash(event_id, version)

        if not article or not stored_hash:
            return False, None

        # Recompute hash from article body
        article_body = article.get("article", {}).get("article_body", "")
        computed_hash = self._hash_file(article_body)

        return computed_hash == stored_hash, computed_hash

    def verify_image_integrity(
        self,
        event_id: str,
        version: str,
    ) -> tuple[bool, str | None]:
        """Verify image integrity against stored hash.

        Returns:
            Tuple of (is_valid, computed_hash)
        """
        image_path = self.get_image_path(event_id, version)
        stored_hash = self.get_image_hash(event_id, version)

        if not image_path or not stored_hash:
            return False, None

        # Compute hash from file
        computed_hash = self._hash_file(image_path.read_bytes())

        return computed_hash == stored_hash, computed_hash

    # Historical Version Retrieval

    def get_version_history(self, event_id: str) -> dict[str, list[str]]:
        """Get complete version history for a story."""
        return {
            "articles": self.list_article_versions(event_id),
            "images": self.list_image_versions(event_id),
        }
