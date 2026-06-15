#!/usr/bin/env python3
"""
validate_headlines.py — Parse the researcher's HEADLINE_SCAN output, validate
it, and atomically rewrite a clean version in place.

Usage:
    python3 validate_headlines.py --path /path/to/headlines.json [--min 3]

Exit 0 + prints HEADLINES_VALID: <count> candidates
Exit 1 + prints HEADLINES_INVALID: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any

# ── shared research checks (single source of truth with Scout's self-check) ──
_RESEARCH_CHECK_DIR = os.path.expanduser(
    "~/.openclaw/workspace-researcher/skills/research-check"
)
if _RESEARCH_CHECK_DIR not in sys.path:
    sys.path.insert(0, _RESEARCH_CHECK_DIR)

import check_research as cr  # noqa: E402

REQUIRED_PER_CANDIDATE = cr.HEADLINE_REQUIRED_PER_CANDIDATE


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        tmp = f.name
    os.replace(tmp, path)


def _try_parse_pubdate(value: Any) -> str | None:
    """Best-effort normalize pub_date to ISO 8601 UTC. Return None if unparseable."""
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    fmts = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S%z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S GMT",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s.replace("GMT", "+0000"), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, help="Path to headlines.json")
    parser.add_argument("--min", type=int, default=2, help="Minimum candidate count (default 2)")
    args = parser.parse_args()

    path = os.path.realpath(args.path)
    if not os.path.exists(path):
        print(f"HEADLINES_INVALID: file not found: {path}")
        return 1

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as e:
        print(f"HEADLINES_INVALID: cannot read file: {e}")
        return 1

    if not text.strip():
        print("HEADLINES_INVALID: file is empty")
        return 1

    # Shared garbage guards — catch trajectory-log / raw-HTML dumps.
    if cr.looks_like_log(text) or cr.looks_like_html(text):
        print("HEADLINES_INVALID: output is a log/HTML dump, not headline JSON")
        return 1

    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        print("HEADLINES_INVALID: no JSON object found")
        return 1

    try:
        data = json.loads(text[start:end])
    except json.JSONDecodeError as e:
        print(f"HEADLINES_INVALID: JSON parse error: {e}")
        return 1

    if data.get("status") != "ok":
        reason = data.get("reason") or data.get("error") or "status != ok"
        print(f"HEADLINES_INVALID: {reason}")
        return 1

    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        print("HEADLINES_INVALID: candidates must be a non-empty list")
        return 1

    cleaned: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for idx, c in enumerate(raw_candidates, 1):
        if not isinstance(c, dict):
            continue
        missing = [k for k in REQUIRED_PER_CANDIDATE if not c.get(k)]
        if missing:
            continue
        url = str(c.get("url") or "").strip()
        if not url or url.startswith(("mailto:", "javascript:")):
            continue
        # Unresolved aggregator wrappers should have been resolved at scan time.
        if cr.is_aggregator_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        pub = _try_parse_pubdate(c.get("pub_date"))
        corroborating = c.get("corroborating_sources") or []
        if not isinstance(corroborating, list):
            corroborating = []
        cleaned.append({
            "candidate_index": len(cleaned) + 1,
            "headline": str(c.get("headline") or "").strip(),
            "url": url,
            "pub_date": pub,
            "source": str(c.get("source") or "").strip() or "Unknown",
            "summary": (str(c.get("summary") or "").strip())[:280],
            "corroborating_sources": [
                {
                    "source": str(cs.get("source") or "").strip(),
                    "url": str(cs.get("url") or "").strip(),
                }
                for cs in corroborating
                if isinstance(cs, dict) and cs.get("url")
            ],
        })

    if len(cleaned) < args.min:
        print(
            f"HEADLINES_INVALID: only {len(cleaned)} usable candidates (min {args.min})"
        )
        return 1

    out = {
        "status": "ok",
        "mode": "headline_scan",
        "scanned_at": data.get("scanned_at")
        or datetime.now(timezone.utc).isoformat(),
        "target_count": int(data.get("target_count") or 10),
        "candidate_count": len(cleaned),
        "candidates": cleaned,
    }

    try:
        atomic_write_json(path, out)
    except OSError as e:
        print(f"HEADLINES_INVALID: cannot write cleaned file: {e}")
        return 1

    print(f"HEADLINES_VALID: {len(cleaned)} candidates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
