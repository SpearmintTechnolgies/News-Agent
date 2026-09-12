#!/usr/bin/env python3
"""
qualify_claims.py — Phase 1.5 claim qualification (deterministic).

Reads research/validated.json sourced_facts and writes research/qualified_claims.json
with VERIFY | SKIP decisions. Does NOT mutate sourced_facts or combined_key_facts.

Usage:
  python3 qualify_claims.py \\
    --validated /run/research/validated.json \\
    --output /run/research/qualified_claims.json
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_HEADLINE_SCAN = os.path.join(os.path.dirname(_HERE), "headline-scan")
if _HEADLINE_SCAN not in sys.path:
    sys.path.insert(0, _HEADLINE_SCAN)

import scan_headlines  # noqa: E402

APP_STORE_DOMAINS = frozenset({"play.google.com", "apps.apple.com"})

# Phrase-level CTAs (word-boundary aware via \b around the whole phrase where needed).
CTA_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\bget started\b",
        r"\bsign up(?:\s+now)?\b",
        r"\bdownload(?:\s+now)?\b",
        r"\bjoin now\b",
        r"\blearn more\b",
        r"\bstart investing\b",
        r"\bstart trading\b",
        r"\bopen an account\b",
        r"\bget the app\b",
        r"\btry now\b",
        r"\bbuy now\b",
        r"\bas little as\s*\$",
        r"\bclaim your bonus\b",
        r"\bdownload the app\b",
    )
]

HOMEPAGE_PROMO_PATTERNS = [
    re.compile(p, re.I)
    for p in (
        r"\bget started\b",
        r"\bas little as\b",
        r"\bzero management fees\b",
        r"\bgold members?\b",
        r"\bcommission-?free\b.*\binvest",
        r"\bstart (?:investing|trading) (?:today|now)\b",
        r"\bjoin millions\b",
        r"\byour money'?s? potential\b",
    )
]

EVENT_VERBS = frozenset(
    {
        "launch", "launched", "launches", "launching",
        "acquire", "acquired", "acquires", "acquisition",
        "approve", "approved", "approves", "approval",
        "ban", "banned", "reject", "rejected",
        "list", "listed", "listing", "delist", "delisted",
        "hack", "hacked", "exploit", "exploited",
        "sue", "sued", "lawsuit",
        "raise", "raised", "fund", "funded",
        "fall", "fell", "rise", "rose", "drop", "dropped",
        "announce", "announced", "close", "closed", "closing",
        "charge", "charges", "charged", "apply", "applied",
        "generate", "generated", "exceed", "exceeding",
        "partner", "partnered", "partnership",
    }
)

REASON_MATERIAL = "MATERIAL_CLAIM"
REASON_CTA = "MARKETING_CTA"
REASON_APP = "APP_STORE_CONTENT"
REASON_HOME = "HOMEPAGE_BOILERPLATE"
REASON_NON = "NON_MATERIAL"
REASON_INSUFF = "INSUFFICIENT_CLAIM_SIGNAL"


def domain_of(url: str) -> str:
    try:
        host = urlparse(url or "").netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def is_apex_homepage(url: str) -> bool:
    """True for https://example.com or https://example.com/ (no meaningful path)."""
    try:
        p = urlparse(url or "")
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.netloc:
        return False
    path = (p.path or "").strip()
    return path in ("", "/")


def meaningful_tokens(text: str) -> set[str]:
    return scan_headlines.get_tokens(text or "")


def has_cta_language(text: str) -> bool:
    t = text or ""
    return any(p.search(t) for p in CTA_PATTERNS)


def has_homepage_promo(text: str) -> bool:
    t = text or ""
    return any(p.search(t) for p in HOMEPAGE_PROMO_PATTERNS)


def has_event_verb(text: str) -> bool:
    toks = meaningful_tokens(text)
    # also check raw lower tokens including stemmed-ish forms already in set
    low = re.findall(r"[a-zA-Z]+", (text or "").lower())
    return bool(toks & EVENT_VERBS) or bool(set(low) & EVENT_VERBS)


def has_named_signal(text: str, primary_asset: str) -> bool:
    """Organization / asset / ticker-ish signal without ML NER."""
    asset = (primary_asset or "").strip()
    if asset and asset.lower() not in ("unknown",) and asset.lower() in (text or "").lower():
        return True
    # Capitalized multi-char tokens (Robinhood, Bitstamp, SEC, ETF, XRP)
    caps = re.findall(r"\b[A-Z][A-Za-z0-9]{1,}\b", text or "")
    # Ignore sentence-start common words when alone is hard; keep if 2+ caps or known short ALLCAPS
    allcaps = [c for c in caps if c.isupper() and len(c) >= 2]
    titleish = [c for c in caps if not c.isupper() and len(c) >= 3]
    if allcaps or len(titleish) >= 1:
        return True
    # Ticker-like $BTC
    if re.search(r"\$[A-Za-z]{2,10}\b", text or ""):
        return True
    return False


