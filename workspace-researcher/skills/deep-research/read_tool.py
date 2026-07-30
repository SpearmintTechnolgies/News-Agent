#!/usr/bin/env python3
"""
read_tool.py — Read ONE link into clean prose for Scout's DEEP_RESEARCH loop.

Primary path is Jina Reader (https://r.jina.ai/<url>): Jina fetches and renders
the page on its OWN servers, so Cloudflare / anti-bot blocks that kill a home-IP
curl are bypassed. Keyless by default (20 RPM per IP); set JINA_API_KEY to lift
to 500 RPM automatically.

Per-link fallback ladder (matches the agreed design):
  1. Jina Reader (keyless, or with JINA_API_KEY if set)
  2. trafilatura (cheap home-IP fetch) — only if Jina misses
  3. skip the link (record ok=false; the agent reads the next search result)

Rate limiting is enforced ACROSS separate invocations via a tiny timestamp file
(~/.openclaw/data/jina_last_request), since each link is its own exec call.
Thread-safe for parallel reads inside run_research.py.

Usage:
  python3 read_tool.py --url "<url>" --out-dir /run/research/sources
  python3 read_tool.py --url "<url>" --out-dir DIR --source "CoinDesk"

Writes DIR/<sha1>.json = {url, source, ok, words, content, tier}.
stderr: READ_OK / READ_SKIP plus CUMULATIVE: words=<W> sources=<S> (from DIR)
Exit 0 on a usable read, 1 on skip.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_RESEARCH_CHECK = os.path.join(os.path.dirname(_HERE), "research-check")
if _RESEARCH_CHECK not in sys.path:
    sys.path.insert(0, _RESEARCH_CHECK)

import resolve_url  # noqa: E402

MIN_WORDS = 80
JINA_TIMEOUT = int(os.environ.get("JINA_TIMEOUT_S", "15"))
_HAS_JINA_KEY = bool(os.environ.get("JINA_API_KEY", "").strip())
_DEFAULT_INTERVAL = "0.15" if _HAS_JINA_KEY else "3.5"
MIN_INTERVAL_S = float(os.environ.get("JINA_MIN_INTERVAL_S", _DEFAULT_INTERVAL))
_THROTTLE_FILE = os.path.expanduser("~/.openclaw/data/jina_last_request")
_THROTTLE_LOCK = threading.Lock()

_CF_MARKERS = (
    "just a moment", "cf-ray", "challenge-platform", "checking your browser",
    "enable javascript and cookies", "attention required", "cf-challenge",
)


def has_jina_key() -> bool:
    return bool(os.environ.get("JINA_API_KEY", "").strip())


def _word_count(text: str) -> int:
    return len((text or "").split())


def _is_challenge(text: str) -> bool:
    if not text:
        return True
    low = text[:6000].lower()
    return sum(1 for m in _CF_MARKERS if m in low) >= 2


def _source_from_url(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.split(".")[0].title() if host else "Unknown"
    except ValueError:
        return "Unknown"


def _clean_jina_markdown(text: str) -> str:
    """Strip Jina's header block + nav/ticker/ad boilerplate into readable prose."""
    if "Markdown Content:" in text:
        text = text.split("Markdown Content:", 1)[1]
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)

    kept: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        raw = block.strip()
        if not raw:
            continue
        links = len(re.findall(r"\]\(", raw))
        plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", raw)
        plain = re.sub(r"<[^>]+>", " ", plain)
        plain = re.sub(r"[#*_`>|]+", " ", plain)
        plain = re.sub(r"[ \t]+", " ", plain).strip()
        words = plain.split()
        if len(words) < 6:
            continue
        if links and links * 8 > len(words):
            continue
        if len(re.findall(r"[A-Z]{2,5}\$[\d,]", raw)) >= 2:
            continue
        if not re.search(r"[.!?]", plain) and len(words) < 25:
            continue
        kept.append(plain)

    out = re.sub(r"\n{3,}", "\n\n", "\n\n".join(kept))
    return out.strip()


