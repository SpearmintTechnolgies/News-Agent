#!/usr/bin/env python3
"""
scan_headlines.py — Deterministic, project-aware HEADLINE_SCAN for Scout.

Usage:
  OUTPUT_FILE=/path/to/headlines.json TARGET_COUNT=10 \
    python3 scan_headlines.py [--project <slug>]

Reads project config (PROJECT_CONFIG / PROJECT_SLUG / manifest).
Prints SCAN_OK or SCAN_ERROR to stderr; writes headlines.json; exit 0 on success.

On failure (zero candidates), writes error JSON and exits 1 (triggers LLM fallback).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

# Pipeline project config
_PIPELINE = os.path.join(
    os.path.expanduser("~/.openclaw/workspace-orchestrator/skills/pipeline")
)
sys.path.insert(0, _PIPELINE)
import project_config as pc  # noqa: E402

# Local researcher helpers
_RESEARCHER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_RESEARCHER, "research-check"))
sys.path.insert(0, os.path.join(_RESEARCHER, "history"))

import resolve_url  # noqa: E402
import history_batch  # noqa: E402

UA = "Mozilla/5.0 (compatible; OpenClawScout/1.0)"
FEED_TIMEOUT = 12
FEED_RETRIES = 2
MAX_WORKERS = 8
SUMMARY_MAX = 280
CORROBORATING_MAX = 5
STOP_WORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
        "with", "by", "of", "is", "are", "was", "were", "be", "been", "has",
        "have", "had", "as", "its", "it", "from", "that", "this",
    }
)


@dataclass
class RawItem:
    headline: str
    url: str
    pub_date: datetime
    source: str
    summary: str
    corroborating_sources: list[dict[str, str]] = field(default_factory=list)


def _parse_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        from dateutil import parser as dup

        dt = dup.parse(text.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def clean_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def fetch_feed(source: str, url: str) -> tuple[str, str, bytes | None]:
    import subprocess

    last_err = ""
    for attempt in range(FEED_RETRIES):
        try:
            proc = subprocess.run(
                [
                    "curl", "-sL", "-A", UA,
                    "-o", "-", "--max-time", str(FEED_TIMEOUT), url,
                ],
                capture_output=True,
                timeout=FEED_TIMEOUT + 5,
            )
            if proc.returncode == 0 and proc.stdout:
                return source, url, proc.stdout
            last_err = proc.stderr.decode("utf-8", errors="replace")[:200]
        except (subprocess.SubprocessError, OSError) as e:
            last_err = str(e)
        if attempt < FEED_RETRIES - 1:
            import time
            time.sleep(0.3)
    print(f"[WARN] feed failed {source}: {last_err}", file=sys.stderr)
    return source, url, None


def _first_child(node: ET.Element, *tags: str) -> ET.Element | None:
    """Return first matching child; avoid Element truthiness (broken in Py3.12+)."""
    for tag in tags:
        found = node.find(tag)
        if found is not None:
            return found
    return None


def parse_feed_xml(source: str, body: bytes, max_age_hours: int) -> list[RawItem]:
    items: list[RawItem] = []
    # Strip illegal XML control chars (some feeds e.g. The Block include them)
    body = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", body)
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        print(f"[WARN] parse error {source}: {e}", file=sys.stderr)
        return items

    nodes = root.findall(".//item")
    if not nodes:
        nodes = root.findall(".//{http://www.w3.org/2005/Atom}entry")
    boundary = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    for idx, node in enumerate(nodes):
        title_node = _first_child(node, "title", "{http://www.w3.org/2005/Atom}title")
        headline = clean_html(title_node.text if title_node is not None and title_node.text else "")

        url = ""
        link_node = node.find("link")
        if link_node is not None:
            if link_node.text and link_node.text.strip():
                url = link_node.text.strip()
            elif link_node.get("href"):
                url = link_node.get("href", "").strip()
        if not url:
            for child in node:
                if "link" in child.tag:
                    if child.text and child.text.strip():
                        url = child.text.strip()
                        break
                    if child.get("href"):
                        url = child.get("href", "").strip()
                        break

        pub_node = _first_child(
            node,
            "pubDate",
            "{http://www.w3.org/2005/Atom}published",
            "{http://www.w3.org/2005/Atom}updated",
            "published",
            "updated",
        )
        pub_text = pub_node.text.strip() if pub_node is not None and pub_node.text else ""
        parsed_date = _parse_date(pub_text)

        desc_node = _first_child(
            node,
            "description",
            "summary",
            "{http://www.w3.org/2005/Atom}summary",
            "{http://purl.org/rss/1.0/modules/content/}encoded",
        )
        summary = clean_html(desc_node.text if desc_node is not None and desc_node.text else "")
        if len(summary) > SUMMARY_MAX:
            summary = summary[:SUMMARY_MAX] + "..."

        if not headline or not url:
            continue
        if url.startswith("mailto:") or url.startswith("javascript:"):
            continue

        keep = False
        if parsed_date and parsed_date >= boundary:
            keep = True
        elif parsed_date is None and idx < 5:
            keep = True
            parsed_date = datetime.now(timezone.utc)

        if not keep:
            continue

        items.append(
            RawItem(
                headline=headline,
                url=url,
                pub_date=parsed_date or datetime.now(timezone.utc),
                source=source,
                summary=summary,
            )
        )
    return items


def matches_exclude(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in keywords if kw)


def compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Compile config keyword patterns as case-insensitive regexes; fall back to
    a literal match if a pattern is not valid regex."""
    out: list[re.Pattern] = []
    for p in patterns:
        if not p:
            continue
        try:
            out.append(re.compile(p, re.IGNORECASE))
        except re.error:
            out.append(re.compile(re.escape(p), re.IGNORECASE))
    return out


