"""Index of the site's published posts, read from the Rank Math sitemap (titles from the REST API)."""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_CACHE = Path("data/v6_site_index.json")
CACHE_TTL_SECONDS = 6 * 3600
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


def _default_get(url: str, params: dict[str, Any] | None = None) -> Any:
    import httpx

    return httpx.get(url, params=params, timeout=30, follow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0 (NewsAgent internal linker)"})


def fetch_site_index(base_url: str, http_get: HttpGet = _default_get, max_title_pages: int = 20) -> SiteIndex:
    base = base_url.rstrip("/")
    index_xml = http_get(f"{base}/sitemap_index.xml", None).text
    sitemaps = [u for u in _SITEMAP_RE.findall(index_xml) if "post-sitemap" in u] or [f"{base}/post-sitemap.xml"]
    posts: dict[str, SitePost] = {}
    for sitemap in sitemaps:
        for loc, lastmod in _LOC_RE.findall(http_get(sitemap, None).text):
            loc = html.unescape(loc.strip())
            posts[loc.rstrip("/")] = SitePost(url=loc, slug=_slug_of(loc), lastmod=(lastmod or "").strip())
    for page in range(1, max_title_pages + 1):
        resp = http_get(f"{base}/wp-json/wp/v2/posts",
                        {"per_page": 100, "page": page, "_fields": "link,title", "status": "publish"})
        if getattr(resp, "status_code", 200) != 200:
            break
        rows = resp.json()
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            key = str(row.get("link") or "").rstrip("/")
            title = html.unescape(re.sub(r"<[^>]+>", "", str((row.get("title") or {}).get("rendered") or ""))).strip()
            if key in posts and title:
                posts[key].title = title
        if len(rows) < 100:
            break
    logger.info("[SITE-INDEX] %d posts from %d sitemap(s)", len(posts), len(sitemaps))
    return SiteIndex(base_url=base, posts=list(posts.values()), fetched_at=time.time())


def load_site_index(
    base_url: str,
    *,
    cache_path: Path = DEFAULT_CACHE,
    ttl_seconds: int = CACHE_TTL_SECONDS,
    http_get: HttpGet = _default_get,
) -> SiteIndex:
    """Cached index; refreshed after ``ttl_seconds``. Falls back to a stale cache if the site is unreachable."""
    cached: SiteIndex | None = None
    if cache_path.is_file():
        try:
            cached = SiteIndex.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
        except (ValueError, TypeError):
            cached = None
    if cached and cached.base_url == base_url.rstrip("/") and time.time() - cached.fetched_at < ttl_seconds:
        return cached
    try:
        index = fetch_site_index(base_url, http_get=http_get)
    except Exception as exc:  # network/XML failures must not stop publishing
        logger.warning("[SITE-INDEX] refresh failed: %s", exc)
        return cached or SiteIndex(base_url=base_url.rstrip("/"))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(index.to_dict(), ensure_ascii=False), encoding="utf-8")
    return index