def has_meaningful_numeric_context(text: str) -> bool:
    """Prefer news-like figures over bare CTA $1."""
    t = text or ""
    if re.search(r"\d+(?:\.\d+)?%", t):
        return True
    if re.search(
        r"\$\s*[\d,]+(?:\.\d+)?\s*(?:million|billion|thousand|m\b|bn\b|k\b)",
        t,
        re.I,
    ):
        return True
    if re.search(r"\b(?:19|20)\d{2}\b", t):
        return True
    if re.search(r"\$\s*[\d,]{3,}(?:\.\d+)?", t):  # $100+ scale amounts
        return True
    # multi-digit counts e.g. 50 assets, 10x — not lone $1
    if re.search(r"\b\d{2,}\b", t):
        return True
    return False


def story_relevance_score(
    text: str,
    headline: str,
    theme: str,
    keyword: str,
    asset: str,
) -> float:
    claim_toks = meaningful_tokens(text)
    if not claim_toks:
        return 0.0
    blob = " ".join(x for x in (headline, theme, keyword, asset) if x)
    story_toks = meaningful_tokens(blob)
    if not story_toks:
        return 0.0
    return len(claim_toks & story_toks) / min(len(claim_toks), len(story_toks))


def qualify_one(
    fact: dict[str, Any],
    *,
    headline: str,
    theme: str,
    keyword: str,
    asset: str,
) -> dict[str, str]:
    claim_id = str(fact.get("id") or "").strip()
    claim = str(fact.get("text") or "").strip()
    source_url = str(fact.get("source_url") or "").strip()
    source_domain = str(fact.get("source_domain") or domain_of(source_url)).lower()

    base = {
        "claim_id": claim_id,
        "claim": claim,
        "source_url": source_url,
        "source_domain": source_domain,
    }

    if not claim:
        return {**base, "decision": "SKIP", "reason_code": REASON_INSUFF}

    # RULE 1 — app store
    if source_domain in APP_STORE_DOMAINS or any(
        source_domain.endswith("." + d) for d in APP_STORE_DOMAINS
    ):
        return {**base, "decision": "SKIP", "reason_code": REASON_APP}

    # RULE 2 — obvious CTA / marketing (phrase-level)
    if has_cta_language(claim):
        # Allow override only for strong news-like sentences that merely mention a CTA phrase
        # inside a longer reportorial claim (rare). Require high story relevance + event verb
        # + meaningful numeric AND length.
        toks = meaningful_tokens(claim)
        if not (
            len(toks) >= 10
            and has_event_verb(claim)
            and has_meaningful_numeric_context(claim)
            and story_relevance_score(claim, headline, theme, keyword, asset) >= 0.35
        ):
            return {**base, "decision": "SKIP", "reason_code": REASON_CTA}

    # RULE 3 — homepage apex + promo/boilerplate
    if is_apex_homepage(source_url):
        if has_homepage_promo(claim) or has_cta_language(claim):
            return {**base, "decision": "SKIP", "reason_code": REASON_HOME}
        # Generic short homepage product fluff without news anchors
        toks = meaningful_tokens(claim)
        if len(toks) < 8 and not has_event_verb(claim) and not has_meaningful_numeric_context(claim):
            return {**base, "decision": "SKIP", "reason_code": REASON_HOME}
        if len(toks) < 8 and not has_named_signal(claim, asset):
            return {**base, "decision": "SKIP", "reason_code": REASON_HOME}

    # RULE 4 — short claims: multi-signal, not char-count alone
    toks = meaningful_tokens(claim)
    if len(toks) <= 6:
        newsy = (
            has_event_verb(claim)
            and has_named_signal(claim, asset)
            and (
                has_meaningful_numeric_context(claim)
                or story_relevance_score(claim, headline, theme, keyword, asset) >= 0.4
            )
        )
        if not newsy:
            if has_cta_language(claim) or has_homepage_promo(claim):
                return {**base, "decision": "SKIP", "reason_code": REASON_CTA}
            return {**base, "decision": "SKIP", "reason_code": REASON_INSUFF}

    # RULE 5/6 — pricing/fees: VERIFY when concrete + story-relevant; else skip fluff
    rel = story_relevance_score(claim, headline, theme, keyword, asset)
    if has_meaningful_numeric_context(claim) and (
        has_event_verb(claim) or has_named_signal(claim, asset) or rel >= 0.25
    ):
        return {**base, "decision": "VERIFY", "reason_code": REASON_MATERIAL}

    if has_event_verb(claim) and has_named_signal(claim, asset):
        return {**base, "decision": "VERIFY", "reason_code": REASON_MATERIAL}

    if rel >= 0.45 and len(toks) >= 8:
        return {**base, "decision": "VERIFY", "reason_code": REASON_MATERIAL}

    # Remaining weak / promotional company copy
    if has_homepage_promo(claim) or (
        is_apex_homepage(source_url) and rel < 0.2 and not has_event_verb(claim)
    ):
        return {**base, "decision": "SKIP", "reason_code": REASON_NON}

    if len(toks) < 5:
        return {**base, "decision": "SKIP", "reason_code": REASON_INSUFF}

    # Default: verify longer factual-looking claims
    if len(toks) >= 8 and (has_named_signal(claim, asset) or rel >= 0.2):
        return {**base, "decision": "VERIFY", "reason_code": REASON_MATERIAL}

    return {**base, "decision": "SKIP", "reason_code": REASON_NON}


