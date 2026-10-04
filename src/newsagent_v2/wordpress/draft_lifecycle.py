"""WordPress draft lifecycle operations.

Create, update, publish, unpublish, retrieve WordPress drafts for events.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

from .adapter import sanitize_wp_error
from .config import WordPressConfig
from .draft_store import WordPressDraftRecord, WordPressDraftStore
from .html_formatter import ArticleHtmlFormatter, FormattedArticle
from .seo_metadata import (
    SEOMetadata,
    SEOValidationResult,
    build_seo_for_article,
    validate_wordpress_seo_meta,
)
from .rankmath import apply_rankmath_seo
from .taxonomy import WordPressTaxonomyResolver
from newsagent_v2.publication.master_index import MasterIndexStore

Transport = Callable[..., Any]


@dataclass
class DraftResult:
    """Result of a draft lifecycle operation."""
    ok: bool
    event_id: str
    wp_post_id: int | None = None
    wp_url: str | None = None
    status: str | None = None
    error: str | None = None
    error_code: str | None = None
    created: bool = False  # True if this was a new draft
    updated: bool = False  # True if existing draft was updated
    category_ids: list[int] = None  # type: ignore  # Assigned in post_init
    tag_ids: list[int] = None  # type: ignore
    taxonomy_created: list[str] = None  # type: ignore  # Categories/tags created
    seo: SEOMetadata = None  # type: ignore  # SEO metadata
    seo_validation: SEOValidationResult = None  # type: ignore  # Validation results
    rank_math_applied: bool = False
    media_alt_applied: bool = False

    def __post_init__(self) -> None:
        if self.category_ids is None:
            self.category_ids = []
        if self.tag_ids is None:
            self.tag_ids = []
        if self.taxonomy_created is None:
            self.taxonomy_created = []


class WordPressDraftLifecycle:
    """WordPress draft lifecycle manager."""

    def __init__(
        self,
        config: WordPressConfig,
        transport: Transport,
        store: WordPressDraftStore | None = None,
        master_index: MasterIndexStore | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.store = store or WordPressDraftStore()
        self.master_index = master_index or MasterIndexStore()

    def _wp_api(self, endpoint: str) -> str:
        """Build WordPress REST API URL."""
        return urljoin(self.config.base_url + "/", f"wp-json/wp/v2/{endpoint}")

    def _auth(self) -> tuple[str, str]:
        """Return auth tuple."""
        return (self.config.username, self.config.app_password)

    def _upload_media(self, image_path: str, event_id: str) -> dict[str, Any]:
        """Upload featured image to WordPress media library."""
        path = Path(image_path)
        if not path.is_file():
            return {"ok": False, "error": "Image file not found"}

        resp = self.transport(
            "POST",
            self._wp_api("media"),
            files={"file": (path.name, path.read_bytes())},
            auth=self._auth(),
        )
        return resp


    def _apply_rankmath_after_draft(
        self,
        *,
        event_id: str,
        wp_post_id: int,
        wp_url: str | None,
        seo: SEOMetadata,
        seo_validation: SEOValidationResult,
        featured_media_id: int | None,
        created: bool = False,
        updated: bool = False,
        category_ids: list[int] | None = None,
        tag_ids: list[int] | None = None,
        taxonomy_created: list[str] | None = None,
        permalink_slug: str | None = None,
    ) -> DraftResult:
        """Apply Rank Math updateMeta + media alt after a successful draft write.

        Soft-fail style consistent with V5 SEO validation: draft remains draft,
        store already saved, but ok=False with a clear error_code if Rank Math fails.
        Never flips status to publish.
        """
        rm = apply_rankmath_seo(
            config=self.config,
            transport=self.transport,
            post_id=int(wp_post_id),
            seo=seo,
            permalink_slug=permalink_slug,
            featured_media_id=featured_media_id,
        )
        if not rm.get("ok"):
            return DraftResult(
                ok=False,
                event_id=event_id,
                wp_post_id=wp_post_id,
                wp_url=wp_url,
                status="draft",
                created=created,
                updated=updated,
                error=rm.get("error") or "Rank Math updateMeta failed",
                error_code=rm.get("error_code") or "rank_math_update_failed",
                category_ids=category_ids if category_ids else None,
                tag_ids=tag_ids if tag_ids else None,
                taxonomy_created=taxonomy_created if taxonomy_created else None,
                seo=seo,
                seo_validation=seo_validation,
                rank_math_applied=False,
                media_alt_applied=bool(rm.get("media_alt_applied")),
            )
        return DraftResult(
            ok=True,
            event_id=event_id,
            wp_post_id=wp_post_id,
            wp_url=wp_url,
            status="draft",
            created=created,
            updated=updated,
            category_ids=category_ids if category_ids else None,
            tag_ids=tag_ids if tag_ids else None,
            taxonomy_created=taxonomy_created if taxonomy_created else None,
            seo=seo,
            seo_validation=seo_validation,
            rank_math_applied=True,
            media_alt_applied=bool(rm.get("media_alt_applied")),
        )

    def create_or_update_draft(
        self,
        event_id: str,
        article: dict[str, Any],
        article_version: str,
        image_path: str | None = None,
        image_version: str | None = None,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        create_taxonomy: bool = True,
        evidence: list[dict[str, Any]] | None = None,
        topic: str = "",
        entities: list[str] | None = None,
        format_html: bool = True,
    ) -> DraftResult:
        """Create new WordPress draft or update existing.

        Returns existing draft if already present for this event_id.

        Args:
            categories: Category names from NewsAgent metadata
            tags: Tag names from NewsAgent metadata
            create_taxonomy: If True, create missing categories/tags
            evidence: Evidence data for source links
            topic: Topic for "Read Also" internal link discovery
            format_html: If True, format article as CMS-ready HTML
        """
        secrets = self.config.secrets()
        existing = self.store.load(event_id)

        # Resolve categories/tags to IDs
        category_ids: list[int] = []
        tag_ids: list[int] = []
        taxonomy_created: list[str] = []
        taxonomy_errors: list[str] = []

        if categories or tags:
            resolver = WordPressTaxonomyResolver(self.config, self.transport)
            tax_result = resolver.resolve(
                categories=categories or [],
                tags=tags or [],
                create_missing=create_taxonomy,
            )
            category_ids = tax_result.category_ids
            tag_ids = tax_result.tag_ids
            taxonomy_created = tax_result.created_categories + tax_result.created_tags
            taxonomy_errors = tax_result.errors

        # Upload featured image if provided
        featured_media_id = None
        if image_path and existing and existing.featured_media_id:
            # Keep existing media if no new image
            featured_media_id = existing.featured_media_id
        elif image_path:
            media_resp = self._upload_media(image_path, event_id)
            if media_resp.get("ok"):
                payload = media_resp.get("payload") or {}
                featured_media_id = payload.get("id")

        # Format article content as CMS-ready HTML
        article_body = article.get("article_body") or ""
        formatted_content = article_body
        headings: list[dict[str, Any]] = []
        source_links: list[dict[str, Any]] = []
        internal_links: list[dict[str, Any]] = []

        if format_html and article_body:
            formatter = ArticleHtmlFormatter(self.config, self.transport, master_index=self.master_index)
            formatted = formatter.format_article(
                article_body=article_body,
                evidence=evidence,
                topic=topic,
                entities=entities,
                include_toc=True,
                include_sources=True,
                include_read_also=bool(topic),
                preserve_structure=bool(article.get("preserve_structure")),
            )
            formatted_content = formatted.html_content
            headings = formatted.headings
            source_links = formatted.source_links
            internal_links = formatted.internal_links

        # Build SEO metadata and validate (topic/entities aid focus keyphrase)
        seo_article = dict(article)
        if topic and not seo_article.get("topic"):
            seo_article["topic"] = topic
        if entities and not seo_article.get("entities"):
            seo_article["entities"] = list(entities)
        seo, seo_validation = build_seo_for_article(
            article=seo_article,
            wp_base_url=self.config.base_url,
            repair=True,
        )

        # Build post body (use SEO-optimized slug)
        body: dict[str, Any] = {
            "title": seo.title or article.get("headline") or "",
            "slug": seo.slug or article.get("slug") or "",
            "content": formatted_content,
            "excerpt": seo.meta_description or article.get("dek") or "",
            "status": "draft",
            "meta": {
                # Rank Math keys omitted from wp/v2/posts — they caused PHP fatals (HTTP 500).
                "description": seo.meta_description or article.get("meta_description") or "",
                "newsagent_seo_title": seo.seo_title,
                "newsagent_focus_keyphrase": seo.focus_keyphrase,
                "newsagent_canonical_url": seo.canonical_url or "",
                "newsagent_og_title": seo.open_graph_title,
                "newsagent_og_description": seo.open_graph_description,
                "newsagent_twitter_title": seo.twitter_title,
                "newsagent_twitter_description": seo.twitter_description,
                "newsagent_event_id": event_id,
                "newsagent_headings": headings,
                "newsagent_sources": source_links,
                "newsagent_internal_links": internal_links,
            },
        }
        if featured_media_id:
            body["featured_media"] = featured_media_id
        if category_ids:
            body["categories"] = category_ids
        if tag_ids:
            body["tags"] = tag_ids

        seo_package_issues = validate_wordpress_seo_meta(body["meta"], seo)
        if seo_package_issues:
            return DraftResult(
                ok=False,
                event_id=event_id,
                error=f"WordPress SEO package is incomplete: {', '.join(seo_package_issues)}",
                error_code="seo_package_incomplete",
                seo=seo,
                seo_validation=seo_validation,
            )

        if existing:
            # Update existing draft
            resp = self.transport(
                "PUT",
                self._wp_api(f"posts/{existing.wp_post_id}"),
                json=body,
                auth=self._auth(),
            )
            if not resp.get("ok"):
                return DraftResult(
                    ok=False,
                    event_id=event_id,
                    error=sanitize_wp_error(str(resp.get("error") or "update failed"), secrets),
                    error_code="update_failed",
                )

            payload = resp.get("payload") or {}
            record = WordPressDraftRecord(
                event_id=event_id,
                wp_post_id=existing.wp_post_id,
                article_version=article_version,
                image_version=image_version or existing.image_version,
                status="draft",
                wp_url=payload.get("link") or existing.wp_url,
                created_at=existing.created_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                wp_modified=payload.get("modified"),
                featured_media_id=featured_media_id or existing.featured_media_id,
            )
            self.store.save(record)
            self.master_index.remove_publication(event_id)
            return self._apply_rankmath_after_draft(
                event_id=event_id,
                wp_post_id=record.wp_post_id,
                wp_url=record.wp_url,
                seo=seo,
                seo_validation=seo_validation,
                featured_media_id=record.featured_media_id,
                created=False,
                updated=True,
                category_ids=category_ids if category_ids else None,
                tag_ids=tag_ids if tag_ids else None,
                taxonomy_created=taxonomy_created if taxonomy_created else None,
                permalink_slug=seo.slug or body.get("slug") or "",
            )

        # Create new draft
        resp = self.transport(
            "POST",
            self._wp_api("posts"),
            json=body,
            auth=self._auth(),
        )
        if not resp.get("ok"):
            return DraftResult(
                ok=False,
                event_id=event_id,
                error=sanitize_wp_error(str(resp.get("error") or "create failed"), secrets),
                error_code="create_failed",
            )

        payload = resp.get("payload") or {}
        wp_url = payload.get("link")
        wp_post_id = payload.get("id")

        if not isinstance(wp_url, str) or not wp_url.startswith("http"):
            return DraftResult(
                ok=False,
                event_id=event_id,
                error="WordPress did not return a valid URL",
                error_code="missing_url",
            )

        now = datetime.now(timezone.utc).isoformat()
        record = WordPressDraftRecord(
            event_id=event_id,
            wp_post_id=wp_post_id,
            article_version=article_version,
            image_version=image_version,
            status="draft",
            wp_url=wp_url,
            created_at=now,
            updated_at=now,
            wp_modified=payload.get("modified"),
            featured_media_id=featured_media_id,
        )
        self.store.save(record)
        return self._apply_rankmath_after_draft(
            event_id=event_id,
            wp_post_id=wp_post_id,
            wp_url=wp_url,
            seo=seo,
            seo_validation=seo_validation,
            featured_media_id=featured_media_id,
            created=True,
            updated=False,
            category_ids=category_ids if category_ids else None,
            tag_ids=tag_ids if tag_ids else None,
            taxonomy_created=taxonomy_created if taxonomy_created else None,
            permalink_slug=seo.slug or body.get("slug") or "",
        )

    def publish_draft(self, event_id: str) -> DraftResult:
        """Publish the draft for this event."""
        secrets = self.config.secrets()
        existing = self.store.load(event_id)
        if not existing:
            return DraftResult(
                ok=False,
                event_id=event_id,
                error="No draft found for this event",
                error_code="draft_not_found",
            )

        if existing.status == "publish":
            return DraftResult(
                ok=True,
                event_id=event_id,
                wp_post_id=existing.wp_post_id,
                wp_url=existing.wp_url,
                status="publish",
                updated=False,
            )

        resp = self.transport(
            "PUT",
            self._wp_api(f"posts/{existing.wp_post_id}"),
            json={"status": "publish"},
            auth=self._auth(),
        )
        if not resp.get("ok"):
            return DraftResult(
                ok=False,
                event_id=event_id,
                error=sanitize_wp_error(str(resp.get("error") or "publish failed"), secrets),
                error_code="publish_failed",
            )

        payload = resp.get("payload") or {}
        record = WordPressDraftRecord(
            event_id=event_id,
            wp_post_id=existing.wp_post_id,
            article_version=existing.article_version,
            image_version=existing.image_version,
            status="publish",
            wp_url=payload.get("link") or existing.wp_url,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            wp_modified=payload.get("modified"),
            featured_media_id=existing.featured_media_id,
        )
        self.store.save(record)
        return DraftResult(
            ok=True,
            event_id=event_id,
            wp_post_id=record.wp_post_id,
            wp_url=record.wp_url,
            status="publish",
            updated=True,
        )

    def unpublish_draft(self, event_id: str) -> DraftResult:
        """Unpublish: revert published post to draft status."""
        secrets = self.config.secrets()
        existing = self.store.load(event_id)
        if not existing:
            return DraftResult(
                ok=False,
                event_id=event_id,
                error="No draft found for this event",
                error_code="draft_not_found",
            )

        resp = self.transport(
            "PUT",
            self._wp_api(f"posts/{existing.wp_post_id}"),
            json={"status": "draft"},
            auth=self._auth(),
        )
        if not resp.get("ok"):
            return DraftResult(
                ok=False,
                event_id=event_id,
                error=sanitize_wp_error(str(resp.get("error") or "unpublish failed"), secrets),
                error_code="unpublish_failed",
            )

        payload = resp.get("payload") or {}
        record = WordPressDraftRecord(
            event_id=event_id,
            wp_post_id=existing.wp_post_id,
            article_version=existing.article_version,
            image_version=existing.image_version,
            status="draft",
            wp_url=payload.get("link") or existing.wp_url,
            created_at=existing.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            wp_modified=payload.get("modified"),
            featured_media_id=existing.featured_media_id,
        )
        self.store.save(record)
        self.master_index.remove_publication(event_id)
        return DraftResult(
            ok=True,
            event_id=event_id,
            wp_post_id=record.wp_post_id,
            wp_url=record.wp_url,
            status="draft",
            updated=True,
        )

    def retrieve_wp_state(self, event_id: str) -> dict[str, Any]:
        """Retrieve current WordPress state for this event's draft."""
        secrets = self.config.secrets()
        existing = self.store.load(event_id)
        if not existing:
            return {"ok": False, "error": "draft_not_found", "event_id": event_id}

        resp = self.transport(
            "GET",
            self._wp_api(f"posts/{existing.wp_post_id}"),
            auth=self._auth(),
        )
        if not resp.get("ok"):
            return {
                "ok": False,
                "error": sanitize_wp_error(str(resp.get("error") or "retrieve failed"), secrets),
                "error_code": "retrieve_failed",
                "event_id": event_id,
            }

        payload = resp.get("payload") or {}
        return {
            "ok": True,
            "event_id": event_id,
            "wp_post_id": payload.get("id"),
            "wp_url": payload.get("link"),
            "status": payload.get("status"),
            "title": payload.get("title", {}).get("rendered"),
            "modified": payload.get("modified"),
            "featured_media": payload.get("featured_media"),
            "persisted_state": existing.to_dict(),
        }

    def get_draft_info(self, event_id: str) -> WordPressDraftRecord | None:
        """Get persisted draft info for event."""
        return self.store.load(event_id)
