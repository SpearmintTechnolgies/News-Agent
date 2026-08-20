#!/usr/bin/env python3
"""
fetch_source_image.py — Download the source story's hero image for Pixel.

Pixel must remix THIS photo (subject/setting/visual story), not invent a
generic 3D coin from the category slug.

Usage:
  python3 fetch_source_image.py --research validated.json --out /run/media/source.jpg
  python3 fetch_source_image.py --url https://example.com/story --out source.jpg

Exit 0 + SOURCE_IMAGE: <path> when a usable JPEG was saved.
Exit 1 + SOURCE_IMAGE_MISS: <reason> when none could be fetched (caller falls back
to a headline-based editorial prompt, still not a category coin).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html import unescape
from urllib.parse import urljoin, urlparse
from typing import Any

UA = (
    "Mozilla/5.0 (compatible; OpenClawScout/2.0; +https://coinnetwork.info) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
MIN_BYTES = 8192
MAX_BYTES = 8 * 1024 * 1024
HTML_TIMEOUT = 12
IMG_TIMEOUT = 20

_SKIP_HINTS = (
    "1x1", "pixel", "spacer", "favicon", "sprite", "blank.gif",
    "/icon", "logo.svg", ".svg", "gravatar", "emoji", "tracking",
    "/brands/", "150x150", "yimg.com/lb/",
)
_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

_OG_RES = (
    re.compile(
        r'<meta[^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\'][^>]+content\s*=\s*["\']([^"\']+)["\']',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content\s*=\s*["\']([^"\']+)["\'][^>]+(?:property|name)\s*=\s*["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']',
        re.I,
    ),
    re.compile(
        r'<link[^>]+rel\s*=\s*["\']image_src["\'][^>]+href\s*=\s*["\']([^"\']+)["\']',
        re.I,
    ),
)
_MD_IMG = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_JSONLD_IMG = re.compile(
    r'"image"\s*:\s*(?:"(https?://[^"]+)"|\[\s*"(https?://[^"]+)")',
    re.I,
)


def _looks_like_image_url(url: str) -> bool:
    if not url or not url.startswith(("http://", "https://")):
        return False
    low = url.lower()
    if any(h in low for h in _SKIP_HINTS):
        return False
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in _IMG_EXT):
        return True
    return any(k in low for k in ("image", "img", "media", "wp-content", "cdn", "photo", "thumb"))


def first_markdown_image(text: str) -> str:
    for m in _MD_IMG.finditer(text or ""):
        url = unescape(m.group(1).split()[0].strip("\"'"))
        if _looks_like_image_url(url):
            return url
    return ""


def images_from_html(html: str, base_url: str) -> list[str]:
    found: list[str] = []
    blob = html or ""
    for cre in _OG_RES:
        for m in cre.finditer(blob):
            raw = unescape((m.group(1) or "").strip())
            if raw:
                found.append(urljoin(base_url, raw))
    for m in _JSONLD_IMG.finditer(blob):
        raw = unescape((m.group(1) or m.group(2) or "").strip())
        if raw:
            found.append(urljoin(base_url, raw))
    out: list[str] = []
    seen: set[str] = set()
    for u in found:
        if u in seen or not _looks_like_image_url(u):
            continue
        seen.add(u)
        out.append(u)
    return out


def _http_get(url: str, timeout: int, accept: str) -> tuple[bytes, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
        },
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        data = resp.read(MAX_BYTES + 1)
        final = resp.geturl() or url
    if len(data) > MAX_BYTES:
        raise ValueError("too large")
    return data, ctype or final


def scrape_og_images(page_url: str) -> list[str]:
    try:
        data, ctype = _http_get(
            page_url, HTML_TIMEOUT, "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8"
        )
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        return []
    if "image/" in (ctype or ""):
        return [page_url]
    html = data.decode("utf-8", errors="ignore")
    return images_from_html(html, page_url)


def _to_jpeg(raw: bytes, dest: str) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(dest)) or ".", exist_ok=True)
    if raw[:2] == b"\xff\xd8":
        with open(dest, "wb") as f:
            f.write(raw)
        return os.path.getsize(dest)
    try:
        from io import BytesIO
        from PIL import Image

        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        elif img.mode == "L":
            img = img.convert("RGB")
        img.save(dest, format="JPEG", quality=90, optimize=True)
        return os.path.getsize(dest)
    except Exception:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:4] == b"RIFF":
            return 0
        with open(dest, "wb") as f:
            f.write(raw)
        return os.path.getsize(dest) if raw[:2] == b"\xff\xd8" else 0


def download_image(url: str, dest: str) -> int:
    data, ctype = _http_get(url, IMG_TIMEOUT, "image/jpeg,image/png,image/webp,image/*;q=0.8,*/*;q=0.5")
    if len(data) < MIN_BYTES:
        return 0
    if ctype.startswith("text/"):
        return 0
    size = _to_jpeg(data, dest)
    return size if size >= MIN_BYTES else 0


def candidate_urls_from_research(research: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in ("source_image_url", "image_url", "og_image", "hero_image"):
        val = research.get(key)
        if isinstance(val, str) and val.startswith("http"):
            urls.append(val.strip())
    for u in research.get("source_urls") or []:
        if isinstance(u, str) and u.startswith("http"):
            urls.append(u.strip())
    primary = research.get("primary_url") or ""
    if isinstance(primary, str) and primary.startswith("http"):
        urls.insert(0, primary.strip())
    # Prefer the original publisher page over syndicates (Yahoo sidebar logos).
    def _page_rank(u: str) -> int:
        host = urlparse(u).netloc.lower()
        if "beincrypto.com" in host or "coindesk.com" in host or "theblock.co" in host:
            return 0
        if "yahoo.com" in host or "news.google.com" in host:
            return 2
        return 1
    urls.sort(key=_page_rank)
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def resolve_and_save(research: dict[str, Any], dest: str) -> tuple[str, str]:
    """Return (local_path, source_url) or ("", "")."""
    dest = os.path.abspath(dest)
    tried: list[str] = []
    page_urls: list[str] = []
    for u in candidate_urls_from_research(research):
        low = u.lower()
        if _looks_like_image_url(u) and any(p in low for p in (".jpg", ".jpeg", ".png", ".webp", "/media", "wp-content", "cdn")):
            tried.append(u)
        else:
            page_urls.append(u)

    for img_url in tried:
        try:
            if download_image(img_url, dest):
                return dest, img_url
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            continue

    for page in page_urls[:4]:
        for img_url in scrape_og_images(page):
            try:
                if download_image(img_url, dest):
                    return dest, img_url
            except (urllib.error.URLError, TimeoutError, ValueError, OSError):
                continue
    return "", ""


def atomic_placeholder_cleanup(dest: str) -> None:
    try:
        if os.path.isfile(dest) and os.path.getsize(dest) < MIN_BYTES:
            os.remove(dest)
    except OSError:
        pass


def main() -> int:
    p = argparse.ArgumentParser(description="Fetch source story hero image")
    p.add_argument("--research", help="validated.json")
    p.add_argument("--url", help="Story page or direct image URL")
    p.add_argument("--out", required=True, help="Destination JPEG path")
    args = p.parse_args()

    research: dict[str, Any] = {}
    if args.research:
        try:
            with open(args.research, encoding="utf-8") as f:
                research = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"SOURCE_IMAGE_MISS: cannot read research ({e})")
            return 1
    if args.url:
        research.setdefault("source_urls", [])
        if isinstance(research["source_urls"], list):
            research["source_urls"] = [args.url] + list(research["source_urls"])

    dest = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    path, url = resolve_and_save(research, dest)
    if not path:
        atomic_placeholder_cleanup(dest)
        print("SOURCE_IMAGE_MISS: no usable hero image on source story")
        return 1
    print(f"SOURCE_IMAGE: {path}")
    print(f"SOURCE_IMAGE_URL: {url}")
    print(f"SOURCE_IMAGE_BYTES: {os.path.getsize(path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