def summarize(claims: list[dict]) -> dict[str, int]:
    verify = sum(1 for c in claims if c.get("decision") == "VERIFY")
    skip = sum(1 for c in claims if c.get("decision") == "SKIP")
    return {"total": len(claims), "verify": verify, "skip": skip}


def qualification_ok(doc: dict, expected_ids: list[str]) -> bool:
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("qualification_version") != 1:
        return False
    claims = doc.get("claims")
    if not isinstance(claims, list):
        return False
    got = [str(c.get("claim_id") or "") for c in claims if isinstance(c, dict)]
    if expected_ids and sorted(got) != sorted(expected_ids):
        return False
    for c in claims:
        if not isinstance(c, dict):
            return False
        if c.get("decision") not in ("VERIFY", "SKIP"):
            return False
        if not c.get("reason_code"):
            return False
    summary = doc.get("summary")
    if not isinstance(summary, dict):
        return False
    try:
        if int(summary["total"]) != len(claims):
            return False
        if int(summary["verify"]) != sum(1 for c in claims if c.get("decision") == "VERIFY"):
            return False
        if int(summary["skip"]) != sum(1 for c in claims if c.get("decision") == "SKIP"):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def qualify_claims(validated: dict[str, Any]) -> dict[str, Any]:
    sourced = validated.get("sourced_facts") or []
    if not isinstance(sourced, list):
        sourced = []
    headline = str(validated.get("primary_headline") or "")
    theme = str(validated.get("topic_theme") or "")
    keyword = str(validated.get("primary_keyword") or "")
    asset = str(validated.get("primary_asset") or "")
    if asset.strip().lower() == "unknown":
        asset = ""

    claims: list[dict[str, str]] = []
    for fact in sourced:
        if not isinstance(fact, dict):
            continue
        claims.append(
            qualify_one(
                fact,
                headline=headline,
                theme=theme,
                keyword=keyword,
                asset=asset,
            )
        )

    return {
        "qualification_version": 1,
        "status": "ok",
        "claims": claims,
        "summary": summarize(claims),
    }


def atomic_write_json(path: str, data: dict) -> None:
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=d, delete=False, suffix=".tmp", encoding="utf-8"
    ) as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def run_qualify(validated_path: str, output_path: str) -> tuple[int, dict]:
    try:
        with open(validated_path, encoding="utf-8") as f:
            validated = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        err = {
            "qualification_version": 1,
            "status": "error",
            "reason": f"read_validated: {e}",
        }
        atomic_write_json(output_path, err)
        print(f"QUALIFY_ERROR: {e}", file=sys.stderr)
        return 1, err

    sourced = validated.get("sourced_facts") or []
    expected_ids = [
        str(f.get("id") or "")
        for f in sourced
        if isinstance(f, dict) and str(f.get("id") or "")
    ]

    if os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                existing = json.load(f)
            if qualification_ok(existing, expected_ids):
                print("QUALIFY_RESUME: qualified_claims.json already valid", file=sys.stderr)
                return 0, existing
        except (OSError, json.JSONDecodeError):
            pass

    doc = qualify_claims(validated)
    if not qualification_ok(doc, [c["claim_id"] for c in doc["claims"]]):
        doc["status"] = "error"
        doc["reason"] = "invalid_qualification"
        atomic_write_json(output_path, doc)
        print("QUALIFY_ERROR: invalid_qualification", file=sys.stderr)
        return 1, doc

    atomic_write_json(output_path, doc)
    s = doc["summary"]
    print(
        f"QUALIFY_OK: total={s['total']} verify={s['verify']} skip={s['skip']}",
        file=sys.stderr,
    )
    return 0, doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Claim Qualification Phase 1.5")
    ap.add_argument("--validated", required=True)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    code, _ = run_qualify(os.path.realpath(args.validated), os.path.realpath(args.output))
    return code


if __name__ == "__main__":
    sys.exit(main())
