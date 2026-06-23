#!/usr/bin/env python3
"""
extract_article.py — Multi-tier article extraction with tested fallbacks.

Usage:
  python3 extract_article.py "<url>"
  python3 extract_article.py --json "<url>"

Prints extracted markdown/text to stdout. On success prints EXTRACT_OK to stderr
with tier and word count. Exit 0 on success, 1 if all tiers fail.

Fallback ladder (required tiers use installed tools only):
  1. trafilatura -u <url>
  2. curl + trafilatura on downloaded HTML
  3. requests + selectolax/bs4 text extraction
  4. (optional) lynx -dump if installed
  5. (optional) web-reader-pro Jina tier when DEEP_RESEARCH_JINA=1 (max 2/run)

Cloudflare challenge pages are rejected even when word count >= MIN_WORDS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from html import unescape

UA = "Mozilla/5.0 (compatible; OpenClawScout/1.0)"
MIN_WORDS = 50
CURL_TIMEOUT = 20
_JINA_CALLS = 0
_JINA_MAX_PER_RUN = 2

_CF_MARKERS = (
    "just a moment",
    "cf-ray",
    "challenge-platform",
    "cloudflare",
    "checking your browser",
    "enable javascript and cookies",
    "attention required",
    "cf-challenge",
)

# ── content cache ──────────────────────────────────────────────────────────
# Repeat sources (CoinDesk/Cointelegraph/etc.) recur across stories within a
# batch and across the day. Cache successful extractions for 24h so we skip the
# whole fetch+extract ladder on a hit. Mirrors resolve_url.py's simple file
# cache; one small JSON per URL keeps it concurrency-safe and easy to prune.
_EXTRACT_CACHE_DIR = os.path.expanduser("~/.openclaw/data/extract_cache")
_EXTRACT_TTL_S = int(os.environ.get("EXTRACT_CACHE_TTL_S", str(24 * 3600)))


def _cache_path(url: str) -> str:
    digest = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()
    return os.path.join(_EXTRACT_CACHE_DIR, f"{digest}.json")


def _cache_get(url: str) -> dict | None:
    path = _cache_path(url)
    try:
        with open(path, encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    cached_at = entry.get("cached_at", 0)
    if not isinstance(cached_at, (int, float)) or (time.time() - cached_at) > _EXTRACT_TTL_S:
        return None
    result = entry.get("result")
    if isinstance(result, dict) and result.get("ok") and result.get("content"):
        return result
    return None


def _cache_put(url: str, result: dict) -> None:
    try:
        os.makedirs(_EXTRACT_CACHE_DIR, exist_ok=True)
        path = _cache_path(url)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"cached_at": time.time(), "result": result}, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    return text.strip()


def _is_cloudflare_challenge(html: str) -> bool:
    if not html:
        return False
    lower = html[:8000].lower()
    hits = sum(1 for m in _CF_MARKERS if m in lower)
    return hits >= 2 or ("cf-ray" in lower and "challenge" in lower)


def _accept_content(html: str, text: str) -> bool:
    if word_count(text) < MIN_WORDS:
        return False
    if html and _is_cloudflare_challenge(html):
        return False
    if _is_cloudflare_challenge(text):
        return False
    return True


def tier_trafilatura_url(url: str) -> tuple[str, str] | None:
    traf = shutil.which("trafilatura")
    if not traf:
        try:
            import trafilatura as traf_mod

            downloaded = traf_mod.fetch_url(url)
            if downloaded:
                if _is_cloudflare_challenge(downloaded):
                    return None
                text = traf_mod.extract(downloaded, output_format="markdown")
                if text and _accept_content(downloaded, text):
                    return "trafilatura_module", clean_text(text)
        except Exception:
            pass
        return None
    try:
        proc = subprocess.run(
            [traf, "-u", url, "--output-format", "markdown"],
            capture_output=True,
            text=True,
            timeout=CURL_TIMEOUT + 10,
        )
        if proc.returncode == 0 and proc.stdout and _accept_content("", proc.stdout):
            return "trafilatura_cli", clean_text(proc.stdout)
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def tier_curl_trafilatura(url: str) -> tuple[str, str] | None:
    curl = shutil.which("curl")
    traf = shutil.which("trafilatura")
    if not curl:
        return None
    try:
        proc = subprocess.run(
            ["curl", "-sL", "-A", UA, "--max-time", str(CURL_TIMEOUT), url],
            capture_output=True,
            timeout=CURL_TIMEOUT + 5,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        html_bytes = proc.stdout
        html_str = html_bytes.decode("utf-8", errors="replace")
        if traf:
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False) as f:
                f.write(html_bytes)
                tmp = f.name
            try:
                proc2 = subprocess.run(
                    [traf, "-i", tmp, "--output-format", "markdown"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc2.returncode == 0 and proc2.stdout and _accept_content(html_str, proc2.stdout):
                    return "curl_trafilatura", clean_text(proc2.stdout)
            finally:
                os.unlink(tmp)
        try:
            import trafilatura as traf_mod

            text = traf_mod.extract(html_str, output_format="markdown")
            if text and _accept_content(html_str, text):
                return "curl_trafilatura_module", clean_text(text)
        except Exception:
            pass
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def tier_bs4(url: str) -> tuple[str, str] | None:
    try:
        import requests
    except ImportError:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=CURL_TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception:
        return None

    text = ""
    try:
        from selectolax.parser import HTMLParser

        tree = HTMLParser(html)
        for bad in tree.css("script, style, nav, footer, header, aside, noscript"):
            bad.decompose()
        body = tree.body
        text = body.text(separator="\n") if body else tree.text()
    except Exception:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()
            text = soup.get_text("\n")
        except Exception:
            return None

    text = clean_text(unescape(re.sub(r"[ \t]+\n", "\n", text)))
    if _accept_content(html, text):
        return "bs4_selectolax", text
    return None


def tier_lynx(url: str) -> tuple[str, str] | None:
    lynx = shutil.which("lynx")
    if not lynx:
        return None
    try:
        proc = subprocess.run(
            [lynx, "-dump", url],
            capture_output=True,
            text=True,
            timeout=CURL_TIMEOUT + 10,
        )
        text = clean_text(proc.stdout[:8000] if proc.stdout else "")
        if _accept_content("", text):
            return "lynx", text
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def tier_webfetch(url: str) -> tuple[str, str] | None:
    try:
        import requests
        from bs4 import BeautifulSoup
        from markdownify import markdownify as md
    except ImportError:
        return None
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=CURL_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        article = soup.find("article") or soup.find("main") or soup.body
        if not article:
            return None
        text = clean_text(md(str(article), heading_style="ATX"))
        if _accept_content(resp.text, text):
            return "webfetch_markdownify", text
    except Exception:
        pass
    return None


def tier_jina(url: str) -> tuple[str, str] | None:
    global _JINA_CALLS
    flag = os.environ.get("DEEP_RESEARCH_JINA", "").strip().lower()
    if flag not in ("1", "true", "yes"):
        return None
    if _JINA_CALLS >= _JINA_MAX_PER_RUN:
        return None
    wrp_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web-reader-pro", "scripts",
    )
    if wrp_dir not in sys.path:
        sys.path.insert(0, wrp_dir)
    try:
        from web_reader_pro import WebReaderPro  # type: ignore[import-untyped]
    except Exception:
        return None
    try:
        _JINA_CALLS += 1
        reader = WebReaderPro(max_retries=1)
        result = reader.fetch_with_tier(url, "jina")
        content = str(result.get("content") or "")
        if _accept_content("", content):
            return "jina", clean_text(content)
    except Exception:
        pass
    return None


def extract(url: str, *, use_cache: bool = True) -> dict:
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "empty_url", "tier": None, "content": "", "words": 0}

    if use_cache:
        cached = _cache_get(url)
        if cached is not None:
            out = dict(cached)
            out["from_cache"] = True
            return out

    tiers = [
        tier_trafilatura_url,
        tier_curl_trafilatura,
        tier_bs4,
        tier_lynx,
        tier_webfetch,
        tier_jina,
    ]
    last_http_status: int | None = None
    for fn in tiers:
        try:
            result = fn(url)
        except Exception:
            result = None
        if result:
            tier, content = result
            out = {
                "ok": True,
                "url": url,
                "tier": tier,
                "content": content,
                "words": word_count(content),
                "http_status": last_http_status,
            }
            if use_cache:
                _cache_put(url, out)
            return out
    return {
        "ok": False,
        "error": "all_tiers_failed",
        "tier": None,
        "content": "",
        "words": 0,
        "http_status": last_http_status,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of raw text")
    parser.add_argument("--no-cache", action="store_true", help="Bypass the 24h content cache")
    args = parser.parse_args()

    result = extract(args.url, use_cache=not args.no_cache)
    if result["ok"]:
        cache_tag = " (cache)" if result.get("from_cache") else ""
        print(f"EXTRACT_OK: tier={result['tier']} words={result['words']}{cache_tag}", file=sys.stderr)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result["content"])
        return 0
    print(f"EXTRACT_FAILED: {result.get('error', 'unknown')}", file=sys.stderr)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
