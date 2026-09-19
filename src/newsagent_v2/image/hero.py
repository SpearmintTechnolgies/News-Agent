"""
Declared article hero-image discovery and safe download.

Extracts only canonical metadata (og/twitter/JSON-LD/image_src).
Does not scrape arbitrary <img> tags. Does not search image engines.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from PIL import Image

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.image.artifacts import REPO_ROOT, utc_now
from newsagent_v2.image.validate import (
    MAX_BYTES,
    ImageValidationError,
    file_sha256,
    refuse_overwrite,
)

HERO_SCHEMA_VERSION = "hero-reference-v1"
DEFAULT_REFERENCES_DIR = REPO_ROOT / "input" / "references"
USER_AGENT = "NewsAgentV2-hero/1.0"
MAX_HTML_BYTES = 8 * 1024 * 1024
MIN_HERO_WIDTH = 320
MIN_HERO_HEIGHT = 180
ARTICLE_LD_TYPES = frozenset(
    {"newsarticle", "article", "blogposting", "reportagenewsarticle", "webpage"}
)


class HeroImageError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _MetaCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.og_image: str | None = None
        self.og_image_secure: str | None = None
        self.twitter_image: str | None = None
        self.image_src: str | None = None
        self._ld_chunks: list[str] = []
        self._in_ld = False
        self._ld_buf = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {str(k).lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (mapping.get("property") or mapping.get("name") or "").strip().lower()
            content = (mapping.get("content") or mapping.get("value") or "").strip()
            if not content:
                return
            if key == "og:image" and not self.og_image:
                self.og_image = content
            elif key == "og:image:secure_url" and not self.og_image_secure:
                self.og_image_secure = content
            elif key in {"twitter:image", "twitter:image:src"} and not self.twitter_image:
                self.twitter_image = content
            return
        if tag == "link":
            rel = " ".join((mapping.get("rel") or "").lower().split())
            href = (mapping.get("href") or "").strip()
            if href and rel == "image_src" and not self.image_src:
                self.image_src = href
            return
        if tag == "script":
            script_type = (mapping.get("type") or "").lower()
            if "ld+json" in script_type:
                self._in_ld = True
                self._ld_buf = ""

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._ld_buf += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            text = self._ld_buf.strip()
            if text:
                self._ld_chunks.append(text)
            self._in_ld = False
            self._ld_buf = ""

    @property
    def json_ld_blocks(self) -> list[str]:
        return list(self._ld_chunks)


def _absolute_http_url(candidate: str, article_url: str) -> str | None:
    raw = (candidate or "").strip()
    if not raw or raw.startswith("data:") or raw.startswith("javascript:"):
        return None
    joined = urljoin(article_url, raw)
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return joined


def _image_field_url(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        return _image_field_url(value[0])
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "contenturl"):
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
    return None


def _type_tokens(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.split("/")[-1].strip().lower()}
    if isinstance(value, list):
        tokens: set[str] = set()
        for item in value:
            tokens.update(_type_tokens(item))
        return tokens
    return set()


def _jsonld_article_image(node: Any) -> str | None:
    if isinstance(node, list):
        for item in node:
            found = _jsonld_article_image(item)
            if found:
                return found
        return None
    if not isinstance(node, dict):
        return None
    if node.get("@graph") is not None:
        found = _jsonld_article_image(node.get("@graph"))
        if found:
            return found
    types = _type_tokens(node.get("@type") or node.get("type"))
    if types & ARTICLE_LD_TYPES:
        return _image_field_url(node.get("image"))
    return None


def extract_declared_hero(html: str, article_url: str) -> dict[str, str] | None:
    """Return the first declared hero URL. Never falls back to arbitrary <img> tags."""
    parser = _MetaCollector()
    parser.feed(html or "")
    parser.close()
    ordered: list[tuple[str, str | None]] = [
        ("og:image", parser.og_image),
        ("og:image:secure_url", parser.og_image_secure),
        ("twitter:image", parser.twitter_image),
    ]
    jsonld_url = None
    for block in parser.json_ld_blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        jsonld_url = _jsonld_article_image(payload)
        if jsonld_url:
            break
    ordered.append(("jsonld_article_image", jsonld_url))
    ordered.append(("link_image_src", parser.image_src))
    for method, raw in ordered:
        absolute = _absolute_http_url(raw or "", article_url)
        if absolute:
            return {"url": absolute, "discovery_method": method}
    return None


def publisher_domain(article_url: str) -> str:
    return urlparse(article_url).netloc.lower()


FetchResult = tuple[int, str, bytes, str]
FetchFn = Callable[[str], FetchResult]


def default_fetch(url: str, *, timeout: int = 30, accept: str = "*/*") -> FetchResult:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            content_type = str(response.headers.get("Content-Type") or "")
            data = response.read(MAX_HTML_BYTES + 1)
            final_url = str(response.geturl() or url)
    except HTTPError as exc:
        body = exc.read(4096) if exc.fp else b""
        raise HeroImageError("http_error", f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise HeroImageError("transport_error", f"request failed: {exc.reason}") from exc
    if len(data) > MAX_HTML_BYTES:
        raise HeroImageError("too_large", "response exceeded the safe size cap")
    if status in {401, 403, 429, 503}:
        raise HeroImageError("blocked", f"HTTP {status} — not bypassing access controls")
    if status < 200 or status >= 300:
        raise HeroImageError("http_error", f"HTTP {status} for {url}")
    return status, content_type, data, final_url


def _suffix_for_image(data: bytes, content_type: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return ".webp"
    lowered = content_type.lower()
    if "png" in lowered:
        return ".png"
    if "webp" in lowered:
        return ".webp"
    if "jpeg" in lowered or "jpg" in lowered:
        return ".jpg"
    raise HeroImageError("not_image", f"response is not a supported image type: {content_type!r}")


def _looks_like_image(content_type: str, data: bytes) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return True
    if data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:4] == b"RIFF" and b"WEBP" in data[:16]:
        return True
    return False


def acquire_hero_image(
    article_url: str,
    dest_stem: Path,
    *,
    fetch: FetchFn | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """
    Fetch the article HTML, extract a declared hero, download that one image.

    dest_stem is a path without required suffix, e.g. .../event-027-source
    """
    if not article_url.strip():
        raise HeroImageError("missing_article_url", "article URL is required")
    fetcher = fetch or default_fetch
    html_status, html_type, html_bytes, final_article = fetcher(article_url)
    html_mime = html_type.split(";", 1)[0].strip().lower()
    if html_mime and "html" not in html_mime and "xml" not in html_mime and "text/" not in html_mime:
        if _looks_like_image(html_type, html_bytes):
            raise HeroImageError(
                "not_article_page",
                "URL resolved to an image file rather than an article page with declared metadata",
            )
    html = html_bytes.decode("utf-8", errors="replace")
    discovered = extract_declared_hero(html, final_article or article_url)
    if discovered is None:
        raise HeroImageError(
            "missing_metadata",
            "no og:image, twitter:image, JSON-LD article image, or link[rel=image_src] was declared",
        )

    image_url = discovered["url"]
    img_status, img_type, img_bytes, final_image_url = fetcher(image_url)
    if not _looks_like_image(img_type, img_bytes):
        raise HeroImageError("not_image", f"hero URL did not return an image ({img_type!r})")
    if len(img_bytes) > MAX_BYTES:
        raise HeroImageError("too_large", "hero image exceeds the reasonable size cap")
    try:
        with Image.open(BytesIO(img_bytes)) as image:
            image.load()
            width, height = image.size
            fmt = (image.format or "").lower()
    except Exception as exc:
        raise HeroImageError("undecodable", f"hero image could not be decoded: {exc}") from exc
    if width < MIN_HERO_WIDTH or height < MIN_HERO_HEIGHT:
        raise HeroImageError(
            "tiny_image",
            f"hero image {width}x{height} is below {MIN_HERO_WIDTH}x{MIN_HERO_HEIGHT}",
        )

    suffix = _suffix_for_image(img_bytes, img_type)
    dest = dest_stem if dest_stem.suffix else dest_stem.with_suffix(suffix)
    if dest.suffix.lower() != suffix:
        dest = dest.with_suffix(suffix)
    refuse_overwrite(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(img_bytes)
    after = dest.read_bytes()
    if after != img_bytes:
        raise HeroImageError("bytes_mutated", "downloaded hero bytes changed on disk")

    digest = file_sha256(dest)
    if file_sha256(dest) != digest:
        raise HeroImageError("bytes_mutated", "hero file changed after hashing")

    provenance = {
        "schema_version": HERO_SCHEMA_VERSION,
        "event_id": event_id,
        "article_url": article_url,
        "resolved_article_url": final_article,
        "image_url": final_image_url,
        "declared_image_url": image_url,
        "discovery_method": discovered["discovery_method"],
        "publisher_domain": publisher_domain(article_url),
        "html_http_status": html_status,
        "image_http_status": img_status,
        "content_type": img_type,
        "format": fmt or suffix.lstrip("."),
        "width": width,
        "height": height,
        "size_bytes": len(img_bytes),
        "sha256": digest,
        "local_path": str(dest),
        "role": "story_reference",
        "usage": "reference_material_only",
        "must_not_be_final_image": True,
        "arbitrary_img_fallback": False,
        "image_generation_requests": 0,
        "timestamp_utc": utc_now().strftime("%Y%m%dT%H%M%SZ"),
    }
    provenance_path = dest.with_name(f"{dest.stem}.provenance.json")
    refuse_overwrite(provenance_path)
    write_json_utf8(provenance_path, provenance)
    provenance["provenance_path"] = str(provenance_path)
    if dest.read_bytes() != img_bytes:
        raise HeroImageError("bytes_mutated", "hero file changed after provenance write")
    return provenance
