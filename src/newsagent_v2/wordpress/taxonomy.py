"""WordPress taxonomy (categories/tags) resolution.

Fetch/search existing, resolve names to IDs, create missing.
Deterministic - uses NewsAgent metadata only, no LLM calls.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urljoin

from .config import WordPressConfig

Transport = Callable[..., Any]


@dataclass
class TaxonomyResult:
    """Result of taxonomy resolution."""
    category_ids: list[int]
    tag_ids: list[int]
    created_categories: list[str]  # Names of categories we had to create
    created_tags: list[str]  # Names of tags we had to create
    errors: list[str]  # Any errors during resolution


class WordPressTaxonomyResolver:
    """Resolve NewsAgent metadata to WordPress category/tag IDs."""

    def __init__(self, config: WordPressConfig, transport: Transport) -> None:
        self.config = config
        self.transport = transport

    def _wp_api(self, endpoint: str) -> str:
        """Build WordPress REST API URL."""
        return urljoin(self.config.base_url + "/", f"wp-json/wp/v2/{endpoint}")

    def _auth(self) -> tuple[str, str]:
        """Return auth tuple."""
        return (self.config.username, self.config.app_password)

    def _search_terms(
        self,
        taxonomy: str,  # "categories" or "tags"
        names: list[str],
    ) -> dict[str, int]:
        """Search for existing terms by name, return {name_lower: id}."""
        found: dict[str, int] = {}
        for name in names:
            name_clean = name.strip()
            if not name_clean:
                continue

            resp = self.transport(
                "GET",
                self._wp_api(f"{taxonomy}?search={quote(name_clean)}&per_page=10"),
                auth=self._auth(),
            )
            if resp.get("ok"):
                items = resp.get("payload", [])
                if isinstance(items, list):
                    for item in items:
                        if isinstance(item, dict):
                            item_name = html.unescape(item.get("name", "")).strip().lower()
                            if item_name == name_clean.lower():
                                found[name_clean.lower()] = item.get("id")
                                break
        return found

    def _create_term(
        self,
        taxonomy: str,  # "categories" or "tags"
        name: str,
    ) -> int | None:
        """Create a new term, return its ID."""
        resp = self.transport(
            "POST",
            self._wp_api(taxonomy),
            json={"name": name.strip()},
            auth=self._auth(),
        )
        if resp.get("ok"):
            payload = resp.get("payload", {})
            if isinstance(payload, dict):
                return payload.get("id")
        return None

    def resolve(
        self,
        categories: list[str],
        tags: list[str],
        create_missing: bool = True,
    ) -> TaxonomyResult:
        """Resolve category/tag names to WordPress IDs.

        Args:
            categories: List of category names from NewsAgent metadata
            tags: List of tag names from NewsAgent metadata
            create_missing: If True, create terms that don't exist

        Returns:
            TaxonomyResult with IDs and creation info
        """
        result = TaxonomyResult(
            category_ids=[],
            tag_ids=[],
            created_categories=[],
            created_tags=[],
            errors=[],
        )

        # Search existing categories
        if categories:
            cat_map = self._search_terms("categories", categories)
            seen_category_ids: set[int] = set()
            for name in categories:
                name_clean = name.strip()
                if not name_clean:
                    continue
                name_lower = name_clean.lower()
                if name_lower in cat_map:
                    category_id = cat_map[name_lower]
                    if category_id not in seen_category_ids:
                        result.category_ids.append(category_id)
                        seen_category_ids.add(category_id)
                elif create_missing:
                    new_id = self._create_term("categories", name_clean)
                    if new_id and new_id not in seen_category_ids:
                        result.category_ids.append(new_id)
                        result.created_categories.append(name_clean)
                        seen_category_ids.add(new_id)
                    else:
                        if not new_id:
                            result.errors.append(f"Failed to create category: {name_clean}")

        # Search existing tags
        if tags:
            tag_map = self._search_terms("tags", tags)
            seen_tag_ids: set[int] = set()
            for name in tags:
                name_clean = name.strip()
                if not name_clean:
                    continue
                name_lower = name_clean.lower()
                if name_lower in tag_map:
                    tag_id = tag_map[name_lower]
                    if tag_id not in seen_tag_ids:
                        result.tag_ids.append(tag_id)
                        seen_tag_ids.add(tag_id)
                elif create_missing:
                    new_id = self._create_term("tags", name_clean)
                    if new_id and new_id not in seen_tag_ids:
                        result.tag_ids.append(new_id)
                        result.created_tags.append(name_clean)
                        seen_tag_ids.add(new_id)
                    else:
                        if not new_id:
                            result.errors.append(f"Failed to create tag: {name_clean}")

        return result

    def fetch_existing_categories(self) -> list[dict[str, Any]]:
        """Fetch existing categories from WordPress."""
        resp = self.transport(
            "GET",
            self._wp_api("categories?per_page=100"),
            auth=self._auth(),
        )
        if resp.get("ok"):
            payload = resp.get("payload", [])
            if isinstance(payload, list):
                return [{"id": item.get("id"), "name": item.get("name")} for item in payload if isinstance(item, dict)]
        return []

    def fetch_existing_tags(self) -> list[dict[str, Any]]:
        """Fetch existing tags from WordPress."""
        resp = self.transport(
            "GET",
            self._wp_api("tags?per_page=100"),
            auth=self._auth(),
        )
        if resp.get("ok"):
            payload = resp.get("payload", [])
            if isinstance(payload, list):
                return [{"id": item.get("id"), "name": item.get("name")} for item in payload if isinstance(item, dict)]
        return []
