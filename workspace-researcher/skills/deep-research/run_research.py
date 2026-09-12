#!/usr/bin/env python3
"""
run_research.py — Deterministic DEEP_RESEARCH engine for Scout.

Runs search_tool + read_tool + build_research_json in one exec — zero LLM tokens.
This is the REQUIRED FIRST command for every DEEP_RESEARCH spawn.

Usage:
  python3 run_research.py --input picks.json --pick-index 1 --output raw.json --self-check
  python3 run_research.py ... --extra-search   # one more DDG round if still short

stderr: RESEARCH_OK | RESEARCH_PARTIAL + optional RESEARCH_CHECK: PASS|FAIL
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_HEADLINE_SCAN = os.path.join(os.path.dirname(_HERE), "headline-scan")
_RESEARCH_CHECK = os.path.join(os.path.dirname(_HERE), "research-check")
for _p in (_HERE, _HEADLINE_SCAN, _RESEARCH_CHECK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scan_headlines  # noqa: E402
import read_tool  # noqa: E402
import search_tool  # noqa: E402
import build_research_json as brj  # noqa: E402

PROSE_MIN_WORDS = 600
MIN_SOURCES = 2
MAX_JINA_ATTEMPTS = 6
MAX_URL_TRIES = 10
WALL_CLOCK_S = 90.0
OVERLAP_THRESHOLD = 0.35
PARALLEL_READS = 2 if read_tool.has_jina_key() else 1

_state_lock = threading.Lock()


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _overlap_score(headline: str, text: str) -> float:
    t1 = scan_headlines.get_tokens(headline)
    t2 = scan_headlines.get_tokens(text)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / min(len(t1), len(t2))


def _rank_candidates(headline: str, results: list[dict], skip_domain: str) -> list[dict]:
    scored: list[tuple[float, dict]] = []
    for r in results:
        dom = r.get("domain") or _domain(r.get("url") or "")
        if skip_domain and dom == skip_domain:
            continue
        blob = f"{r.get('title', '')} {r.get('snippet', '')}"
        score = _overlap_score(headline, blob)
        if score < OVERLAP_THRESHOLD:
            continue
        # Absolute-token gate: ratio alone can pass a one-token homonym
        # (e.g. "Download Maya" vs "Maya Protocol …"). Require the same
        # secondary-source anchor rule used at build time.
        pseudo = {
            "title": str(r.get("title") or ""),
            "snippet": str(r.get("snippet") or ""),
            "content": "",
            "url": str(r.get("url") or ""),
        }
        if not brj.secondary_source_relevant(headline, pseudo):
            continue
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]


def _cumulative(out_dir: str) -> tuple[int, int]:
    return read_tool._cumulative(out_dir)


def _meets_target(out_dir: str) -> bool:
    words, sources = _cumulative(out_dir)
    return words >= PROSE_MIN_WORDS and sources >= MIN_SOURCES


_EXTRA_STOP = frozenset({
    "the", "a", "an", "and", "or", "for", "of", "to", "in", "on", "as",
    "new", "select",
})


def _extra_query(headline: str, pick: dict[str, Any]) -> str:
    """Keyword query that is not identical to the headline (DDG + GNews)."""
    tokens = [
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9%-]*", headline or "")
        if t.lower() not in _EXTRA_STOP
    ]
    q = " ".join(tokens[:8]).strip()
    if not q:
        q = search_tool._sanitize_query(headline) if headline else ""
    return q or (headline or "").strip()


def _corroborating_urls(pick: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for cs in pick.get("corroborating_sources") or []:
        if not isinstance(cs, dict):
            continue
        url = str(cs.get("url") or "").strip()
        if url:
            src = str(cs.get("source") or _domain(url)).strip()
            out.append((url, src))
    return out


class _RunState:
    def __init__(self, out_dir: str, start: float) -> None:
        self.out_dir = out_dir
        self.start = start
        self.tried_domains: set[str] = set()
        self.jina_attempts = 0
        self.url_tries = 0

    def can_continue(self) -> bool:
        with _state_lock:
            if _meets_target(self.out_dir):
                return False
            if (time.time() - self.start) >= WALL_CLOCK_S:
                return False
            if self.jina_attempts >= MAX_JINA_ATTEMPTS:
                return False
            if self.url_tries >= MAX_URL_TRIES:
                return False
            return True

    def read_one(self, url: str, source: str) -> bool:
        dom = _domain(url)
        with _state_lock:
            if not url or dom in self.tried_domains:
                return False
            self.tried_domains.add(dom)
            if not self.can_continue_unlocked():
                return False
            self.url_tries += 1
            self.jina_attempts += 1

        rec = read_tool.read_url(url, self.out_dir, source)
        return bool(rec.get("ok"))

    def can_continue_unlocked(self) -> bool:
        if _meets_target(self.out_dir):
            return False
        if (time.time() - self.start) >= WALL_CLOCK_S:
            return False
        if self.jina_attempts >= MAX_JINA_ATTEMPTS:
            return False
        if self.url_tries >= MAX_URL_TRIES:
            return False
        return True


def _read_batch(state: _RunState, batch: list[tuple[str, str]]) -> None:
    """Read URLs sequentially or 2-wide parallel when JINA_API_KEY is set."""
    pending = [(u, s) for u, s in batch if u and _domain(u) not in state.tried_domains]
    if not pending:
        return

    if PARALLEL_READS <= 1 or len(pending) == 1:
        for url, src in pending:
            if not state.can_continue():
                break
            state.read_one(url, src)
            if _meets_target(state.out_dir):
                break
        return

    idx = 0
    while idx < len(pending) and state.can_continue() and not _meets_target(state.out_dir):
        chunk = pending[idx: idx + PARALLEL_READS]
        idx += len(chunk)
        with ThreadPoolExecutor(max_workers=len(chunk)) as pool:
            futs = [pool.submit(state.read_one, u, s) for u, s in chunk]
            for fut in as_completed(futs):
                fut.result()
        if _meets_target(state.out_dir):
            break


def _run_self_check(output_file: str) -> bool:
    import check_research as cr  # noqa: E402

    ok, results = cr.validate_file(output_file, "deep_research")
    for name, passed, detail in results:
        line = f"{'PASS' if passed else 'FAIL'}: {name} — {detail}"
        print(line, file=sys.stderr)
    verdict = "PASS" if ok else "FAIL"
    print(f"RESEARCH_CHECK: {verdict}", file=sys.stderr)
    return ok


def run_research(
    input_file: str,
    pick_index: int,
    output_file: str,
    *,
    extra_search: bool = False,
    self_check: bool = False,
) -> tuple[int, dict]:
    pick = brj.load_pick(input_file, pick_index)
    if not pick:
        err = {"status": "error", "reason": "pick_index_not_found"}
        brj.atomic_write_json(output_file, err)
        print("RESEARCH_ERROR: pick_index_not_found", file=sys.stderr)
        return 1, err

    headline = str(pick.get("headline") or pick.get("primary_headline") or "").strip()
    pick_url = str(pick.get("url") or "").strip()
    pick_source = str(pick.get("source") or "").strip()
    pick_domain = _domain(pick_url)

    out_dir = os.path.join(os.path.dirname(os.path.abspath(output_file)), "sources")
    os.makedirs(out_dir, exist_ok=True)

    start = time.time()
    state = _RunState(out_dir, start)
    search_results: list[dict] = []

    # Overlap: read pick URL + DDG search in parallel
    if pick_url and headline and state.can_continue():
        with ThreadPoolExecutor(max_workers=2) as pool:
            read_fut = pool.submit(
                state.read_one, pick_url, pick_source or pick_domain,
            )
            search_fut = pool.submit(search_tool.search, headline, max_results=8)
            read_fut.result()
            search_results = search_fut.result() or []
    elif pick_url and state.can_continue():
        state.read_one(pick_url, pick_source or pick_domain)
    elif headline and state.can_continue():
        search_results = search_tool.search(headline, max_results=8)

    # DDG candidates (ranked)
    if state.can_continue() and search_results:
        batch = [
            (str(c.get("url") or "").strip(),
             str(c.get("domain") or c.get("title") or "").strip())
            for c in _rank_candidates(headline, search_results, pick_domain)
        ]
        _read_batch(state, batch)

    # Corroborating sources from pick (often zero-latency if picker attached them)
    if state.can_continue():
        _read_batch(state, _corroborating_urls(pick))

    # Extra search round (always runs — do not skip when query == headline)
    if extra_search and state.can_continue() and headline:
        q = _extra_query(headline, pick)
        extra_results = search_tool.search(q, max_results=6)
        ranked = _rank_candidates(headline, extra_results, pick_domain)
        if not ranked:
            ranked = [
                c for c in extra_results
                if _domain(str(c.get("url") or "")) != pick_domain
            ][:4]
        batch = [
            (str(c.get("url") or "").strip(),
             str(c.get("domain") or c.get("title") or "").strip())
            for c in ranked
        ]
        _read_batch(state, batch)

    code, doc = brj.build(input_file, pick_index, out_dir, output_file)
    words, sources = _cumulative(out_dir)
    elapsed = time.time() - start

    if code == 0:
        print(
            f"RESEARCH_OK: words={words} sources={sources} "
            f"attempts={state.jina_attempts} elapsed={elapsed:.1f}s",
            file=sys.stderr,
        )
    else:
        print(
            f"RESEARCH_PARTIAL: words={words} sources={sources} "
            f"attempts={state.jina_attempts} elapsed={elapsed:.1f}s "
            f"need>={PROSE_MIN_WORDS}w and >={MIN_SOURCES} sources",
            file=sys.stderr,
        )

    if self_check:
        check_ok = _run_self_check(output_file)
        if not check_ok:
            return 1, doc

    return code, doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic DEEP_RESEARCH (search + read + build)")
    ap.add_argument("--input", required=True, help="picks.json path")
    ap.add_argument("--pick-index", type=int, required=True)
    ap.add_argument("--output", required=True, help="raw.json output path")
    ap.add_argument(
        "--extra-search",
        action="store_true",
        help="one additional DDG search round with broader keywords if still short",
    )
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="run check_research.py inline after build; prints RESEARCH_CHECK on stderr",
    )
    args = ap.parse_args()
    try:
        code, _ = run_research(
            os.path.realpath(args.input),
            args.pick_index,
            os.path.realpath(args.output),
            extra_search=args.extra_search,
            self_check=args.self_check,
        )
        return code
    except Exception as e:
        err = {"status": "error", "reason": "scanner_exception", "detail": str(e)}
        brj.atomic_write_json(args.output, err)
        print(f"RESEARCH_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