def passes_require(
    text: str,
    strong_re: list[re.Pattern],
    weak_re: list[re.Pattern],
    ctx_re: list[re.Pattern],
) -> bool:
    """Two-tier inclusion gate. Keep an item if a STRONG (unambiguous) term
    matches anywhere, OR a WEAK ticker (e.g. doge/pepe/bonk) matches AND a
    crypto-context term is also present (so 'hockey player Bonk' is dropped but
    'Bonk (BONK) price surge' is kept). No filter configured -> keep all."""
    if not strong_re and not weak_re:
        return True
    if any(r.search(text) for r in strong_re):
        return True
    if weak_re and any(r.search(text) for r in weak_re):
        if not ctx_re or any(r.search(text) for r in ctx_re):
            return True
    return False


def source_priority(name: str, order: list[str]) -> int:
    try:
        return order.index(name)
    except ValueError:
        return len(order) + 10


def get_tokens(text: str) -> set[str]:
    text_clean = re.sub(r"[^\w\s]", "", text.lower())
    return set(text_clean.split()) - STOP_WORDS


def is_near_duplicate(a: str, b: str) -> bool:
    t1, t2 = get_tokens(a), get_tokens(b)
    if not t1 or not t2:
        return False
    overlap = len(t1 & t2) / min(len(t1), len(t2))
    return overlap >= 0.8


def dedupe_items(items: list[RawItem], priority_order: list[str]) -> list[RawItem]:
    by_url: dict[str, RawItem] = {}
    for item in items:
        if item.url not in by_url:
            by_url[item.url] = item
            continue
        existing = by_url[item.url]
        if source_priority(item.source, priority_order) < source_priority(existing.source, priority_order):
            item.corroborating_sources.extend(existing.corroborating_sources)
            if existing.source != item.source:
                item.corroborating_sources.append({"source": existing.source, "url": existing.url})
            by_url[item.url] = item
        elif existing.source != item.source:
            if len(existing.corroborating_sources) < CORROBORATING_MAX:
                existing.corroborating_sources.append({"source": item.source, "url": item.url})

    url_deduped = list(by_url.values())
    result: list[RawItem] = []
    for item in url_deduped:
        merged = False
        for existing in result:
            if is_near_duplicate(item.headline, existing.headline):
                if len(existing.corroborating_sources) < CORROBORATING_MAX:
                    existing.corroborating_sources.append({"source": item.source, "url": item.url})
                if source_priority(item.source, priority_order) < source_priority(existing.source, priority_order):
                    old_src, old_url = existing.source, existing.url
                    existing.headline = item.headline
                    existing.url = item.url
                    existing.pub_date = item.pub_date
                    existing.source = item.source
                    existing.summary = item.summary or existing.summary
                    if old_src != existing.source and len(existing.corroborating_sources) < CORROBORATING_MAX:
                        existing.corroborating_sources.append({"source": old_src, "url": old_url})
                merged = True
                break
        if not merged:
            result.append(item)
    for item in result:
        item.corroborating_sources = item.corroborating_sources[:CORROBORATING_MAX]
    return result


