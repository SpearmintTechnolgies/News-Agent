#!/usr/bin/env python3
"""
audit_article_evidence.py — Phase 4 article evidence audit (audit-only).

Checks factual prose in article/final.md against research/verification.json.
Writes research/article_audit.json. Does NOT rewrite the article. Does NOT
block publishing based on finding severity; pipeline fails only when the
artifact cannot be produced/validated.

Usage:
  python3 audit_article_evidence.py \\
    --article /run/article/final.md \\
    --verification /run/research/verification.json \\
    --output /run/research/article_audit.json \\
    [--validated /run/research/validated.json] \\
    [--consistency /run/research/claim_consistency.json] \\
    [--qualified /run/research/qualified_claims.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import verify_claims as vc  # noqa: E402

COMPARABLE_STATUSES = frozenset({"DIRECT", "CORROBORATED", "SINGLE_SOURCE"})

META_END_RE = re.compile(r"^---\s*$", re.MULTILINE)
HEADING_RE = re.compile(r"^#{1,6}\s+")
FAQ_HEADER_RE = re.compile(r"^##\s+FAQs?\s*$", re.IGNORECASE | re.MULTILINE)
SOURCES_SPLIT_RE = re.compile(
    r"\n(?:\*\*Sources:\*\*|^Sources:)\s*\n",
    re.IGNORECASE | re.MULTILINE,
)
WORD_COUNT_RE = re.compile(
    r"^\[?Word Count:.*?\]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
MD_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def atomic_write_json(path: str, data: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".article_audit_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def strip_markdown_inline(text: str) -> str:
    t = MD_LINK_RE.sub(r"\1", text or "")
    t = MD_BOLD_RE.sub(r"\1", t)
    t = MD_ITALIC_RE.sub(r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def extract_audit_prose(markdown: str) -> str:
    """
    Return main article prose + conclusion only.
    Excludes META, heading lines, FAQ, Sources, Word Count.
    """
    text = markdown or ""
    # Drop META block (META ... ---)
    if text.lstrip().startswith("META"):
        m = META_END_RE.search(text)
        if m:
            text = text[m.end() :]
        else:
            # fall through: drop until first H1
            h1 = re.search(r"^#\s+", text, re.MULTILINE)
            if h1:
                text = text[h1.start() :]

    # Drop Sources footer and beyond
    parts = SOURCES_SPLIT_RE.split(text, maxsplit=1)
    text = parts[0]

    # Drop FAQ section and beyond (keep Conclusion which is before FAQ)
    faq = FAQ_HEADER_RE.search(text)
    if faq:
        text = text[: faq.start()]

    text = WORD_COUNT_RE.sub("", text)

    lines_out: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            lines_out.append("")
            continue
        if HEADING_RE.match(s):
            continue
        if s.lower().startswith("sources:"):
            continue
        if WORD_COUNT_RE.match(s):
            continue
        # Skip bullet markers but keep item text as prose candidates
        if re.match(r"^[-*•]\s+", s):
            s = re.sub(r"^[-*•]\s+", "", s)
        lines_out.append(s)

    prose = "\n".join(lines_out)
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()
    return prose


def extract_faq_prose(markdown: str) -> str:
    """FAQ body only (not counted in main audit)."""
    text = markdown or ""
    parts = SOURCES_SPLIT_RE.split(text, maxsplit=1)
    main = parts[0]
    faq_m = FAQ_HEADER_RE.search(main)
    if not faq_m:
        return ""
    faq = main[faq_m.end() :]
    lines: list[str] = []
    for line in faq.splitlines():
        s = line.strip()
        if not s or HEADING_RE.match(s):
            continue
        s = re.sub(r"^\*\*\d+\.\s+", "", s)
        s = s.strip("* ").strip()
        if s.endswith("?"):
            continue  # question lines
        lines.append(s)
    return "\n".join(lines)


def story_ctx_from_validated(validated: dict[str, Any] | None) -> dict[str, str]:
    validated = validated or {}
    return {
        "primary_headline": str(validated.get("primary_headline") or ""),
        "topic_theme": str(validated.get("topic_theme") or ""),
        "primary_keyword": str(validated.get("primary_keyword") or ""),
        "primary_asset": str(validated.get("primary_asset") or ""),
    }


def strong_entities(text: str, *, story_ctx: dict[str, str] | None = None) -> set[str]:
    anchors = vc.extract_anchors(text or "", story_ctx=story_ctx)
    return {
        e
        for e in (anchors.get("entities") or set())
        if e not in vc.WEAK_ENTITY_TOKENS and len(e) >= 3
    }


def years_of(text: str) -> set[int]:
    out: set[int] = set()
    for n in vc.extract_typed_numbers(text or ""):
        if n.get("kind") == "YEAR":
            out.add(int(float(n["value"])))
    return out


def strong_money(text: str) -> list[dict[str, Any]]:
    return [
        n
        for n in vc.extract_typed_numbers(text or "")
        if n.get("kind") == "MONEY" and vc._number_is_strong(n)
    ]


def strong_percents(text: str) -> list[dict[str, Any]]:
    return [
        n
        for n in vc.extract_typed_numbers(text or "")
        if n.get("kind") == "PERCENT" and vc._number_is_strong(n)
    ]


def is_factual_candidate(sent: str, *, story_ctx: dict[str, str] | None = None) -> bool:
    s = strip_markdown_inline(sent)
    if len(s) < 35:
        return False
    tokens = vc.meaningful_token_count(s)
    if tokens < 5:
        return False

    typed = vc.extract_typed_numbers(s)
    if any(vc._number_is_strong(n) for n in typed):
        return True
    if any(n.get("kind") == "YEAR" for n in typed):
        return True

    ents = strong_entities(s, story_ctx=story_ctx)
    events = vc._event_stems(s)
    if ents and events:
        return True

    story_ents: set[str] = set()
    if story_ctx:
        blob = " ".join(story_ctx.values())
        story_ents = strong_entities(blob, story_ctx=story_ctx)
    if ents & story_ents and tokens >= 8:
        return True
    if ents and tokens >= 10 and (typed or events):
        return True
    return False


def extract_article_claims(
    markdown: str,
    *,
    story_ctx: dict[str, str] | None = None,
) -> list[str]:
    prose = extract_audit_prose(markdown)
    claims: list[str] = []
    seen: set[str] = set()
    for sent in vc.split_sentences(prose):
        clean = strip_markdown_inline(sent)
        if not clean or not is_factual_candidate(clean, story_ctx=story_ctx):
            continue
        key = clean[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        claims.append(clean)
    return claims


def eligible_verification_claims(verification: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in verification.get("claims") or []:
        if not isinstance(c, dict):
            continue
        if str(c.get("status") or "") not in COMPARABLE_STATUSES:
            continue
        if not str(c.get("claim_id") or "").strip():
            continue
        if not str(c.get("claim") or "").strip():
            continue
        out.append(c)
    return out


def consistency_index(consistency: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    """Map claim_id -> list of consistency issues referencing it."""
    idx: dict[str, list[dict[str, Any]]] = {}
    if not consistency or consistency.get("status") != "ok":
        return idx
    for issue in consistency.get("issues") or []:
        if not isinstance(issue, dict):
            continue
        for cid in issue.get("claim_ids") or []:
            key = str(cid)
            idx.setdefault(key, []).append(
                {
                    "issue_id": issue.get("issue_id"),
                    "severity": issue.get("severity"),
                    "rule": issue.get("rule"),
                }
            )
    return idx


def _numbers_conflict(article: str, research: str) -> bool:
    """True when both sides have strong non-year numbers that are incompatible."""
    a = [
        n
        for n in vc.extract_typed_numbers(article)
        if vc._number_is_strong(n) and n.get("kind") != "YEAR"
    ]
    b = [n for n in vc.extract_typed_numbers(research) if n.get("kind") != "YEAR"]
    if not a:
        return False
    return not vc.numbers_compatible(a, b)


def _years_conflict(article: str, research: str) -> bool:
    ya = years_of(article)
    yb = years_of(research)
    if not ya or not yb:
        return False
    only_a = ya - yb
    only_b = yb - ya
    return bool(only_a and only_b)


def score_match(
    article_claim: str,
    research_claim: str,
    *,
    story_ctx: dict[str, str] | None = None,
) -> tuple[float, str, bool]:
    """
    Return (score, match_rule, structural_match).
    structural_match means same-event identity even if numbers/years differ.
    """
    # Strict either direction
    if vc.claim_directly_supported(article_claim, research_claim):
        return 1.0, "STRICT", True
    if vc.claim_directly_supported(research_claim, article_claim):
        return 0.98, "STRICT", True

    ok, meta = vc.support_with_meta(article_claim, research_claim, story_ctx=story_ctx)
    if ok and meta:
        rule = str(meta.get("match_rule") or "STRICT")
        score = 0.92 if rule == "ANCHOR_PARAPHRASE" else 0.95
        soft = float(meta.get("overlap_score") or score)
        return max(score, soft), rule, True

    # Soft identity for mismatch classification
    shared = strong_entities(article_claim, story_ctx=story_ctx) & strong_entities(
        research_claim, story_ctx=story_ctx
    )
    soft = vc.overlap_score(article_claim, research_claim)
    events_a = vc._event_stems(article_claim)
    events_b = vc._event_stems(research_claim)
    shared_events = events_a & events_b

    if len(shared) >= 2 and (shared_events or soft >= 0.45):
        return soft, "ANCHOR_SOFT", True
    if len(shared) >= 2 and soft >= 0.35 and (
        strong_money(article_claim) or years_of(article_claim)
    ):
        return soft, "ANCHOR_SOFT", True
    return soft, "NONE", False


def choose_best_match(
    article_claim: str,
    research_claims: list[dict[str, Any]],
    *,
    story_ctx: dict[str, str] | None = None,
) -> tuple[dict[str, Any] | None, str, float]:
    best: dict[str, Any] | None = None
    best_rule = "NONE"
    best_score = -1.0
    for rc in research_claims:
        score, rule, structural = score_match(
            article_claim, str(rc.get("claim") or ""), story_ctx=story_ctx
        )
        if not structural:
            continue
        if score > best_score:
            best_score = score
            best = rc
            best_rule = rule
    if best is None or best_score < 0.35:
        return None, "NONE", 0.0
    return best, best_rule, best_score


def classify_audit_status(
    article_claim: str,
    matched: dict[str, Any] | None,
    match_rule: str,
    consistency_refs: list[dict[str, Any]],
) -> str:
    if matched is None:
        return "UNMAPPED"

    research_text = str(matched.get("claim") or "")
    if _numbers_conflict(article_claim, research_text):
        return "NUMBER_MISMATCH"
    if _years_conflict(article_claim, research_text):
        return "YEAR_MISMATCH"
    if consistency_refs:
        return "CONFLICT_REF"
    if str(matched.get("status") or "") == "SINGLE_SOURCE":
        return "SINGLE_SOURCE_REF"
    return "SUPPORTED"


def audit_article(
    article_md: str,
    verification: dict[str, Any],
    *,
    validated: dict[str, Any] | None = None,
    consistency: dict[str, Any] | None = None,
    article_source: str = "article/final.md",
    verification_source: str = "research/verification.json",
    consistency_source: str = "research/claim_consistency.json",
) -> dict[str, Any]:
    story_ctx = story_ctx_from_validated(validated)
    research_claims = eligible_verification_claims(verification)
    cons_idx = consistency_index(consistency)
    article_claims = extract_article_claims(article_md, story_ctx=story_ctx)

    rows: list[dict[str, Any]] = []
    for i, claim in enumerate(article_claims, start=1):
        matched, rule, _score = choose_best_match(claim, research_claims, story_ctx=story_ctx)
        refs: list[dict[str, Any]] = []
        matched_id = None
        ver_status = None
        if matched is not None:
            matched_id = str(matched.get("claim_id"))
            ver_status = str(matched.get("status"))
            refs = list(cons_idx.get(matched_id) or [])

        status = classify_audit_status(claim, matched, rule, refs)
        row: dict[str, Any] = {
            "article_claim_id": f"article_{i:03d}",
            "article_claim": claim,
            "matched_claim_id": matched_id,
            "verification_status": ver_status,
            "audit_status": status,
            "match_rule": rule if matched is not None else None,
            "consistency_refs": refs,
        }
        rows.append(row)

    supported = sum(1 for r in rows if r["audit_status"] == "SUPPORTED")
    single = sum(1 for r in rows if r["audit_status"] == "SINGLE_SOURCE_REF")
    unmapped = sum(1 for r in rows if r["audit_status"] == "UNMAPPED")
    num_mm = sum(1 for r in rows if r["audit_status"] == "NUMBER_MISMATCH")
    year_mm = sum(1 for r in rows if r["audit_status"] == "YEAR_MISMATCH")
    conflict = sum(1 for r in rows if r["audit_status"] == "CONFLICT_REF")
    matched_n = sum(1 for r in rows if r.get("matched_claim_id"))

    return {
        "audit_version": 1,
        "status": "ok",
        "mode": "deterministic",
        "article_source": article_source,
        "verification_source": verification_source,
        "consistency_source": consistency_source,
        "summary": {
            "article_claims": len(rows),
            "matched": matched_n,
            "supported": supported,
            "single_source_refs": single,
            "unmapped": unmapped,
            "number_mismatches": num_mm,
            "year_mismatches": year_mm,
            "conflict_refs": conflict,
        },
        "claims": rows,
    }


def audit_ok(doc: dict[str, Any]) -> bool:
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("audit_version") != 1:
        return False
    summary = doc.get("summary")
    claims = doc.get("claims")
    if not isinstance(summary, dict) or not isinstance(claims, list):
        return False
    try:
        total = int(summary.get("article_claims", -1))
        matched = int(summary.get("matched", -1))
        supported = int(summary.get("supported", -1))
        single = int(summary.get("single_source_refs", -1))
        unmapped = int(summary.get("unmapped", -1))
        num_mm = int(summary.get("number_mismatches", -1))
        year_mm = int(summary.get("year_mismatches", -1))
        conflict = int(summary.get("conflict_refs", -1))
    except (TypeError, ValueError):
        return False
    if total != len(claims):
        return False
    if supported + single + unmapped + num_mm + year_mm + conflict != total:
        return False
    if matched > total or matched < 0:
        return False
    for row in claims:
        if not isinstance(row, dict):
            return False
        if row.get("audit_status") not in (
            "SUPPORTED",
            "UNMAPPED",
            "NUMBER_MISMATCH",
            "YEAR_MISMATCH",
            "CONFLICT_REF",
            "SINGLE_SOURCE_REF",
        ):
            return False
        if not row.get("article_claim_id") or not row.get("article_claim"):
            return False
    return True


def run_audit(
    article_path: str,
    verification_path: str,
    output_path: str,
    *,
    validated_path: str | None = None,
    consistency_path: str | None = None,
    qualified_path: str | None = None,
) -> tuple[int, dict]:
    del qualified_path  # accepted for CLI/pipeline symmetry; not required for V1 mapping

    try:
        with open(article_path, encoding="utf-8", errors="replace") as f:
            article_md = f.read()
    except OSError as e:
        err = {"audit_version": 1, "status": "error", "reason": f"read_article: {e}"}
        atomic_write_json(output_path, err)
        print(f"ARTICLE_AUDIT_ERROR: {e}", file=sys.stderr)
        return 1, err

    try:
        with open(verification_path, encoding="utf-8") as f:
            verification = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        err = {"audit_version": 1, "status": "error", "reason": f"read_verification: {e}"}
        atomic_write_json(output_path, err)
        print(f"ARTICLE_AUDIT_ERROR: {e}", file=sys.stderr)
        return 1, err

    if not isinstance(verification, dict) or verification.get("status") != "ok":
        err = {"audit_version": 1, "status": "error", "reason": "verification_invalid"}
        atomic_write_json(output_path, err)
        print("ARTICLE_AUDIT_ERROR: verification_invalid", file=sys.stderr)
        return 1, err

    validated: dict[str, Any] | None = None
    if validated_path and os.path.isfile(validated_path):
        try:
            with open(validated_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                validated = loaded
        except (OSError, json.JSONDecodeError):
            validated = None

    consistency: dict[str, Any] | None = None
    if consistency_path and os.path.isfile(consistency_path):
        try:
            with open(consistency_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict) and loaded.get("status") == "ok":
                consistency = loaded
        except (OSError, json.JSONDecodeError):
            consistency = None

    if os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                existing = json.load(f)
            if audit_ok(existing):
                print("ARTICLE_AUDIT_RESUME: article_audit.json already valid", file=sys.stderr)
                return 0, existing
        except (OSError, json.JSONDecodeError):
            pass

    doc = audit_article(
        article_md,
        verification,
        validated=validated,
        consistency=consistency,
    )
    if not audit_ok(doc):
        doc["status"] = "error"
        doc["reason"] = "invalid_audit"
        atomic_write_json(output_path, doc)
        print("ARTICLE_AUDIT_ERROR: invalid_audit", file=sys.stderr)
        return 1, doc

    atomic_write_json(output_path, doc)
    s = doc["summary"]
    print(
        f"ARTICLE_AUDIT_OK: claims={s['article_claims']} supported={s['supported']} "
        f"unmapped={s['unmapped']} number_mm={s['number_mismatches']} "
        f"year_mm={s['year_mismatches']} conflict={s['conflict_refs']} "
        f"single={s['single_source_refs']}",
        file=sys.stderr,
    )
    return 0, doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Article evidence audit Phase 4 (audit-only)")
    ap.add_argument("--article", required=True, help="path to article/final.md")
    ap.add_argument("--verification", required=True, help="path to verification.json")
    ap.add_argument("--output", required=True, help="path to article_audit.json")
    ap.add_argument("--validated", default=None, help="optional validated.json")
    ap.add_argument("--consistency", default=None, help="optional claim_consistency.json")
    ap.add_argument("--qualified", default=None, help="optional qualified_claims.json")
    args = ap.parse_args()
    code, _ = run_audit(
        os.path.realpath(args.article),
        os.path.realpath(args.verification),
        os.path.realpath(args.output),
        validated_path=os.path.realpath(args.validated) if args.validated else None,
        consistency_path=os.path.realpath(args.consistency) if args.consistency else None,
        qualified_path=os.path.realpath(args.qualified) if args.qualified else None,
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
