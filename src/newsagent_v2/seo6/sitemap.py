"""Index of the site's published posts, read from the Rank Math sitemap (titles from the REST API)."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path("data/v6_site_index.json")
SITEMAP_DIR = Path("data/v5_state/sitemaps")
CACHE_TTL_SECONDS = 6 * 3600
RECENT_DAYS = 120
_LOC_RE = re.compile(r"<url>\s*<loc>(.*?)</loc>(?:\s*<lastmod>(.*?)</lastmod>)?", re.S)
_SITEMAP_RE = re.compile(r"<sitemap>\s*<loc>(.*?)</loc>", re.S)

HttpGet = Callable[[str, dict[str, Any] | None], Any]


@dataclass
class SitePost:
    url: str
    slug: str
    title: str = ""
    lastmod: str = ""

    @property
    def label(self) -> str:
        return self.title or self.slug.replace("-", " ")


@dataclass
class SiteIndex:
    base_url: str
    posts: list[SitePost] = field(default_factory=list)
    fetched_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"base_url": self.base_url, "fetched_at": self.fetched_at, "posts": [asdict(p) for p in self.posts]}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SiteIndex":
        return cls(base_url=data.get("base_url", ""), fetched_at=float(data.get("fetched_at") or 0),
                   posts=[SitePost(**p) for p in data.get("posts") or []])


def _slug_of(url: str) -> str:
    return urlparse(url).path.strip("/").split("/")[-1]


def cache_path_for(base_url: str) -> Path:
    """One index file per website, so Coinography never reuses the Coin Network list."""
    host = (urlparse(base_url).hostname or "site").lower()
    safe = re.sub(r"[^a-z0-9.-]", "", host) or "site"
    return SITEMAP_DIR / f"{safe}.json"


def _is_xml(text: str) -> bool:
    head = text.lstrip()[:300].lower()
    return head.startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head


def _xml_text(response: Any) -> str:
    if getattr(response, "status_code", 200) != 200:
        return ""
    text = str(getattr(response, "text", "") or "")
    return text if _is_xml(text) else ""


def _post_sitemap(url: str) -> bool:
    name = url.lower()
    return "post-sitemap" in name or "posts-post" in name or "sitemap-post" in name


def _too_old(stamp: str, days: int = RECENT_DAYS) -> bool:
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days > days


def _default_get(url: str, params: dict[str, Any] | None = None) -> Any:
    import httpx

    return httpx.get(url, params=params, timeout=30, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (NewsAgent internal linker)"})


def _read_urlset(xml: str, posts: dict[str, SitePost]) -> None:
    for loc, lastmod in _LOC_RE.findall(xml):
        loc = html.unescape(loc.strip())
        if not loc:
            continue
        posts[loc.rstrip("/")] = SitePost(url=loc, slug=_slug_of(loc), lastmod=(lastmod or "").strip())


def _sitemap_documents(base: str, http_get: HttpGet) -> list[str]:
    """Rank Math uses sitemap_index.xml. Other sites publish one sitemap.xml urlset."""
    index_xml = _xml_text(http_get(f"{base}/sitemap_index.xml", None))
    if index_xml:
        found = [html.unescape(url.strip()) for url in _SITEMAP_RE.findall(index_xml)]
        post_maps = [url for url in found if _post_sitemap(url)]
        return post_maps or found or [f"{base}/post-sitemap.xml"]
    return [f"{base}/sitemap.xml", f"{base}/post-sitemap.xml", f"{base}/wp-sitemap.xml"]


def fetch_site_index(base_url: str, http_get: HttpGet = _default_get, max_title_pages: int = 10) -> SiteIndex:
    base = base_url.rstrip("/")
    host = (urlparse(base).hostname or "").lower()
    posts: dict[str, SitePost] = {}
    documents = _sitemap_documents(base, http_get)
    for sitemap in documents:
        xml = _xml_text(http_get(sitemap, None))
        if not xml:
            continue
        nested = [html.unescape(url.strip()) for url in _SITEMAP_RE.findall(xml)]
        children = [url for url in nested if _post_sitemap(url)] or nested
        if children and "<urlset" not in xml[:800].lower():
            for child in children:
                child_xml = _xml_text(http_get(child, None))
                if child_xml:
                    _read_urlset(child_xml, posts)
            continue
        _read_urlset(xml, posts)
    for page in range(1, max_title_pages + 1):
        resp = http_get(
            f"{base}/wp-json/wp/v2/posts",
            {"per_page": 100, "page": page, "_fields": "link,title,date,modified", "status": "publish"},
        )
        if getattr(resp, "status_code", 200) != 200:
            break
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            break
        oldest = ""
        for row in rows:
            key = str(row.get("link") or "").rstrip("/")
            title = html.unescape(re.sub(r"<[^>]+>", "", str((row.get("title") or {}).get("rendered") or ""))).strip()
            stamp = str(row.get("modified") or row.get("date") or "").strip()
            if stamp:
                oldest = stamp
            if not key or (urlparse(key).hostname or "").lower() not in {"", host}:
                continue
            if key in posts:
                if title:
                    posts[key].title = title
                if stamp and _too_old(posts[key].lastmod) and not _too_old(stamp):
                    posts[key].lastmod = stamp
            elif title:
                posts[key] = SitePost(url=str(row.get("link") or key), slug=_slug_of(key), title=title, lastmod=stamp)
        if len(rows) < 100 or (oldest and _too_old(oldest)):
            break
    own = [post for post in posts.values() if not host or (urlparse(post.url).hostname or "").lower() == host]
    logger.info("[SITE-INDEX] %s %d posts from %d sitemap(s)", host or base, len(own), len(documents))
    return SiteIndex(base_url=base, posts=own, fetched_at=time.time())


def load_site_index(
    base_url: str,
    *,
    cache_path: Path | None = None,
    ttl_seconds: int = CACHE_TTL_SECONDS,
    http_get: HttpGet = _default_get,
) -> SiteIndex:
    """Cached index for this website only. A stale file from another site is not reused."""
    path = cache_path or cache_path_for(base_url)
    base = base_url.rstrip("/")
    cached: SiteIndex | None = None
    if path.is_file():
        try:
            loaded = SiteIndex.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError, json.JSONDecodeError):
            loaded = None
        if loaded and loaded.base_url == base:
            cached = loaded
    if cached and time.time() - cached.fetched_at < ttl_seconds:
        return cached
    try:
        index = fetch_site_index(base_url, http_get=http_get)
    except Exception as exc:  # network/XML failures must not stop publishing
        logger.warning("[SITE-INDEX] refresh failed: %s", exc)
        return cached or SiteIndex(base_url=base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index.to_dict(), ensure_ascii=False), encoding="utf-8")
    return index