def _throttle() -> None:
    """Ensure >= MIN_INTERVAL_S since the previous Jina request (cross-process safe)."""
    if MIN_INTERVAL_S <= 0:
        return
    with _THROTTLE_LOCK:
        try:
            os.makedirs(os.path.dirname(_THROTTLE_FILE), exist_ok=True)
            with open(_THROTTLE_FILE, "a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                last = float(raw or "0")
                wait = MIN_INTERVAL_S - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
        except (OSError, ValueError):
            pass


def _mark_request_time() -> None:
    with _THROTTLE_LOCK:
        try:
            os.makedirs(os.path.dirname(_THROTTLE_FILE), exist_ok=True)
            with open(_THROTTLE_FILE, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.write(str(time.time()))
        except OSError:
            pass


def _fetch_jina(url: str) -> tuple[str, str] | None:
    try:
        import requests
    except ImportError:
        return None
    headers = {
        "X-Return-Format": "markdown",
        "Accept": "text/plain, text/markdown, */*",
        "User-Agent": "Mozilla/5.0 (compatible; OpenClawScout/2.0)",
    }
    key = os.environ.get("JINA_API_KEY", "").strip()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    _throttle()
    resp = None
    try:
        resp = requests.get(
            f"https://r.jina.ai/{url}", headers=headers, timeout=JINA_TIMEOUT,
        )
    except Exception as e:
        print(f"[read_tool] jina error: {e}", file=sys.stderr)
        return None
    finally:
        _mark_request_time()
    if resp is None or resp.status_code != 200 or not resp.text:
        code = resp.status_code if resp is not None else 0
        print(f"[read_tool] jina HTTP {code}", file=sys.stderr)
        return None
    content = _clean_jina_markdown(resp.text)
    if _word_count(content) < MIN_WORDS or _is_challenge(content):
        return None
    return "jina", content


def _fetch_trafilatura(url: str) -> tuple[str, str] | None:
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded or _is_challenge(downloaded):
            return None
        text = trafilatura.extract(downloaded, output_format="markdown")
    except Exception:
        return None
    if not text:
        return None
    content = _clean_jina_markdown(text)
    if _word_count(content) < MIN_WORDS or _is_challenge(content):
        return None
    return "trafilatura", content


def _cumulative(out_dir: str) -> tuple[int, int]:
    """Total words + distinct-domain source count from ok records in out_dir."""
    words = 0
    domains: set[str] = set()
    if not os.path.isdir(out_dir):
        return 0, 0
    for fn in os.listdir(out_dir):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("ok"):
            content = str(rec.get("content") or "")
            words += len(content.split())
            domains.add(_source_from_url(rec.get("url") or ""))
    return words, len(domains)


def read_url(url: str, out_dir: str, source: str | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    resolved = resolve_url.resolve(url) or url
    if resolve_url.is_aggregator(resolved):
        rec = {"url": url, "source": source or _source_from_url(url),
               "ok": False, "words": 0, "content": "", "tier": "unresolved"}
    else:
        got = _fetch_jina(resolved) or _fetch_trafilatura(resolved)
        if got:
            tier, content = got
            rec = {"url": resolved, "source": source or _source_from_url(resolved),
                   "ok": True, "words": _word_count(content),
                   "content": content, "tier": tier}
        else:
            rec = {"url": resolved, "source": source or _source_from_url(resolved),
                   "ok": False, "words": 0, "content": "", "tier": "failed"}

    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()
    path = os.path.join(out_dir, f"{digest}.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False)
    os.replace(tmp, path)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser(description="Read one link via Jina for DEEP_RESEARCH")
    ap.add_argument("--url", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--source", default=None)
    args = ap.parse_args()

    rec = read_url(args.url, args.out_dir, args.source)
    words, sources = _cumulative(args.out_dir)
    if rec["ok"]:
        print(f"READ_OK: words={rec['words']} tier={rec['tier']} {rec['url']}", file=sys.stderr)
    else:
        print(f"READ_SKIP: tier={rec['tier']} {rec['url']}", file=sys.stderr)
    print(f"CUMULATIVE: words={words} sources={sources}", file=sys.stderr)
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
