#!/usr/bin/env python3
"""
build_research_json.py — Assemble the DEEP_RESEARCH Fact File from read_tool
outputs. Deterministic, zero LLM tokens.

Reads every source record written by read_tool.py into --out-dir, dedupes by
domain, and writes a schema-correct raw.json that satisfies check_research.py
(>= 600 words, >= 2 source URLs, >= 2 key facts, no aggregator wrappers). Copies
category / wp_category fields straight through from picks.json. On zero usable
content it writes a clean error JSON the orchestrator can skip cleanly.

Usage:
  python3 build_research_json.py \
    --input picks.json --pick-index 1 \
    --out-dir /run/research/sources --output /run/research/raw.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any
from urllib.parse import urlparse

PROSE_MIN_WORDS = 600
MIN_SOURCES = 2

UNKNOWN_ASSET = "Unknown"
UNKNOWN_CHART_COIN = ""

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
    (re.compile(r"\bzcash\b|\bzec\b", re.I), "Zcash", "zcash"),
]


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False, suffix=".tmp",
                                     encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", (text or "").lower())
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or "story"


def _first_asset_by_position(text: str) -> tuple[str, str] | None:
    """Return (asset, coin) for the earliest textual match in ``text``.

    When multiple supported assets appear, the earliest start index wins — not
    ``ASSET_PATTERNS`` list order. Equal start: longer match span, then list
    order as a final tie-break.
    """
    best: tuple[int, int, int, str, str] | None = None
    for idx, (pat, asset, coin) in enumerate(ASSET_PATTERNS):
        m = pat.search(text or "")
        if not m:
            continue
        # Sort key: earlier start, then longer span (more specific), then list idx.
        cand = (m.start(), -len(m.group()), idx, asset, coin)
        if best is None or cand[:3] < best[:3]:
            best = cand
    if best is None:
        return None
    return best[3], best[4]


def detect_asset(headline: str, aggregate: str = "") -> tuple[str, str]:
    """Headline-first asset detection; body text only if headline has no match.

    Step 1: match against headline only (textual position if multiple).
    Step 2: if none, match against aggregate/body (textual position, not list order).
    Step 3: if nothing matches, return Unknown with an empty chart_coin — never
    silently default to Bitcoin.
    """
    hit = _first_asset_by_position(headline)
    if hit is not None:
        return hit
    hit = _first_asset_by_position(aggregate)
    if hit is not None:
        return hit
    return UNKNOWN_ASSET, UNKNOWN_CHART_COIN


def is_unknown_asset(asset: str) -> bool:
    return (asset or "").strip().lower() in ("", "unknown")


# ── Extraction V1.6: two-stage material fact selection ─────────────────────

PER_SOURCE_CANDIDATES = 6
FINAL_FACT_CAP = 12
MIN_SENTENCE_LEN = 30
MIN_FACTS = 2

_STOP = frozenset(
    {
        "the", "and", "for", "its", "are", "was", "has", "have", "with", "that",
        "this", "from", "not", "but", "can", "will", "all", "new", "into", "over",
        "more", "than", "after", "before", "about", "their", "they", "been", "also",
    }
)

APP_STORE_DOMAINS = frozenset({"play.google.com", "apps.apple.com"})

# Weak tokens that must not alone satisfy secondary-source relevance.
SECONDARY_WEAK_TOKENS = frozenset(
    {
        "the", "crypto", "cryptocurrency", "cryptocurrencies", "trading", "trade",
        "traders", "investors", "investor", "market", "markets", "network", "networks",
        "launch", "launches", "launched", "news", "price", "prices", "coin", "coins",
        "token", "tokens", "digital", "asset", "assets", "blockchain", "defi",
        "protocol", "protocols", "platform", "platforms", "company", "companies",
    }
)

SECONDARY_MIN_ANCHOR_OVERLAP = 2
SECONDARY_CONTENT_SAMPLE = 2000


def _gate_tokens(text: str) -> set[str]:
    """Meaningful tokens for secondary-source gating (len>=3, local stopwords)."""
    return _meaningful_tokens(text)


def _is_primary_source(source: dict, pick_url: str, pick_dom: str) -> bool:
    url = str(source.get("url") or "").strip()
    if not url:
        return False
    if pick_url and url.rstrip("/") == pick_url.rstrip("/"):
        return True
    dom = domain_of(url)
    if pick_dom and dom and dom == pick_dom:
        return True
    return False


def secondary_source_relevant(headline: str, source: dict) -> bool:
    """True when a non-primary source shares enough story anchors with the headline.

    Requires at least ``SECONDARY_MIN_ANCHOR_OVERLAP`` shared meaningful tokens, and
    at least one of those must not be in ``SECONDARY_WEAK_TOKENS`` (so a lone
    homonym like \"Maya\" or pure \"crypto/market\" overlap cannot pass).
    """
    headline_toks = _gate_tokens(headline)
    if not headline_toks:
        return False
    blob = " ".join(
        [
            str(source.get("title") or ""),
            str(source.get("snippet") or ""),
            str(source.get("content") or "")[:SECONDARY_CONTENT_SAMPLE],
            str(source.get("url") or ""),
        ]
    )
    source_toks = _gate_tokens(blob)
    overlap = headline_toks & source_toks
    if len(overlap) < SECONDARY_MIN_ANCHOR_OVERLAP:
        return False
    strong = overlap - SECONDARY_WEAK_TOKENS
    return len(strong) >= 1


def filter_sources_for_facts(
    sources: list[dict],
    headline: str,
    pick_url: str,
) -> tuple[list[dict], list[dict]]:
    """Keep primary + sufficiently relevant secondaries for fact extraction.

    Source JSON records are not deleted; rejected secondaries simply do not
    contribute candidates. If the pick URL/domain is not among loaded sources
    (unit-test packs), skip gating so existing fixtures keep working.
    """
    pick_dom = domain_of(pick_url)
    has_primary = any(_is_primary_source(s, pick_url, pick_dom) for s in sources)
    if not has_primary:
        return list(sources), []
    kept: list[dict] = []
    rejected: list[dict] = []
    for s in sources:
        if _is_primary_source(s, pick_url, pick_dom):
            kept.append(s)
        elif secondary_source_relevant(headline, s):
            kept.append(s)
        else:
            rejected.append(s)
    return kept, rejected


EVENT_VERB_RE = re.compile(
    r"\b("
    r"launch(?:ed|es|ing)?|acquire(?:d|s)?|acquisition|clos(?:e|ed|ing)|"
    r"approv(?:e|ed|es|al)|reject(?:ed|s)?|register(?:ed|s|ing)?|registration|"
    r"list(?:ed|ing|s)?|delist(?:ed|ing|s)?|partner(?:ed|s|ship)?|"
    r"integrat(?:e|ed|es|ion)|introduc(?:e|ed|es|ing)|releas(?:e|ed|es|ing)|"
    r"announc(?:e|ed|es|ing)|invest(?:ed|s|ment)?"
    r")\b",
    re.I,
)

REGULATORY_RE = re.compile(
    r"\b("
    r"fca|sec|cftc|regulator(?:y|s)?|regulation|registration|registered|"
    r"licen[cs]e(?:s|d)?|compliance|compliant|framework|anti-?money|"
    r"laundering|fscs|fdic|sipc|ombudsman"
    r")\b",
    re.I,
)

PRODUCT_RE = re.compile(
    r"\b("
    r"product|feature|digest(?:s)?|cortex|ai-?powered|generative\s+ai|"
    r"introduces|integrates|education\s+tool"
    r")\b",
    re.I,
)

MARKETING_CTA_RE = re.compile(
    r"\b("
    r"get\s+started|as\s+little\s+as|sign\s+up|download|install\s+now|"
    r"earn\s+\d|apy\b|no\s+cap|members\s+get|zero\s+management\s+fees|"
    r"uninvested\s+cash"
    r")\b",
    re.I,
)

BIO_RE = re.compile(
    r"\b(has\s+been\s+a|holds\s+a\s+master|researcher\s+since|areas\s+of\s+focus|"
    r"author\s+bio|about\s+the\s+author)\b",
    re.I,
)

NEWS_PATH_RE = re.compile(r"/(news|article|articles|press|releases|blog)/", re.I)
BIO_PATH_RE = re.compile(r"/(author|authors|about|team|bio)/", re.I)
TICKER_RE = re.compile(r"\$[A-Za-z]{2,10}\b")
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _meaningful_tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 3 and w not in _STOP
    }


def _story_token_set(story_ctx: dict[str, str] | None) -> set[str]:
    story_ctx = story_ctx or {}
    blob = " ".join(
        str(story_ctx.get(k) or "")
        for k in ("primary_headline", "topic_theme", "primary_keyword", "primary_asset")
    )
    return _meaningful_tokens(blob)


def _is_apex_homepage(url: str) -> bool:
    try:
        p = urlparse(url or "")
        path = (p.path or "").rstrip("/")
        return path == "" or path == "/"
    except ValueError:
        return False


def _source_signal(url: str, domain: str) -> int:
    """Positive/negative source quality adjustment."""
    dom = (domain or domain_of(url) or "").lower()
    score = 0
    if dom in APP_STORE_DOMAINS:
        score -= 8
    if _is_apex_homepage(url) and dom not in APP_STORE_DOMAINS:
        # Company homepage can still host facts, but demote generic promo pages.
        score -= 6
    if BIO_PATH_RE.search(url or ""):
        score -= 6
    if NEWS_PATH_RE.search(url or ""):
        score += 2
    return score


def classify_fact_category(text: str) -> str:
    t = text or ""
    if REGULATORY_RE.search(t):
        return "REGULATORY"
    if TICKER_RE.search(t) and re.search(
        r"\b(include|including|assets?|tokens?|cryptocurrenc)", t, re.I
    ):
        return "ASSET_AVAILABILITY"
    if re.search(r"\b(launch(?:ed|es|ing)?|acquire(?:d|s)?|acquisition)\b", t, re.I):
        return "NEWS_EVENT"
    if PRODUCT_RE.search(t):
        return "PRODUCT_FEATURE"
    if re.search(r"\$\s*[\d,]+|\d+(?:\.\d+)?%|\b(?:million|billion)\b", t, re.I):
        return "FINANCIAL"
    if EVENT_VERB_RE.search(t):
        return "NEWS_EVENT"
    return "COMPANY_BACKGROUND"


def is_material_candidate(text: str, story_ctx: dict[str, str] | None = None) -> bool:
    """Stage 1: admit sentences with any strong material signal."""
    s = (text or "").strip()
    if len(s) < MIN_SENTENCE_LEN:
        return False
    if re.search(r"\d", s) or "$" in s or "%" in s:
        return True
    if YEAR_RE.search(s):
        return True
    if EVENT_VERB_RE.search(s):
        return True
    if REGULATORY_RE.search(s) or PRODUCT_RE.search(s):
        return True
    if TICKER_RE.search(s):
        return True
    story = _story_token_set(story_ctx)
    toks = _meaningful_tokens(s)
    if story and len(story & toks) >= 2:
        return True
    asset = str((story_ctx or {}).get("primary_asset") or "").strip().lower()
    if asset and asset in s.lower():
        return True
    # Capitalized multi-char names (org/product heuristic)
    if re.search(r"\b[A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,})+\b", s):
        return True
    return False


def materiality_score(
    text: str,
    story_ctx: dict[str, str] | None = None,
    *,
    url: str = "",
    domain: str = "",
) -> int:
    """Stage 2: deterministic materiality / relevance ranking."""
    s = (text or "").strip()
    score = 0
    story_ctx = story_ctx or {}

    # Base materiality
    if re.search(r"\$\s*[\d,]+|\d+(?:\.\d+)?%|\b(?:million|billion)\b", s, re.I):
        score += 2
    elif re.search(r"\d", s) or YEAR_RE.search(s):
        score += 1
    if EVENT_VERB_RE.search(s):
        score += 2
    if REGULATORY_RE.search(s):
        score += 2
    if PRODUCT_RE.search(s):
        score += 2
    if TICKER_RE.search(s):
        score += 1
        # Multi-ticker availability lists are high-value story context.
        if len(TICKER_RE.findall(s)) >= 3 and re.search(
            r"\b(include|including|assets?|tokens?)\b", s, re.I
        ):
            score += 2

    # Named / story anchors
    story = _story_token_set(story_ctx)
    toks = _meaningful_tokens(s)
    overlap = len(story & toks)
    if overlap >= 3:
        score += 2
    elif overlap >= 1:
        score += 1
    else:
        # Off-story numeric blurbs (unrelated TVL/DEX, etc.)
        if story and re.search(r"\$\s*[\d,]+|\d+(?:\.\d+)?%", s):
            score -= 3
    # Weak story tether + large money often marks unrelated digressions.
    if (
        overlap <= 1
        and re.search(r"\$\s*[\d,]+\s*(?:billion|million)|\$\s*\d+\s*b\b", s, re.I)
        and not re.search(r"\brobinhood\b|\bbitstamp\b", s, re.I)
    ):
        score -= 4
    asset = str(story_ctx.get("primary_asset") or "").strip().lower()
    if asset and asset in s.lower():
        score += 1
    if re.search(r"\b[A-Z][A-Za-z0-9]{2,}(?:\s+[A-Z][A-Za-z0-9]{2,})+\b", s):
        score += 2

    # Source / URL signals
    score += _source_signal(url, domain)

    # Marketing / bio demotion
    if MARKETING_CTA_RE.search(s):
        score -= 8
    if BIO_RE.search(s):
        score -= 8
    if domain_of(url) in APP_STORE_DOMAINS or (domain or "").lower() in APP_STORE_DOMAINS:
        score -= 4  # extra on top of domain signal

    # Quote-heavy executive commentary is weaker than hard news facts.
    if re.search(r"\b(commented|said|told reporters)\b", s, re.I) and s.count('"') + s.count("“") >= 1:
        score -= 2

    return score


def score_candidates(
    content: str,
    story_ctx: dict[str, str] | None = None,
    *,
    url: str = "",
    domain: str = "",
) -> list[dict[str, Any]]:
    """Return scored candidate dicts for one source body."""
    out: list[dict[str, Any]] = []
    for raw in re.split(r"(?<=[.!?])\s+", content or ""):
        s = raw.strip()
        if not is_material_candidate(s, story_ctx):
            continue
        score = materiality_score(s, story_ctx, url=url, domain=domain)
        # Drop hard marketing / bio after demotion
        if score < 1:
            continue
        out.append(
            {
                "text": s,
                "score": score,
                "category": classify_fact_category(s),
            }
        )
    out.sort(key=lambda x: (-int(x["score"]), -len(str(x["text"]))))
    return out


def diversify_select(
    scored: list[dict[str, Any]],
    *,
    max_total: int = FINAL_FACT_CAP,
) -> list[dict[str, Any]]:
    """
    Soft diversity: seed with the best fact per category, then fill by score.
    Does not invent facts to fill empty categories.
    """
    if not scored:
        return []

    def _dup_key(text: str) -> str:
        return (text or "")[:80]

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    by_cat: dict[str, list[dict[str, Any]]] = {}
    for cand in scored:
        cat = str(cand.get("category") or "COMPANY_BACKGROUND")
        by_cat.setdefault(cat, []).append(cand)

    # Pass 1 — one best per present category (already score-sorted within pool).
    for _cat, items in by_cat.items():
        item = items[0]
        key = _dup_key(str(item["text"]))
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= max_total:
            return selected

    # Pass 2 — fill remaining slots by global score order.
    for item in scored:
        key = _dup_key(str(item["text"]))
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= max_total:
            break
    return selected


def extract_key_facts(
    content: str,
    max_facts: int = PER_SOURCE_CANDIDATES,
    story_ctx: dict[str, str] | None = None,
    *,
    url: str = "",
    domain: str = "",
) -> list[str]:
    """
    Per-source candidate extraction (V1.6).

    Returns up to ``max_facts`` material sentences for one source body.
    Backward compatible with callers that only pass ``content``.
    """
    scored = score_candidates(content, story_ctx, url=url, domain=domain)
    facts = [str(c["text"]) for c in scored[:max_facts]]
    if len(facts) < MIN_FACTS:
        for s in re.split(r"(?<=[.!?])\s+", content or ""):
            s = s.strip()
            if len(s) >= 40 and s not in facts:
                facts.append(s)
            if len(facts) >= MIN_FACTS:
                break
    return facts[:max_facts]


def select_material_facts(
    sources: list[dict],
    story_ctx: dict[str, str],
    *,
    max_total: int = FINAL_FACT_CAP,
    per_source: int = PER_SOURCE_CANDIDATES,
) -> list[tuple[str, dict[str, str] | None, dict[str, Any]]]:
    """
    Global two-stage selection across sources.

    Returns list of (text, provenance_or_None, meta) preserving provenance
    for selected facts. Higher-scoring duplicate wins (richer version).
    """
    pool: list[dict[str, Any]] = []
    for s in sources:
        url = str(s.get("url") or "").strip()
        dom = domain_of(url)
        record_path = str(s.get("_record_path") or "").strip()
        scored = score_candidates(
            str(s.get("content") or ""),
            story_ctx,
            url=url,
            domain=dom,
        )[:per_source]
        for c in scored:
            prov: dict[str, str] | None = None
            if url and dom:
                prov = {
                    "text": str(c["text"]),
                    "source_url": url,
                    "source_domain": dom,
                    "source_record": record_path,
                }
            pool.append(
                {
                    "text": str(c["text"]),
                    "score": int(c["score"]),
                    "category": str(c["category"]),
                    "prov": prov,
                }
            )

    # Prefer higher score when near-duplicate keys collide
    best_by_key: dict[str, dict[str, Any]] = {}
    for item in pool:
        key = item["text"][:80]
        prev = best_by_key.get(key)
        if prev is None or int(item["score"]) > int(prev["score"]):
            best_by_key[key] = item
    deduped = sorted(
        best_by_key.values(),
        key=lambda x: (-int(x["score"]), -len(str(x["text"]))),
    )
    selected = diversify_select(deduped, max_total=max_total)
    out: list[tuple[str, dict[str, str] | None, dict[str, Any]]] = []
    for item in selected:
        meta = {"score": item["score"], "category": item["category"]}
        out.append((str(item["text"]), item.get("prov"), meta))
    return out


def load_json_file(path: str) -> Any:
    """Load JSON written by agents that sometimes save Windows-1252 on Windows."""
    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        raise ValueError(f"empty json file: {path}")
    last_err: Exception | None = None
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            last_err = e
            continue
    assert last_err is not None
    raise last_err


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def load_pick(input_file: str, pick_index: int) -> dict[str, Any] | None:
    doc = load_json_file(input_file)

    if isinstance(doc, list):
        picks = doc
    elif isinstance(doc, dict):
        if "picks" in doc and isinstance(doc["picks"], list):
            picks = doc["picks"]
        elif int(doc.get("pick_index", -1)) == pick_index or "url" in doc or "headline" in doc:
            return doc
        else:
            picks = []
    else:
        picks = []

    for p in picks:
        if isinstance(p, dict) and int(p.get("pick_index", -1)) == pick_index:
            return p

    if picks and isinstance(picks[0], dict):
        return picks[0]

    return None


def load_sources(out_dir: str) -> list[dict]:
    """Load ok records, dedupe by domain (keep the longest content per domain).

    Each retained record includes ``_record_path`` as
    ``research/sources/<filename>`` for optional fact provenance.
    """
    by_domain: dict[str, dict] = {}
    if not os.path.isdir(out_dir):
        return []
    for fn in sorted(os.listdir(out_dir)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(out_dir, fn), encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not rec.get("ok") or not rec.get("content"):
            continue
        dom = domain_of(rec.get("url") or "")
        if not dom:
            continue
        if dom not in by_domain or len(rec["content"]) > len(by_domain[dom]["content"]):
            stored = dict(rec)
            stored["_record_path"] = f"research/sources/{fn}"
            by_domain[dom] = stored
    return list(by_domain.values())


def write_error(reason: str, output: str) -> dict:
    err = {"status": "error", "reason": reason}
    atomic_write_json(output, err)
    return err


def build(input_file: str, pick_index: int, out_dir: str, output_file: str) -> tuple[int, dict]:
    pick = load_pick(input_file, pick_index)
    if not pick:
        print("BUILD_ERROR: pick_index_not_found", file=sys.stderr)
        return 1, write_error("pick_index_not_found", output_file)

    sources = load_sources(out_dir)
    if not sources:
        print("BUILD_ERROR: all_urls_dead", file=sys.stderr)
        return 1, write_error("all_urls_dead", output_file)

    prose_parts = [s["content"] for s in sources if s.get("content")]
    aggregated = "\n\n---\n\n".join(prose_parts)
    total_words = sum(int(s.get("words") or 0) for s in sources)

    headline = str(pick.get("headline") or pick.get("primary_headline") or "Untitled")
    pick_url = str(pick.get("url") or pick.get("primary_url") or "").strip()
    primary_asset, chart_coin = detect_asset(headline, aggregated[:500])
    # Keep Unknown out of relevance scoring so it cannot tether unrelated sentences.
    story_asset = "" if is_unknown_asset(primary_asset) else primary_asset
    story_ctx = {
        "primary_headline": headline,
        "topic_theme": headline,
        "primary_keyword": " ".join(headline.split()[:3]),
        "primary_asset": story_asset,
    }

    # Non-primary sources must share ≥2 meaningful headline anchors before
    # contributing facts. Source JSON on disk is left intact for audit.
    fact_sources, _rejected_secondary = filter_sources_for_facts(
        sources, headline, pick_url
    )

    # V1.6 two-stage material selection (global cap + soft diversity).
    selected = select_material_facts(fact_sources, story_ctx)
    facts_meta: list[tuple[str, dict[str, str] | None]] = [
        (text, prov) for text, prov, _meta in selected
    ]
    while len(facts_meta) < MIN_FACTS and prose_parts:
        # Padding keeps combined_key_facts compatible; no fabricated provenance.
        facts_meta.append((prose_parts[0][:200], None))

    facts = [t for t, _ in facts_meta]
    sourced_facts: list[dict[str, str]] = []
    for text, prov in facts_meta[:FINAL_FACT_CAP]:
        if not prov:
            continue
        sourced_facts.append(
            {
                "id": f"fact_{len(sourced_facts) + 1:03d}",
                "text": prov["text"],
                "source_url": prov["source_url"],
                "source_domain": prov["source_domain"],
                "source_record": prov["source_record"],
            }
        )

    pick_dom = domain_of(pick_url)

    def _image_ok(url: str) -> bool:
        u = url.lower()
        if not url.startswith("http"):
            return False
        bad = ("/brands/", "favicon", "150x150", "sprite", "logo.svg", "yimg.com/lb/")
        return not any(b in u for b in bad)

    source_image_url = ""
    for s in sources:
        img = str(s.get("image_url") or "").strip()
        if not _image_ok(img):
            continue
        if pick_dom and domain_of(s.get("url") or "") == pick_dom:
            source_image_url = img
            break
        if not source_image_url:
            source_image_url = img

    out = {
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
        "sources_used": [s.get("source") or domain_of(s["url"]) for s in sources],
        "source_urls": [s["url"] for s in sources],
        "source_image_url": source_image_url,
        "combined_key_facts": facts[:FINAL_FACT_CAP],
        "sourced_facts": sourced_facts,
        "aggregated_raw_content": aggregated,
        "partial_words": total_words,
    }
    atomic_write_json(output_file, out)

    meets = total_words >= PROSE_MIN_WORDS and len(sources) >= MIN_SOURCES
    if meets:
        print(f"BUILD_OK: words={total_words} sources={len(sources)}", file=sys.stderr)
        return 0, out
    out["status"] = "partial"
    out["reason"] = "insufficient_content"
    atomic_write_json(output_file, out)
    print(
        f"BUILD_PARTIAL: words={total_words} sources={len(sources)} "
        f"need>={PROSE_MIN_WORDS}w and >={MIN_SOURCES} sources",
        file=sys.stderr,
    )
    return 1, out


def main() -> int:
    ap = argparse.ArgumentParser(description="Assemble DEEP_RESEARCH raw.json from read_tool outputs")
    ap.add_argument("--input", required=True, help="picks.json")
    ap.add_argument("--pick-index", type=int, required=True)
    ap.add_argument("--out-dir", required=True, help="dir of read_tool source records")
    ap.add_argument("--output", required=True, help="raw.json path")
    args = ap.parse_args()
    try:
        code, _ = build(
            os.path.realpath(args.input), args.pick_index,
            os.path.realpath(args.out_dir), os.path.realpath(args.output),
        )
        return code
    except Exception as e:
        write_error("scanner_exception", args.output)
        print(f"BUILD_ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
