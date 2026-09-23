"""Deterministic evidence-dimension coverage for bounded research expansion."""
from __future__ import annotations

import re
from typing import Any

# Coverage dimensions evaluated before Top-5 selection / research stop.
EVIDENCE_DIMENSIONS: tuple[str, ...] = (
    "core_event",
    "quantities",
    "chronology",
    "direct_statements",
    "background",
    "implications",
    "next_steps",
)

# Per-event research budgets (sensible caps; stop early when coverage satisfied).
MAX_EXPANSION_ROUNDS = 4
MAX_CANDIDATES_PER_ROUND = 16
MAX_TOTAL_EVIDENCE_ROWS = 28
MAX_SEARCH_HITS = 24
MIN_RELEVANCE_SCORE = 0.28

_QUANTITY_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|million|billion|bn|m\b|btc|eth|usd|\$)|"
    r"\$\s?\d",
    re.I,
)
_CHRONOLOGY_RE = re.compile(
    r"\b(?:19|20)\d{2}\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|yesterday|"
    r"overnight|this week|last week|timeline|deadline|effective)\b",
    re.I,
)
_STATEMENT_RE = re.compile(
    r"\b(?:said|says|stated|announced|confirmed|according to|commented|told|"
    r"statement|spokesperson|press release)\b",
    re.I,
)
_BACKGROUND_RE = re.compile(
    r"\b(?:previously|earlier|background|context|history|since|following|"
    r"comes after|amid|in the wake)\b",
    re.I,
)
_IMPLICATION_RE = re.compile(
    r"\b(?:impact|implication|means|could|may|market|investors|risk|effect|"
    r"consequence|outlook|sentiment)\b",
    re.I,
)
_NEXT_RE = re.compile(
    r"\b(?:next|will|expected|plans?|upcoming|hearing|deadline|follow[- ]up|"
    r"to be announced|scheduled|implementation)\b",
    re.I,
)

_DIMENSION_QUERY_HINTS: dict[str, list[str]] = {
    "core_event": ["what happened", "announcement"],
    "quantities": ["figures", "amount", "percent", "statistics"],
    "chronology": ["timeline", "date", "when"],
    "direct_statements": ["official statement", "said", "press release"],
    "background": ["background", "context", "previously"],
    "implications": ["impact", "market reaction", "implications"],
    "next_steps": ["next steps", "what next", "upcoming"],
}


def _blob(rows: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        parts.append(
            " ".join(
                str(row.get(k) or "")
                for k in ("title", "summary", "headline", "description", "extracted_text")
            )
        )
    return "\n".join(parts)


def evaluate_evidence_dimensions(
    evidence_rows: list[dict[str, Any]],
    *,
    event_title: str = "",
) -> dict[str, Any]:
    """Return per-dimension coverage booleans + missing list. Deterministic / no LLM."""
    rows = [r for r in evidence_rows if isinstance(r, dict)]
    text = _blob(rows)
    title = str(event_title or "").strip()
    title_tokens = {t.lower() for t in re.findall(r"[A-Za-z]{3,}", title)}
    core = False
    if title_tokens:
        for row in rows:
            blob = f"{row.get('title') or ''} {row.get('summary') or ''}".lower()
            hits = sum(1 for t in title_tokens if t in blob)
            if hits >= max(2, min(4, len(title_tokens) // 2)):
                core = True
                break
    else:
        core = len(rows) >= 1

    coverage = {
        "core_event": core and len(rows) >= 1,
        "quantities": bool(_QUANTITY_RE.search(text)),
        "chronology": bool(_CHRONOLOGY_RE.search(text)),
        "direct_statements": bool(_STATEMENT_RE.search(text)),
        "background": bool(_BACKGROUND_RE.search(text)),
        "implications": bool(_IMPLICATION_RE.search(text)),
        "next_steps": bool(_NEXT_RE.search(text)),
    }
    # Soft-pass: with 3+ independent domains, treat sparse soft dims as covered if core+qty or statements present.
    domains = {
        str(r.get("url") or "").split("/")[2].lower().removeprefix("www.")
        for r in rows
        if str(r.get("url") or "").startswith("http")
    }
    if len(domains) >= 3 and coverage["core_event"]:
        if coverage["quantities"] or coverage["direct_statements"]:
            for soft in ("background", "implications", "next_steps"):
                if not coverage[soft] and len(rows) >= 4:
                    # still mark missing so expansion can try once; do not auto-fill here
                    pass

    missing = [dim for dim in EVIDENCE_DIMENSIONS if not coverage.get(dim)]
    return {
        "coverage": coverage,
        "missing": missing,
        "covered_count": len(EVIDENCE_DIMENSIONS) - len(missing),
        "dimension_count": len(EVIDENCE_DIMENSIONS),
        "sufficient": len(missing) <= 2 and coverage.get("core_event", False),
        "independent_domains": len(domains),
        "evidence_rows": len(rows),
    }


def dimension_targeted_queries(
    event_title: str,
    entities: list[str],
    missing: list[str],
    *,
    max_queries: int = 6,
) -> list[str]:
    """Build search queries aimed at missing dimensions (not same-headline repeats)."""
    head = " ".join(str(e) for e in (entities or [])[:3]).strip() or str(event_title or "").strip()
    out: list[str] = []
    seen: set[str] = set()
    for dim in missing:
        for hint in _DIMENSION_QUERY_HINTS.get(dim, [dim.replace("_", " ")]):
            q = f"{head} {hint}".strip()
            key = re.sub(r"\s+", " ", q.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(q)
            if len(out) >= max_queries:
                return out
    return out
