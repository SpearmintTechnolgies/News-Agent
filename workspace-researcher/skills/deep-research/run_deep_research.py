#!/usr/bin/env python3
"""
run_deep_research.py — Deterministic DEEP_RESEARCH extractor for Scout.

Usage:
  python3 run_deep_research.py --input picks.json --pick-index 1 --output raw.json
  python3 run_deep_research.py ... --discover-aggressive

Resolves URLs, parallel-extracts, discovers corroborating sources from pool/RSS
when content is thin, assembles raw.json. Zero LLM tokens.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

_PIPELINE = os.path.join(
    os.path.expanduser("~/.openclaw/workspace-orchestrator/skills/pipeline")
)
_HEADLINE_SCAN = os.path.join(
    os.path.expanduser("~/.openclaw/workspace-researcher/skills/headline-scan")
)
_RESEARCHER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEEP = os.path.dirname(os.path.abspath(__file__))

for p in (_PIPELINE, _HEADLINE_SCAN, os.path.join(_RESEARCHER, "research-check"),
          os.path.join(_RESEARCHER, "history"), _DEEP):
    if p not in sys.path:
        sys.path.insert(0, p)

import project_config as pc  # noqa: E402
import editorial_db as db  # noqa: E402
import resolve_url  # noqa: E402
import history_batch  # noqa: E402
import extract_article  # noqa: E402
import scan_headlines  # noqa: E402

PROSE_MIN_WORDS = 600
MIN_SOURCES = 2
MAX_URLS = 6
MAX_WORKERS = 6
OVERLAP_NORMAL = 0.5
OVERLAP_AGGRESSIVE = 0.35

ASSET_PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bbitcoin\b|\bbtc\b", re.I), "Bitcoin", "bitcoin"),
    (re.compile(r"\bethereum\b|\beth\b", re.I), "Ethereum", "ethereum"),
    (re.compile(r"\bxrp\b|\bripple\b", re.I), "XRP", "ripple"),
    (re.compile(r"\bsolana\b|\bsol\b", re.I), "Solana", "solana"),
    (re.compile(r"\bbnb\b|\bbinance coin\b", re.I), "BNB", "binancecoin"),
    (re.compile(r"\bdoge\b|\bdogecoin\b", re.I), "Dogecoin", "dogecoin"),
    (re.compile(r"\bada\b|\bcardano\b", re.I), "Cardano", "cardano"),
    (re.compile(r"\bavax\b|\bavalanche\b", re.I), "Avalanche", "avalanche-2"),
    (re.compile(r"\bpolkadot\b|\bdot\b", re.I), "Polkadot", "polkadot"),
    (re.compile(r"\bchainlink\b|\blink\b", re.I), "Chainlink", "chainlink"),
]


@dataclass
class SourceExtract:
    url: str
    source: str
    content: str = ""
    words: int = 0
    ok: bool = False
    tier: str | None = None


@dataclass
class RunState:
    pick: dict[str, Any]
    project_slug: str
    headline: str
    tried_urls: set[str] = field(default_factory=set)
    extracts: list[SourceExtract] = field(default_factory=list)
    pending_corro: list[dict[str, str]] = field(default_factory=list)


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or "story"


def headline_overlap(a: str, b: str, threshold: float) -> bool:
    t1, t2 = scan_headlines.get_tokens(a), scan_headlines.get_tokens(b)
    if not t1 or not t2:
        return False
    return len(t1 & t2) / min(len(t1), len(t2)) >= threshold


def curl_http_code(url: str) -> int:
    try:
        proc = subprocess.run(
            ["curl", "-o", "/dev/null", "-s", "-w", "%{http_code}", "-L",
             "--max-time", "15", url],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return int(proc.stdout.strip() or "0")
    except (subprocess.SubprocessError, OSError, ValueError):
        return 0


def is_url_dead(code: int) -> bool:
    return code in (404, 410)


def source_name_from_url(url: str, fallback: str = "Unknown") -> str:
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return host.split(".")[0].title() if host else fallback
    except Exception:
        return fallback


def detect_asset(text: str) -> tuple[str, str]:
    for pat, asset, coin in ASSET_PATTERNS:
        if pat.search(text):
            return asset, coin
    return "Bitcoin", "bitcoin"


def extract_key_facts(content: str, max_facts: int = 4) -> list[str]:
    """Pull substantive sentences (numbers, $, %, quotes) from prose."""
    sentences = re.split(r"(?<=[.!?])\s+", content or "")
    scored: list[tuple[int, str]] = []
    for s in sentences:
        s = s.strip()
        if len(s) < 30:
            continue
        score = 0
        if re.search(r"\d", s):
            score += 2
        if "$" in s or "%" in s:
            score += 2
        if '"' in s or "'" in s:
            score += 1
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    facts = [s for _, s in scored[:max_facts]]
    if len(facts) < 2:
        for s in sentences:
            s = s.strip()
            if len(s) >= 40 and s not in facts:
                facts.append(s)
            if len(facts) >= 2:
                break
    return facts[:max_facts]


def total_words(extracts: list[SourceExtract]) -> int:
    return sum(e.words for e in extracts if e.ok)


def successful_sources(extracts: list[SourceExtract]) -> list[SourceExtract]:
    seen: set[str] = set()
    out: list[SourceExtract] = []
    for e in extracts:
        if not e.ok:
            continue
        key = urlparse(e.url).netloc.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def meets_target(extracts: list[SourceExtract]) -> bool:
    ok = successful_sources(extracts)
    return total_words(extracts) >= PROSE_MIN_WORDS and len(ok) >= MIN_SOURCES


def resolve_candidate(url: str) -> str | None:
    resolved = resolve_url.resolve(url)
    if resolved and not resolve_url.is_aggregator(resolved):
        return resolved
    return None


def extract_one(url: str, source: str) -> SourceExtract:
    result = extract_article.extract(url)
    if result.get("ok"):
        return SourceExtract(
            url=url,
            source=source,
            content=str(result.get("content") or ""),
            words=int(result.get("words") or 0),
            ok=True,
            tier=str(result.get("tier") or ""),
        )
    return SourceExtract(url=url, source=source, ok=False)


def parallel_extract(urls_sources: list[tuple[str, str]]) -> list[SourceExtract]:
    if not urls_sources:
        return []
    results: list[SourceExtract] = []
    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(urls_sources))) as pool:
        futs = {pool.submit(extract_one, u, s): (u, s) for u, s in urls_sources}
        for fut in as_completed(futs):
            results.append(fut.result())
    return results


def load_pick(input_file: str, pick_index: int) -> dict[str, Any] | None:
    with open(input_file, encoding="utf-8") as f:
        doc = json.load(f)
    picks = doc.get("picks") or []
    for p in picks:
        if int(p.get("pick_index", -1)) == pick_index:
            return p
    return None


def build_initial_queue(pick: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return list of (original_url, resolved_url, source_name)."""
    entries: list[tuple[str, str, str]] = []
    primary = str(pick.get("url") or "").strip()
    if primary:
        entries.append((primary, primary, str(pick.get("source") or source_name_from_url(primary))))
    for cs in pick.get("corroborating_sources") or []:
        if not isinstance(cs, dict):
            continue
        u = str(cs.get("url") or "").strip()
        if u:
            entries.append((u, u, str(cs.get("source") or source_name_from_url(u))))
    return entries


