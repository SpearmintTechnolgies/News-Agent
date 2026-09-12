#!/usr/bin/env python3
"""
verify_claims.py — Claim Verification V1 (offline-first, bounded search).

Reads research/validated.json + research/sources/*.json, optionally searches and
reads a few corroborating URLs, and writes research/verification.json.

Statuses (evidence support only — not objective truth):
  DIRECT | CORROBORATED | SINGLE_SOURCE | CONFLICTED | UNSUPPORTED

Usage:
  python3 verify_claims.py \\
    --validated /run/research/validated.json \\
    --sources-dir /run/research/sources \\
    --output /run/research/verification.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_HEADLINE_SCAN = os.path.join(os.path.dirname(_HERE), "headline-scan")
_RESEARCH_CHECK = os.path.join(os.path.dirname(_HERE), "research-check")
for _p in (_HERE, _HEADLINE_SCAN, _RESEARCH_CHECK):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import scan_headlines  # noqa: E402
import search_tool  # noqa: E402
import read_tool  # noqa: E402
import resolve_url  # noqa: E402

OVERLAP_THRESHOLD = 0.35
TOKEN_SUPPORT_THRESHOLD = 0.8
SOFT_PARAPHRASE_OVERLAP = 0.45
LONG_CLAIM_MIN_TOKENS = 8
MAX_SEARCH_RESULTS = 6
MAX_CANDIDATES_CONSIDERED = 3
MAX_FETCHES_PER_CLAIM = 2
MAX_FETCHES_PER_JOB = 6
QUERY_TOKEN_CAP = 12

# Weak tokens: may aid soft overlap but are NOT strong entity anchors alone.
WEAK_ENTITY_TOKENS = frozenset(
    {
        "uk", "us", "eu", "trading", "investors", "investor", "market", "markets",
        "network", "crypto", "cryptocurrency", "cryptocurrencies", "launch", "users",
        "user", "service", "app", "digital", "assets", "asset", "access", "more",
        "over", "than", "week", "year", "years", "firm", "company",
    }
)

# Lightweight event canonicalization (no synonym database).
EVENT_CANON: dict[str, str] = {
    "launch": "launch",
    "launched": "launch",
    "launches": "launch",
    "launching": "launch",
    "acquire": "acquire",
    "acquired": "acquire",
    "acquires": "acquire",
    "acquisition": "acquire",
    "approve": "approve",
    "approved": "approve",
    "approves": "approve",
    "approval": "approve",
    "reject": "reject",
    "rejected": "reject",
    "list": "list",
    "listed": "list",
    "listing": "list",
    "delist": "delist",
    "delisted": "delist",
    "announce": "announce",
    "announced": "announce",
    "partner": "partner",
    "partnered": "partner",
    "partnership": "partner",
    "integrate": "integrate",
    "integrated": "integrate",
    "raise": "raise",
    "raised": "raise",
    "invest": "invest",
    "invested": "invest",
    "investment": "invest",
}

HIGH_IMPORTANCE_PATTERNS = re.compile(
    r"\b("
    r"launch(?:ed|es|ing)?|acquisition|acquired|partnership|partner(?:ed|s)?|"
    r"regulatory|regulation|approval|approved|approves|ban(?:ned|s)?|"
    r"lawsuit|sued|hack(?:ed|s)?|exploit(?:ed|s)?|security\s+incident|"
    r"funding|investment|invested|revenue|profit|valuation|"
    r"list(?:ed|ing|s)?|delist(?:ed|ing|s)?|deadline|"
    r"price|percent(?:age)?|profit"
    r")\b|"
    r"[\$€£]\s*[\d,]+(?:\.\d+)?|\d+(?:\.\d+)?%|"
    r"\b(?:19|20)\d{2}[-/]\d{1,2}[-/]\d{1,2}\b|"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
    re.I,
)

OPPOSITE_PAIRS = (
    ("approved", "rejected"),
    ("approve", "reject"),
    ("listed", "delisted"),
    ("launched", "cancelled"),
    ("launched", "canceled"),
    ("accepted", "denied"),
    ("won", "lost"),
    ("passed", "failed"),
    ("increased", "decreased"),
    ("rose", "fell"),
    ("up", "down"),
)

STATUS_KEYS = (
    "DIRECT",
    "CORROBORATED",
    "SINGLE_SOURCE",
    "CONFLICTED",
    "UNSUPPORTED",
)


def domain_of(url: str) -> str:
    try:
        host = urlparse(url or "").netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def normalize_text(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t


def split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def extract_numbers(text: str) -> set[str]:
    """Distinctive numeric tokens normalized for comparison."""
    found: set[str] = set()
    for m in re.finditer(
        r"\$?\s*[\d,]+(?:\.\d+)?%?|\d{4}-\d{1,2}-\d{1,2}",
        text or "",
    ):
        raw = m.group(0)
        cleaned = re.sub(r"[^\d.%]", "", raw.replace(",", ""))
        if cleaned:
            found.add(cleaned)
    return found


def overlap_score(a: str, b: str) -> float:
    t1 = scan_headlines.get_tokens(a)
    t2 = scan_headlines.get_tokens(b)
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / min(len(t1), len(t2))


def is_high_importance(claim: str) -> bool:
    if not (claim or "").strip():
        return False
    if extract_numbers(claim):
        return True
    return bool(HIGH_IMPORTANCE_PATTERNS.search(claim))


def claim_directly_supported(claim: str, evidence: str) -> bool:
    """Deterministic DIRECT support tests A/B/C (strict path; threshold unchanged)."""
    if not claim or not evidence:
        return False
    c_norm = normalize_text(claim)
    e_norm = normalize_text(evidence)
    if len(c_norm) >= 20 and c_norm in e_norm:
        return True
    # Near-substring: claim without trailing punctuation
    c_trim = c_norm.rstrip(" .!?")
    if len(c_trim) >= 24 and c_trim in e_norm:
        return True

    claim_nums = extract_numbers(claim)
    for sent in split_sentences(evidence):
        if scan_headlines.is_near_duplicate(claim, sent):
            if not claim_nums or claim_nums.issubset(extract_numbers(sent)):
                return True
        score = overlap_score(claim, sent)
        if score >= TOKEN_SUPPORT_THRESHOLD:
            if not claim_nums or claim_nums.issubset(extract_numbers(sent)):
                return True
        # Also allow larger windows (2 sentences)
    # Sliding window over paragraphs
    for block in re.split(r"\n\s*\n", evidence):
        if scan_headlines.is_near_duplicate(claim, block):
            if not claim_nums or claim_nums.issubset(extract_numbers(block)):
                return True
        score = overlap_score(claim, block)
        if score >= TOKEN_SUPPORT_THRESHOLD and (
            not claim_nums or claim_nums.issubset(extract_numbers(block))
        ):
            return True
    return False


def meaningful_token_count(text: str) -> int:
    return len(scan_headlines.get_tokens(text or ""))


def is_long_claim(text: str) -> bool:
    return meaningful_token_count(text) >= LONG_CLAIM_MIN_TOKENS


def extract_typed_numbers(text: str) -> list[dict[str, Any]]:
    """Corroboration-only typed numbers (does not change extract_numbers)."""
    out: list[dict[str, Any]] = []
    t = text or ""

    for m in re.finditer(
        r"\$\s*([\d,]+(?:\.\d+)?)\s*(million|billion|thousand|m\b|bn\b|k\b)?",
        t,
        re.I,
    ):
        raw_val = m.group(1).replace(",", "")
        try:
            val = float(raw_val)
        except ValueError:
            continue
        scale_tok = (m.group(2) or "").lower()
        if scale_tok in ("million", "m"):
            scale = "MILLION"
        elif scale_tok in ("billion", "bn"):
            scale = "BILLION"
        elif scale_tok in ("thousand", "k"):
            scale = "THOUSAND"
        else:
            scale = "NONE"
        out.append({"kind": "MONEY", "value": val, "scale": scale, "raw": m.group(0)})

    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*%", t):
        try:
            val = float(m.group(1))
        except ValueError:
            continue
        out.append({"kind": "PERCENT", "value": val, "scale": "NONE", "raw": m.group(0)})

    for m in re.finditer(r"\b((?:19|20)\d{2})\b", t):
        out.append({"kind": "YEAR", "value": float(m.group(1)), "scale": "NONE", "raw": m.group(1)})

    for m in re.finditer(r"\b(\d{2,}(?:,\d{3})*)\+?\b", t):
        # Skip if already captured as year or part of money/% (rough: skip 19xx/20xx)
        raw = m.group(1).replace(",", "")
        if re.fullmatch(r"(?:19|20)\d{2}", raw):
            continue
        # Skip if this span sits inside a $...money match already — still ok as COUNT
        try:
            val = float(raw)
        except ValueError:
            continue
        if val < 10:
            continue
        out.append({"kind": "COUNT", "value": val, "scale": "NONE", "raw": m.group(0)})

    return out


def _number_is_strong(n: dict[str, Any]) -> bool:
    kind = n.get("kind")
    val = float(n.get("value") or 0)
    scale = n.get("scale") or "NONE"
    if kind == "PERCENT":
        return True
    if kind == "YEAR":
        return True
    if kind == "COUNT" and val >= 10:
        return True
    if kind == "MONEY":
        if scale in ("MILLION", "BILLION", "THOUSAND"):
            return True
        if val >= 100:
            return True
    return False


def numbers_compatible(claim_nums: list[dict[str, Any]], evidence_nums: list[dict[str, Any]]) -> bool:
    """True if every strong claim number has a compatible evidence number."""
    strong = [n for n in claim_nums if _number_is_strong(n)]
    if not strong:
        return True
    for cn in strong:
        matched = False
        for en in evidence_nums:
            if cn["kind"] != en["kind"]:
                continue
            if abs(float(cn["value"]) - float(en["value"])) > 1e-6:
                continue
            if cn["kind"] == "MONEY" and (cn.get("scale") or "NONE") != (en.get("scale") or "NONE"):
                continue
            matched = True
            break
        if not matched:
            return False
    return True


def _event_stems(text: str) -> set[str]:
    stems: set[str] = set()
    for tok in re.findall(r"[A-Za-z]+", (text or "").lower()):
        if tok in EVENT_CANON:
            stems.add(EVENT_CANON[tok])
    return stems


def extract_anchors(
    text: str,
    *,
    story_ctx: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Deterministic anchors for paraphrase corroboration (no NER)."""
    story_ctx = story_ctx or {}
    entities: set[str] = set()

    # Capitalized / ALLCAPS names (Bitstamp, Robinhood, SEC, XRP)
    for m in re.findall(r"\b[A-Z][A-Za-z0-9]{1,}\b", text or ""):
        low = m.lower()
        if low in WEAK_ENTITY_TOKENS or low in scan_headlines.STOP_WORDS:
            continue
        entities.add(low)

    # Tickers $XRP
    for m in re.findall(r"\$([A-Za-z]{2,10})\b", text or ""):
        entities.add(m.lower())

    asset = str(story_ctx.get("primary_asset") or "").strip()
    if asset and asset.lower() in (text or "").lower():
        entities.add(asset.lower())

    # Story headline/theme tokens that also appear in text (strong if not weak)
    blob = " ".join(
        str(story_ctx.get(k) or "")
        for k in ("primary_headline", "topic_theme", "primary_keyword", "primary_asset")
    )
    story_toks = scan_headlines.get_tokens(blob)
    text_toks = scan_headlines.get_tokens(text or "")
    for tok in story_toks & text_toks:
        if tok not in WEAK_ENTITY_TOKENS and len(tok) >= 3:
            entities.add(tok)

    typed = extract_typed_numbers(text)
    strong_nums = [n for n in typed if _number_is_strong(n)]
    events = _event_stems(text)

    return {
        "entities": entities,
        "events": events,
        "numbers": typed,
        "strong_numbers": strong_nums,
    }


