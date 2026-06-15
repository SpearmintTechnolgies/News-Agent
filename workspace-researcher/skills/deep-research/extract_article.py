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
  5. (optional) web-reader-pro WebFetch tier if markdownify/diskcache available
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from html import unescape

UA = "Mozilla/5.0 (compatible; OpenClawScout/1.0)"
MIN_WORDS = 50
CURL_TIMEOUT = 20


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text or "")
    return text.strip()


def tier_trafilatura_url(url: str) -> tuple[str, str] | None:
    traf = shutil.which("trafilatura")
    if not traf:
        try:
            import trafilatura as traf_mod

            downloaded = traf_mod.fetch_url(url)
            if downloaded:
                text = traf_mod.extract(downloaded, output_format="markdown")
                if text and word_count(text) >= MIN_WORDS:
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
        if proc.returncode == 0 and proc.stdout and word_count(proc.stdout) >= MIN_WORDS:
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
                if proc2.returncode == 0 and proc2.stdout and word_count(proc2.stdout) >= MIN_WORDS:
                    return "curl_trafilatura", clean_text(proc2.stdout)
            finally:
                os.unlink(tmp)
        try:
            import trafilatura as traf_mod

            text = traf_mod.extract(html_bytes.decode("utf-8", errors="replace"), output_format="markdown")
            if text and word_count(text) >= MIN_WORDS:
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
    if word_count(text) >= MIN_WORDS:
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
        if word_count(text) >= MIN_WORDS:
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
        if word_count(text) >= MIN_WORDS:
            return "webfetch_markdownify", text
    except Exception:
        pass
    return None


def extract(url: str) -> dict:
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "empty_url", "tier": None, "content": "", "words": 0}

    tiers = [
        tier_trafilatura_url,
        tier_curl_trafilatura,
        tier_bs4,
        tier_lynx,
        tier_webfetch,
    ]
    for fn in tiers:
        try:
            result = fn(url)
        except Exception:
            result = None
        if result:
            tier, content = result
            return {
                "ok": True,
                "url": url,
                "tier": tier,
                "content": content,
                "words": word_count(content),
            }
    return {"ok": False, "error": "all_tiers_failed", "tier": None, "content": "", "words": 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of raw text")
    args = parser.parse_args()

    result = extract(args.url)
    if result["ok"]:
        print(f"EXTRACT_OK: tier={result['tier']} words={result['words']}", file=sys.stderr)
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