def resolve_and_filter_queue(
    entries: list[tuple[str, str, str]],
    tried: set[str],
) -> list[tuple[str, str]]:
    """Resolve URLs, drop dead/unresolvable, return (url, source) ready to extract."""
    ready: list[tuple[str, str]] = []
    for orig, _res, source in entries:
        if orig in tried:
            continue
        resolved = resolve_candidate(orig)
        if not resolved:
            continue
        if resolved in tried:
            continue
        code = curl_http_code(resolved)
        if is_url_dead(code):
            print(f"[WARN] dead URL {code}: {resolved}", file=sys.stderr)
            tried.add(resolved)
            continue
        ready.append((resolved, source))
    return ready


def discover_from_pool(
    state: RunState,
    threshold: float,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        pool = db.fresh_pool(state.project_slug, limit=200)
    except Exception as e:
        print(f"[WARN] pool discovery failed: {e}", file=sys.stderr)
        return out
    for row in pool:
        if row.url in state.tried_urls:
            continue
        if not headline_overlap(state.headline, row.headline or "", threshold):
            continue
        resolved = resolve_candidate(row.url)
        if not resolved or resolved in state.tried_urls:
            continue
        src = row.source or source_name_from_url(resolved)
        out.append((resolved, src))
        corro_raw = row.corroborating_json
        if corro_raw:
            try:
                corro = json.loads(corro_raw)
                if isinstance(corro, list):
                    for cs in corro:
                        if isinstance(cs, dict) and cs.get("url"):
                            state.pending_corro.append(cs)
            except json.JSONDecodeError:
                pass
    return out


def discover_from_rss(
    state: RunState,
    cfg: pc.ProjectConfig,
    threshold: float,
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    feeds = cfg.get_path("research.rss_feeds", []) or []
    max_age = int(cfg.get_path("research.max_age_hours", 24) or 24)
    feed_pairs: list[tuple[str, str]] = []
    for f in feeds:
        if isinstance(f, dict) and f.get("url"):
            feed_pairs.append((str(f.get("source") or "Unknown"), str(f["url"])))
    if not feed_pairs:
        return out
    all_items: list[scan_headlines.RawItem] = []
    with ThreadPoolExecutor(max_workers=min(scan_headlines.MAX_WORKERS, len(feed_pairs))) as pool:
        futs = [pool.submit(scan_headlines.fetch_feed, src, url) for src, url in feed_pairs]
        for fut in as_completed(futs):
            source, _url, body = fut.result()
            if body:
                all_items.extend(scan_headlines.parse_feed_xml(source, body, max_age))
    url_map = resolve_url.resolve_many([it.url for it in all_items])
    for item in all_items:
        resolved = url_map.get(item.url)
        if not resolved or resolve_url.is_aggregator(resolved):
            continue
        if resolved in state.tried_urls:
            continue
        if not headline_overlap(state.headline, item.headline, threshold):
            continue
        out.append((resolved, item.source))
    return out


def filter_history(urls: list[str], project: str) -> list[str]:
    if not urls:
        return []
    exists = history_batch.existing_urls(urls, project)
    return [u for u in urls if u not in exists]


def assemble_raw_json(state: RunState) -> dict[str, Any]:
    pick = state.pick
    ok_sources = successful_sources(state.extracts)
    prose_parts = [e.content for e in ok_sources if e.content]
    aggregated = "\n\n---\n\n".join(prose_parts)
    all_facts: list[str] = []
    for e in ok_sources:
        all_facts.extend(extract_key_facts(e.content, max_facts=3))
    # Dedupe facts while preserving order
    seen: set[str] = set()
    combined_facts: list[str] = []
    for f in all_facts:
        key = f[:80]
        if key not in seen:
            seen.add(key)
            combined_facts.append(f)
    while len(combined_facts) < 2 and prose_parts:
        combined_facts.append(prose_parts[0][:200])

    headline = str(pick.get("headline") or pick.get("primary_headline") or "Untitled")
    blob = f"{headline} {aggregated[:500]}"
    primary_asset, chart_coin = detect_asset(blob)

    return {
        "status": "ok",
        "mode": "deep_research",
        "story_id": slugify(headline),
        "category": pick.get("category") or "",
        "wp_category_slugs": pick.get("wp_category_slugs") or [],
        "wp_category_ids": pick.get("wp_category_ids") or [],
        "topic_theme": headline,
        "primary_keyword": " ".join(headline.split()[:3]),
        "primary_headline": headline,
        "primary_asset": primary_asset,
        "chart_coin": chart_coin,
        "sources_used": [e.source for e in ok_sources],
        "source_urls": [e.url for e in ok_sources],
        "combined_key_facts": combined_facts[:8],
        "aggregated_raw_content": aggregated,
        "partial_words": total_words(state.extracts),
    }


def write_error(reason: str, output: str, *, partial_words: int = 0, detail: str = "") -> dict:
    err: dict[str, Any] = {"status": "error", "reason": reason}
    if partial_words:
        err["partial_words"] = partial_words
    if detail:
        err["detail"] = detail
    atomic_write_json(output, err)
    return err


def run_deep_research(
    *,
    input_file: str,
    pick_index: int,
    output_file: str,
    project_config: str | None = None,
    discover_aggressive: bool = False,
) -> tuple[int, dict]:
    pick = load_pick(input_file, pick_index)
    if not pick:
        err = write_error("pick_index_not_found", output_file)
        print("DEEP_RESEARCH_ERROR: pick_index_not_found", file=sys.stderr)
        return 1, err

    try:
        if project_config:
            os.environ["PROJECT_CONFIG"] = project_config
        cfg = pc.load_project_config()
        project_slug = cfg.slug
    except (FileNotFoundError, ValueError) as e:
        err = write_error("config_error", output_file, detail=str(e))
        print(f"DEEP_RESEARCH_ERROR: {e}", file=sys.stderr)
        return 1, err

    headline = str(pick.get("headline") or "")
    state = RunState(pick=pick, project_slug=project_slug, headline=headline)
    threshold = OVERLAP_AGGRESSIVE if discover_aggressive else OVERLAP_NORMAL

    # Phase A: initial URL queue
    queue_entries = build_initial_queue(pick)
    resolved_ready = resolve_and_filter_queue(queue_entries, state.tried_urls)
    if not resolved_ready:
        err = write_error("url_unresolvable", output_file)
        print("DEEP_RESEARCH_ERROR: url_unresolvable", file=sys.stderr)
        return 1, err

    # Phase B/C loop: extract + discover until target met or max URLs
    rss_discovered = False
    while len(state.tried_urls) < MAX_URLS:
        batch: list[tuple[str, str]] = []
        for url, source in resolved_ready:
            if url not in state.tried_urls and len(state.tried_urls) + len(batch) < MAX_URLS:
                batch.append((url, source))
        if batch:
            urls_only = [u for u, _ in batch]
            allowed = set(filter_history(urls_only, project_slug))
            batch = [(u, s) for u, s in batch if u in allowed]
            for u, _ in batch:
                state.tried_urls.add(u)
            new_extracts = parallel_extract(batch)
            state.extracts.extend(new_extracts)

        if meets_target(state.extracts):
            break

        if len(state.tried_urls) >= MAX_URLS:
            break

        # Discovery phase
        discovered: list[tuple[str, str]] = []
        # Pending corroborating from pick not yet tried
        for cs in pick.get("corroborating_sources") or []:
            if isinstance(cs, dict) and cs.get("url"):
                u = str(cs["url"])
                if u not in state.tried_urls:
                    state.pending_corro.append(cs)
        for cs in state.pending_corro:
            u = str(cs.get("url") or "").strip()
            if u and u not in state.tried_urls:
                discovered.append((u, str(cs.get("source") or source_name_from_url(u))))

        discovered.extend(discover_from_pool(state, threshold))
        if discover_aggressive or not discovered:
            if not rss_discovered or discover_aggressive:
                discovered.extend(discover_from_rss(state, cfg, threshold))
                rss_discovered = True

        discovered = [(u, s) for u, s in discovered if u not in state.tried_urls]
        urls = [u for u, _ in discovered]
        allowed = set(filter_history(urls, project_slug))
        discovered = [(u, s) for u, s in discovered if u in allowed]

        resolved_ready = resolve_and_filter_queue(
            [(u, u, s) for u, s in discovered],
            state.tried_urls,
        )
        if not resolved_ready:
            break

    partial = total_words(state.extracts)
    ok_count = len(successful_sources(state.extracts))

    if partial == 0 and not successful_sources(state.extracts):
        err = write_error("all_urls_dead", output_file, partial_words=0)
        print("DEEP_RESEARCH_ERROR: all_urls_dead", file=sys.stderr)
        return 1, err

    if meets_target(state.extracts):
        out = assemble_raw_json(state)
        atomic_write_json(output_file, out)
        print(
            f"DEEP_RESEARCH_OK: words={total_words(state.extracts)} "
            f"sources={ok_count} urls={len(state.tried_urls)}",
            file=sys.stderr,
        )
        return 0, out

    # Partial success — write diagnostic bundle for Scout fallback
    out = assemble_raw_json(state)
    out["status"] = "partial"
    out["reason"] = "insufficient_content"
    out["partial_words"] = partial
    atomic_write_json(output_file, out)
    print(
        f"DEEP_RESEARCH_PARTIAL: words={partial} sources={ok_count} "
        f"need>={PROSE_MIN_WORDS}w and >={MIN_SOURCES} sources",
        file=sys.stderr,
    )
    return 1, out


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic DEEP_RESEARCH extractor")
    parser.add_argument("--input", required=True, help="Path to picks.json")
    parser.add_argument("--pick-index", type=int, required=True)
    parser.add_argument("--output", required=True, help="Path to raw.json")
    parser.add_argument("--project-config", default=os.environ.get("PROJECT_CONFIG"))
    parser.add_argument(
        "--discover-aggressive",
        action="store_true",
        help="Lower headline overlap threshold and force RSS discovery",
    )
    args = parser.parse_args()

    try:
        code, _ = run_deep_research(
            input_file=os.path.realpath(args.input),
            pick_index=args.pick_index,
            output_file=os.path.realpath(args.output),
            project_config=args.project_config,
            discover_aggressive=args.discover_aggressive,
        )
        return code
    except Exception as e:
        write_error("scanner_exception", args.output, detail=str(e))
        print(f"DEEP_RESEARCH_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