def anchors_support_paraphrase(
    claim: str,
    window: str,
    *,
    story_ctx: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """LONG-claim only ANCHOR_PARAPHRASE gate."""
    if not is_long_claim(claim):
        return False, None
    if not window or not claim:
        return False, None

    ca = extract_anchors(claim, story_ctx=story_ctx)
    ea = extract_anchors(window, story_ctx=story_ctx)
    shared_entities = sorted(ca["entities"] & ea["entities"])
    shared_events = sorted(ca["events"] & ea["events"])
    soft = overlap_score(claim, window)

    if len(shared_entities) < 2:
        return False, None
    if not (shared_events or soft >= SOFT_PARAPHRASE_OVERLAP):
        return False, None
    if not numbers_compatible(ca["strong_numbers"], ea["numbers"]):
        return False, None

    # Reject matches driven only by one weak number / generics (entities already ≥2)
    matched_anchors = list(shared_entities)
    for n in ca["strong_numbers"]:
        for en in ea["numbers"]:
            if (
                n["kind"] == en["kind"]
                and abs(float(n["value"]) - float(en["value"])) <= 1e-6
                and (
                    n["kind"] != "MONEY"
                    or (n.get("scale") or "NONE") == (en.get("scale") or "NONE")
                )
            ):
                label = str(n.get("raw") or n["value"])
                if label not in matched_anchors:
                    matched_anchors.append(label)
                break
    matched_anchors.extend(shared_events)

    meta = {
        "support_type": "CORROBORATING",
        "match_rule": "ANCHOR_PARAPHRASE",
        "overlap_score": round(soft, 4),
        "matched_anchors": matched_anchors[:12],
    }
    return True, meta


def support_with_meta(
    claim: str,
    evidence: str,
    *,
    story_ctx: dict[str, str] | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """
    Strict path first (unchanged thresholds). LONG claims may use ANCHOR_PARAPHRASE.
    """
    if claim_directly_supported(claim, evidence):
        return True, {"support_type": "DIRECT", "match_rule": "STRICT"}

    if not is_long_claim(claim):
        return False, None

    for sent in split_sentences(evidence):
        ok, meta = anchors_support_paraphrase(claim, sent, story_ctx=story_ctx)
        if ok and meta:
            return True, meta
    for block in re.split(r"\n\s*\n", evidence or ""):
        ok, meta = anchors_support_paraphrase(claim, block, story_ctx=story_ctx)
        if ok and meta:
            return True, meta
    # Whole-document window for long articles
    ok, meta = anchors_support_paraphrase(claim, evidence, story_ctx=story_ctx)
    if ok and meta:
        return True, meta
    return False, None


def detect_conflict(claim: str, evidence: str) -> bool:
    """Conservative CONFLICTED: shared topical tokens + conflicting numbers or antonyms."""
    if not claim or not evidence:
        return False
    claim_tokens = scan_headlines.get_tokens(claim)
    if len(claim_tokens) < 2:
        return False

    claim_nums = extract_numbers(claim)
    for sent in split_sentences(evidence):
        if overlap_score(claim, sent) < 0.45:
            continue
        sent_nums = extract_numbers(sent)
        if claim_nums and sent_nums and claim_nums.isdisjoint(sent_nums):
            # Same topical window, different distinctive numbers → conflict
            return True
        low_c = claim.lower()
        low_s = sent.lower()
        for a, b in OPPOSITE_PAIRS:
            if (a in low_c and b in low_s) or (b in low_c and a in low_s):
                return True
    return False


def build_primary_query(claim: str) -> str:
    """Strategy B: stopword-stripped claim tokens; preserve numbers/$/%/tickers."""
    # Keep currency/percent spans as tokens
    preserved: list[str] = []
    for m in re.finditer(r"\$[\d,]+(?:\.\d+)?%?|\d+(?:\.\d+)?%|[A-Za-z][A-Za-z0-9%-]*", claim or ""):
        tok = m.group(0)
        low = tok.lower()
        if low in scan_headlines.STOP_WORDS:
            continue
        if len(tok) == 1 and not tok.isdigit():
            continue
        preserved.append(tok)
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in preserved:
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if len(out) >= QUERY_TOKEN_CAP:
            break
    return " ".join(out).strip()


def build_fallback_query(
    claim: str,
    headline: str,
    primary_asset: str,
) -> str:
    """Strategy C: headline keywords + asset + claim numbers/entities."""
    parts: list[str] = []
    for t in re.findall(r"[A-Za-z][A-Za-z0-9%-]*", headline or ""):
        if t.lower() in scan_headlines.STOP_WORDS:
            continue
        parts.append(t)
        if len(parts) >= 6:
            break
    if primary_asset and str(primary_asset).strip().lower() not in ("", "unknown"):
        parts.append(str(primary_asset).strip())
    for n in sorted(extract_numbers(claim)):
        parts.append(n)
    # a few claim entity tokens
    for t in re.findall(r"[A-Za-z][A-Za-z0-9%-]*", claim or ""):
        if t.lower() in scan_headlines.STOP_WORDS:
            continue
        if t.lower() not in {p.lower() for p in parts}:
            parts.append(t)
        if len(parts) >= QUERY_TOKEN_CAP:
            break
    return " ".join(parts).strip()


def load_source_corpus(sources_dir: str) -> list[dict[str, Any]]:
    corpus: list[dict[str, Any]] = []
    if not sources_dir or not os.path.isdir(sources_dir):
        return corpus
    for fn in sorted(os.listdir(sources_dir)):
        if not fn.endswith(".json"):
            continue
        path = os.path.join(sources_dir, fn)
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        if not rec.get("ok"):
            continue
        content = str(rec.get("content") or "").strip()
        if not content:
            continue
        url = str(rec.get("url") or "").strip()
        dom = domain_of(url) or str(rec.get("source") or "").strip().lower()
        corpus.append(
            {
                "url": url,
                "domain": dom,
                "content": content,
                "path": path,
                "record_name": fn,
            }
        )
    return corpus


def sha1_url(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()


def find_cached_record(*dirs: str, url: str) -> dict[str, Any] | None:
    resolved = resolve_url.resolve(url) or url
    digest = sha1_url(resolved)
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        path = os.path.join(d, f"{digest}.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("ok") and str(rec.get("content") or "").strip():
            return {
                "url": str(rec.get("url") or resolved),
                "domain": domain_of(str(rec.get("url") or resolved)),
                "content": str(rec["content"]),
                "path": path,
                "from_cache": True,
            }
    return None


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=d, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def empty_summary() -> dict[str, int]:
    return {
        "total": 0,
        "direct": 0,
        "corroborated": 0,
        "single_source": 0,
        "conflicted": 0,
        "unsupported": 0,
    }


def summarize(claims: list[dict]) -> dict[str, int]:
    s = empty_summary()
    s["total"] = len(claims)
    key_map = {
        "DIRECT": "direct",
        "CORROBORATED": "corroborated",
        "SINGLE_SOURCE": "single_source",
        "CONFLICTED": "conflicted",
        "UNSUPPORTED": "unsupported",
    }
    for c in claims:
        k = key_map.get(str(c.get("status") or ""), "")
        if k:
            s[k] += 1
    return s


def validation_ok(doc: dict, expected_ids: list[str]) -> bool:
    if not isinstance(doc, dict):
        return False
    if doc.get("verification_version") != 1:
        return False
    if doc.get("status") != "ok":
        return False
    claims = doc.get("claims")
    if not isinstance(claims, list):
        return False
    got = {str(c.get("claim_id") or "") for c in claims if isinstance(c, dict)}
    if set(expected_ids) != got:
        return False
    for c in claims:
        if not isinstance(c, dict):
            return False
        if c.get("status") not in STATUS_KEYS:
            return False
    summary = doc.get("summary")
    if not isinstance(summary, dict):
        return False
    expected_summary = summarize(claims)
    try:
        actual_summary = {
            "total": int(summary["total"]),
            "direct": int(summary["direct"]),
            "corroborated": int(summary["corroborated"]),
            "single_source": int(summary["single_source"]),
            "conflicted": int(summary["conflicted"]),
            "unsupported": int(summary["unsupported"]),
        }
    except (KeyError, TypeError, ValueError):
        return False
    if expected_summary != actual_summary:
        return False
    return True


def load_verification_if_valid(path: str, expected_ids: list[str]) -> dict | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if validation_ok(doc, expected_ids):
        return doc
    return None


class VerifyBudget:
    def __init__(self, max_fetches: int = MAX_FETCHES_PER_JOB) -> None:
        self.max_fetches = max_fetches
        self.fetches = 0
        self.read_calls = 0  # actual read_tool.read_url invocations

    @property
    def remaining(self) -> int:
        return max(0, self.max_fetches - self.fetches)


def filter_and_rank_candidates(
    results: list[dict],
    *,
    claim: str,
    origin_url: str,
    known_urls: set[str],
    origin_domain: str,
) -> list[dict]:
    origin_url_n = (origin_url or "").rstrip("/")
    known_norm = {u.rstrip("/") for u in known_urls}
    seen_domains: set[str] = set()
    scored: list[tuple[float, dict]] = []
    for r in results or []:
        url = str(r.get("url") or "").strip()
        if not url:
            continue
        if url.rstrip("/") == origin_url_n or url.rstrip("/") in known_norm:
            continue
        dom = str(r.get("domain") or domain_of(url)).lower()
        if not dom or dom == origin_domain or dom in seen_domains:
            continue
        blob = f"{r.get('title', '')} {r.get('snippet', '')}"
        score = overlap_score(claim, blob)
        if score < OVERLAP_THRESHOLD:
            continue
        seen_domains.add(dom)
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:MAX_CANDIDATES_CONSIDERED]]


def verify_claims(
    validated: dict[str, Any],
    sources_dir: str,
    *,
    verify_cache_dir: str | None = None,
    search_fn: Callable[..., list[dict]] | None = None,
    read_fn: Callable[..., dict] | None = None,
    enable_search: bool = True,
    qualification_source: str | None = None,
) -> dict[str, Any]:
    """
    Core verifier. search_fn/read_fn injectable for tests.
    """
    search_fn = search_fn or search_tool.search
    read_fn = read_fn or read_tool.read_url

    sourced = validated.get("sourced_facts") or []
    if not isinstance(sourced, list):
        sourced = []
    source_urls = [
        str(u).strip()
        for u in (validated.get("source_urls") or [])
        if isinstance(u, str) and u.strip()
    ]
    known_urls = set(source_urls)
    headline = str(
        validated.get("primary_headline") or validated.get("topic_theme") or ""
    )
    primary_asset = str(validated.get("primary_asset") or "")
    story_ctx = {
        "primary_headline": headline,
        "topic_theme": str(validated.get("topic_theme") or ""),
        "primary_keyword": str(validated.get("primary_keyword") or ""),
        "primary_asset": primary_asset,
    }

    corpus = load_source_corpus(sources_dir)
    by_domain: dict[str, list[dict]] = {}
    by_url: dict[str, dict] = {}
    for s in corpus:
        by_domain.setdefault(s["domain"], []).append(s)
        if s["url"]:
            by_url[s["url"].rstrip("/")] = s

    budget = VerifyBudget()
    claims_out: list[dict[str, Any]] = []

    for fact in sourced:
        if not isinstance(fact, dict):
            continue
        claim_id = str(fact.get("id") or f"fact_{len(claims_out) + 1:03d}")
        claim = str(fact.get("text") or "").strip()
        origin_url = str(fact.get("source_url") or "").strip()
        origin_domain = str(fact.get("source_domain") or domain_of(origin_url)).lower()

        evidence_entries: list[dict[str, str]] = []
        supporting_domains: list[str] = []

        # Resolve origin content from corpus
        origin_content = ""
        origin_rec = by_url.get(origin_url.rstrip("/"))
        if origin_rec:
            origin_content = origin_rec["content"]
        elif origin_domain in by_domain:
            origin_content = by_domain[origin_domain][0]["content"]
        else:
            # try source_record path relative to sources parent
            rec_path = str(fact.get("source_record") or "").strip()
            if rec_path:
                # research/sources/<fn> → join with dirname(sources_dir)
                base = os.path.dirname(os.path.abspath(sources_dir))
                # sources_dir is .../research/sources; record is research/sources/x.json
                cand = os.path.join(os.path.dirname(base), rec_path.replace("/", os.sep))
                if not os.path.isfile(cand):
                    cand = os.path.join(sources_dir, os.path.basename(rec_path))
                if os.path.isfile(cand):
                    try:
                        with open(cand, encoding="utf-8") as f:
                            rec = json.load(f)
                        if rec.get("ok") and rec.get("content"):
                            origin_content = str(rec["content"])
                    except (OSError, json.JSONDecodeError):
                        pass

        origin_supports = claim_directly_supported(claim, origin_content)
        if origin_supports and origin_domain:
            supporting_domains.append(origin_domain)
            if origin_url:
                evidence_entries.append(
                    {
                        "source_url": origin_url,
                        "source_domain": origin_domain,
                        "support_type": "DIRECT",
                    }
                )

        if not origin_supports:
            claims_out.append(
                {
                    "claim_id": claim_id,
                    "claim": claim,
                    "origin_url": origin_url,
                    "origin_domain": origin_domain,
                    "status": "UNSUPPORTED",
                    "supporting_domains": [],
                    "evidence": [],
                }
            )
            continue

        # Offline corroboration / conflict across OTHER domains
        offline_corroborated = False
        conflicted = False
        for s in corpus:
            if not s["domain"] or s["domain"] == origin_domain:
                continue
            if s["url"] and s["url"].rstrip("/") == origin_url.rstrip("/"):
                continue
            if detect_conflict(claim, s["content"]):
                conflicted = True
                evidence_entries.append(
                    {
                        "source_url": s["url"],
                        "source_domain": s["domain"],
                        "support_type": "CONFLICT",
                    }
                )
                break
            ok, meta = support_with_meta(claim, s["content"], story_ctx=story_ctx)
            if ok:
                offline_corroborated = True
                if s["domain"] not in supporting_domains:
                    supporting_domains.append(s["domain"])
                entry: dict[str, Any] = {
                    "source_url": s["url"],
                    "source_domain": s["domain"],
                    "support_type": (meta or {}).get("support_type") or "DIRECT",
                }
                if meta and meta.get("match_rule") == "ANCHOR_PARAPHRASE":
                    entry["match_rule"] = meta["match_rule"]
                    entry["overlap_score"] = meta.get("overlap_score")
                    entry["matched_anchors"] = meta.get("matched_anchors") or []
                evidence_entries.append(entry)
                break

        if conflicted:
            claims_out.append(
                {
                    "claim_id": claim_id,
                    "claim": claim,
                    "origin_url": origin_url,
                    "origin_domain": origin_domain,
                    "status": "CONFLICTED",
                    "supporting_domains": supporting_domains,
                    "evidence": evidence_entries,
                }
            )
            continue

        if offline_corroborated:
            claims_out.append(
                {
                    "claim_id": claim_id,
                    "claim": claim,
                    "origin_url": origin_url,
                    "origin_domain": origin_domain,
                    "status": "CORROBORATED",
                    "supporting_domains": supporting_domains,
                    "evidence": evidence_entries,
                }
            )
            continue

        # Offline-only → DIRECT until/unless high-importance search runs
        searched = False
        if enable_search and is_high_importance(claim) and budget.remaining > 0:
            searched = True
            q = build_primary_query(claim) or claim[:120]
            results = search_fn(q, max_results=MAX_SEARCH_RESULTS) or []
            useful_domains = {
                str(r.get("domain") or domain_of(str(r.get("url") or "")))
                for r in results
                if r.get("url")
            }
            useful_domains.discard(origin_domain)
            useful_domains.discard("")
            if len(useful_domains) < 2:
                q2 = build_fallback_query(claim, headline, primary_asset)
                if q2 and q2 != q:
                    extra = search_fn(q2, max_results=MAX_SEARCH_RESULTS) or []
                    # merge unique domains
                    seen_u = {str(r.get("url") or "").rstrip("/") for r in results}
                    for r in extra:
                        u = str(r.get("url") or "").rstrip("/")
                        if u and u not in seen_u:
                            results.append(r)
                            seen_u.add(u)

            candidates = filter_and_rank_candidates(
                results,
                claim=claim,
                origin_url=origin_url,
                known_urls=known_urls,
                origin_domain=origin_domain,
            )

            fetches_this_claim = 0
            for cand in candidates:
                if fetches_this_claim >= MAX_FETCHES_PER_CLAIM or budget.remaining <= 0:
                    break
                url = str(cand.get("url") or "").strip()
                dom = str(cand.get("domain") or domain_of(url)).lower()
                if not url or not dom or dom == origin_domain:
                    continue

                cached = find_cached_record(sources_dir, verify_cache_dir or "", url=url)
                if cached:
                    content = cached["content"]
                    url = cached["url"]
                    dom = cached["domain"] or dom
                else:
                    out_dir = verify_cache_dir or sources_dir
                    os.makedirs(out_dir, exist_ok=True)
                    rec = read_fn(url, out_dir, dom)
                    budget.read_calls += 1
                    budget.fetches += 1
                    fetches_this_claim += 1
                    if not rec.get("ok") or not str(rec.get("content") or "").strip():
                        continue
                    content = str(rec["content"])
                    url = str(rec.get("url") or url)
                    dom = domain_of(url) or dom

                if detect_conflict(claim, content):
                    conflicted = True
                    evidence_entries.append(
                        {
                            "source_url": url,
                            "source_domain": dom,
                            "support_type": "CONFLICT",
                        }
                    )
                    break
                ok, meta = support_with_meta(claim, content, story_ctx=story_ctx)
                if ok:
                    if dom not in supporting_domains:
                        supporting_domains.append(dom)
                    entry = {
                        "source_url": url,
                        "source_domain": dom,
                        "support_type": (meta or {}).get("support_type") or "DIRECT",
                    }
                    if meta and meta.get("match_rule") == "ANCHOR_PARAPHRASE":
                        entry["match_rule"] = meta["match_rule"]
                        entry["overlap_score"] = meta.get("overlap_score")
                        entry["matched_anchors"] = meta.get("matched_anchors") or []
                    evidence_entries.append(entry)
                    offline_corroborated = True
                    break

        if conflicted:
            status = "CONFLICTED"
        elif offline_corroborated or len(supporting_domains) >= 2:
            status = "CORROBORATED"
        elif searched:
            status = "SINGLE_SOURCE"
        else:
            status = "DIRECT"

        claims_out.append(
            {
                "claim_id": claim_id,
                "claim": claim,
                "origin_url": origin_url,
                "origin_domain": origin_domain,
                "status": status,
                "supporting_domains": supporting_domains,
                "evidence": evidence_entries,
            }
        )

    doc = {
        "verification_version": 1,
        "status": "ok",
        "mode": "offline_first",
        "claims": claims_out,
        "summary": summarize(claims_out),
    }
    if qualification_source:
        doc["qualification_source"] = qualification_source
    return doc


def _filter_validated_to_verify(
    validated: dict[str, Any],
    qualified: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Return validated copy with only VERIFY sourced_facts + expected verify ids."""
    sourced = validated.get("sourced_facts") or []
    if not isinstance(sourced, list):
        sourced = []
    if not qualified:
        ids = [
            str(f.get("id") or "")
            for f in sourced
            if isinstance(f, dict) and str(f.get("id") or "")
        ]
        return validated, ids

    verify_ids = {
        str(c.get("claim_id") or "")
        for c in (qualified.get("claims") or [])
        if isinstance(c, dict) and c.get("decision") == "VERIFY" and c.get("claim_id")
    }
    filtered = [f for f in sourced if isinstance(f, dict) and str(f.get("id") or "") in verify_ids]
    out = dict(validated)
    out["sourced_facts"] = filtered
    return out, sorted(verify_ids)


def run_verify(
    validated_path: str,
    sources_dir: str,
    output_path: str,
    *,
    verify_cache_dir: str | None = None,
    qualified_path: str | None = None,
) -> tuple[int, dict]:
    try:
        with open(validated_path, encoding="utf-8") as f:
            validated = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        err = {"verification_version": 1, "status": "error", "reason": f"read_validated: {e}"}
        atomic_write_json(output_path, err)
        print(f"VERIFY_ERROR: {e}", file=sys.stderr)
        return 1, err

    qualified: dict[str, Any] | None = None
    qual_rel = None
    if qualified_path:
        try:
            with open(qualified_path, encoding="utf-8") as f:
                qualified = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            err = {
                "verification_version": 1,
                "status": "error",
                "reason": f"read_qualified: {e}",
            }
            atomic_write_json(output_path, err)
            print(f"VERIFY_ERROR: {e}", file=sys.stderr)
            return 1, err
        if not isinstance(qualified, dict) or qualified.get("status") != "ok":
            err = {
                "verification_version": 1,
                "status": "error",
                "reason": "qualified_claims_invalid",
            }
            atomic_write_json(output_path, err)
            print("VERIFY_ERROR: qualified_claims_invalid", file=sys.stderr)
            return 1, err
        qual_rel = "research/qualified_claims.json"

    validated_for_verify, expected_ids = _filter_validated_to_verify(validated, qualified)

    existing = load_verification_if_valid(output_path, expected_ids)
    if existing is not None:
        print("VERIFY_RESUME: verification.json already valid", file=sys.stderr)
        return 0, existing

    if verify_cache_dir is None:
        verify_cache_dir = os.path.join(os.path.dirname(os.path.abspath(sources_dir)), "verify_sources")

    doc = verify_claims(
        validated_for_verify,
        sources_dir,
        verify_cache_dir=verify_cache_dir,
        enable_search=True,
        qualification_source=qual_rel,
    )
    out_ids = [str(c.get("claim_id") or "") for c in doc.get("claims") or []]
    if sorted(expected_ids) != sorted(out_ids):
        doc["status"] = "error"
        doc["reason"] = "claim_count_mismatch"
        atomic_write_json(output_path, doc)
        print("VERIFY_ERROR: claim_count_mismatch", file=sys.stderr)
        return 1, doc

    if not validation_ok(doc, out_ids):
        doc["summary"] = summarize(doc.get("claims") or [])
        if doc.get("status") != "ok" or not validation_ok(doc, out_ids):
            atomic_write_json(output_path, doc)
            print("VERIFY_ERROR: invalid artifact", file=sys.stderr)
            return 1, doc

    atomic_write_json(output_path, doc)
    s = doc["summary"]
    print(
        f"VERIFY_OK: total={s['total']} corroborated={s['corroborated']} "
        f"direct={s['direct']} single={s['single_source']} "
        f"conflicted={s['conflicted']} unsupported={s['unsupported']}",
        file=sys.stderr,
    )
    return 0, doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Claim Verification V1")
    ap.add_argument("--validated", required=True, help="path to validated.json")
    ap.add_argument("--sources-dir", required=True, help="path to research/sources")
    ap.add_argument("--output", required=True, help="path to verification.json")
    ap.add_argument(
        "--qualified",
        default=None,
        help="path to qualified_claims.json (VERIFY-only filter)",
    )
    ap.add_argument(
        "--verify-cache-dir",
        default=None,
        help="optional cache dir for new reads (default: research/verify_sources)",
    )
    args = ap.parse_args()
    code, _ = run_verify(
        os.path.realpath(args.validated),
        os.path.realpath(args.sources_dir),
        os.path.realpath(args.output),
        verify_cache_dir=os.path.realpath(args.verify_cache_dir)
        if args.verify_cache_dir
        else None,
        qualified_path=os.path.realpath(args.qualified) if args.qualified else None,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