def atomic_write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=os.path.dirname(path), delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def run_scan(
    *,
    project_slug: str | None,
    output_file: str,
    target_count: int,
) -> tuple[int, dict]:
    try:
        cfg = pc.load_project_config(slug=project_slug)
    except (FileNotFoundError, ValueError) as e:
        err = {"status": "error", "reason": "config_error", "detail": str(e)}
        atomic_write_json(output_file, err)
        print(f"SCAN_ERROR: {e}", file=sys.stderr)
        return 1, err

    slug = cfg.slug
    feeds = cfg.get_path("research.rss_feeds", []) or []
    exclude_kw = [str(k) for k in (cfg.get_path("research.exclude_keywords", []) or [])]
    require_kw = [str(k) for k in (cfg.get_path("research.require_keywords", []) or [])]
    require_weak_kw = [str(k) for k in (cfg.get_path("research.require_weak_keywords", []) or [])]
    require_ctx_kw = [str(k) for k in (cfg.get_path("research.require_context_keywords", []) or [])]
    priority = [str(s) for s in (cfg.get_path("research.source_priority_order", []) or [])]
    max_age = int(cfg.get_path("research.max_age_hours", 24) or 24)

    if not feeds:
        err = {"status": "error", "reason": "no_feeds"}
        atomic_write_json(output_file, err)
        print("SCAN_ERROR: no rss_feeds in project config", file=sys.stderr)
        return 1, err

    # Parallel feed fetch
    feed_pairs: list[tuple[str, str]] = []
    for f in feeds:
        if isinstance(f, dict) and f.get("url"):
            feed_pairs.append((str(f.get("source") or "Unknown"), str(f["url"])))

    all_items: list[RawItem] = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(feed_pairs))) as pool:
        futures = [pool.submit(fetch_feed, src, url) for src, url in feed_pairs]
        for fut in as_completed(futures):
            source, _url, body = fut.result()
            if body:
                all_items.extend(parse_feed_xml(source, body, max_age))

    print(f"[INFO] raw items after parse: {len(all_items)}", file=sys.stderr)

    # Resolve aggregator URLs in parallel
    url_map = resolve_url.resolve_many([it.url for it in all_items])
    resolved_items: list[RawItem] = []
    for item in all_items:
        resolved = url_map.get(item.url)
        if not resolved or resolve_url.is_aggregator(resolved):
            continue
        item.url = resolved
        resolved_items.append(item)

    # Exclude keywords (hard drop)
    filtered: list[RawItem] = []
    for item in resolved_items:
        blob = f"{item.headline} {item.summary}"
        if matches_exclude(blob, exclude_kw):
            continue
        filtered.append(item)

    # Inclusion gate (two-tier): only when require_keywords are configured.
    # Strong terms match anywhere; weak tickers must co-occur with crypto context.
    strong_re = compile_patterns(require_kw)
    weak_re = compile_patterns(require_weak_kw)
    ctx_re = compile_patterns(require_ctx_kw)
    if strong_re or weak_re:
        before = len(filtered)
        filtered = [
            it
            for it in filtered
            if passes_require(f"{it.headline} {it.summary}", strong_re, weak_re, ctx_re)
        ]
        print(
            f"[INFO] require-filter kept {len(filtered)}/{before} project={slug}",
            file=sys.stderr,
        )

    deduped = dedupe_items(filtered, priority)

    # Batched history gate
    urls = [it.url for it in deduped]
    exists = history_batch.existing_urls(urls, slug)
    surviving = [it for it in deduped if it.url not in exists]

    # Relevance is enforced up front by the inclusion gate (require_keywords);
    # here we simply order by recency for every project.
    surviving.sort(key=lambda x: x.pub_date, reverse=True)

    final = surviving[:target_count]

    if not final:
        err = {"status": "error", "reason": "no_candidates"}
        atomic_write_json(output_file, err)
        print("SCAN_ERROR: no_candidates", file=sys.stderr)
        return 1, err

    candidates: list[dict[str, Any]] = []
    for i, item in enumerate(final, 1):
        candidates.append(
            {
                "candidate_index": i,
                "headline": item.headline,
                "url": item.url,
                "pub_date": item.pub_date.isoformat().replace("+00:00", "Z"),
                "source": item.source,
                "summary": item.summary,
                "corroborating_sources": item.corroborating_sources,
            }
        )

    out = {
        "status": "ok",
        "mode": "headline_scan",
        "project": slug,
        "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target_count": target_count,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    atomic_write_json(output_file, out)
    print(f"SCAN_OK: {len(candidates)} candidates project={slug}", file=sys.stderr)
    return 0, out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("PROJECT_SLUG"))
    parser.add_argument(
        "--output",
        default=os.environ.get("OUTPUT_FILE"),
        help="Path to headlines.json",
    )
    parser.add_argument(
        "--target-count",
        type=int,
        default=int(os.environ.get("TARGET_COUNT", "10")),
    )
    args = parser.parse_args()

    if not args.output:
        print("SCAN_ERROR: OUTPUT_FILE required", file=sys.stderr)
        return 1

    try:
        code, _ = run_scan(
            project_slug=args.project,
            output_file=os.path.realpath(args.output),
            target_count=max(1, args.target_count),
        )
        return code
    except Exception as e:
        err = {"status": "error", "reason": "scanner_exception", "detail": str(e)}
        try:
            atomic_write_json(args.output, err)
        except OSError:
            pass
        print(f"SCAN_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
