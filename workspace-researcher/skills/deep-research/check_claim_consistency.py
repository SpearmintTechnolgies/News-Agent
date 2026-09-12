#!/usr/bin/env python3
"""
check_claim_consistency.py — Phase 3 story-level cross-claim consistency (audit-only).

Reads research/verification.json and writes research/claim_consistency.json.
Does NOT mutate verification claim statuses. Does NOT block Quill by itself;
pipeline integration fails only when the artifact cannot be produced.

Usage:
  python3 check_claim_consistency.py \\
    --verification /run/research/verification.json \\
    --output /run/research/claim_consistency.json
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

# High-precision antonym rules only (Phase 3).
ANTONYM_RULES: list[tuple[str, tuple[tuple[str, str], ...]]] = [
    (
        "APPROVED_VS_REJECTED",
        (
            ("approved", "rejected"),
            ("approve", "reject"),
        ),
    ),
    (
        "LAUNCHED_VS_CANCELLED",
        (
            ("launched", "cancelled"),
            ("launched", "canceled"),
            ("launch", "cancel"),
            ("launch", "cancelled"),
            ("launch", "canceled"),
        ),
    ),
    (
        "LISTED_VS_DELISTED",
        (
            ("listed", "delisted"),
            ("list", "delist"),
        ),
    ),
]


def atomic_write_json(path: str, data: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".consistency_", suffix=".json", dir=parent)
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


def strong_entities(text: str, *, story_ctx: dict[str, str] | None = None) -> set[str]:
    """Entity anchors minus weak generics (from verify_claims helpers)."""
    anchors = vc.extract_anchors(text or "", story_ctx=story_ctx)
    ents = set(anchors.get("entities") or set())
    return {e for e in ents if e not in vc.WEAK_ENTITY_TOKENS and len(e) >= 3}


def money_values(text: str) -> list[dict[str, Any]]:
    return [n for n in vc.extract_typed_numbers(text or "") if n.get("kind") == "MONEY"]


def year_values(text: str) -> list[int]:
    years: list[int] = []
    for n in vc.extract_typed_numbers(text or ""):
        if n.get("kind") == "YEAR":
            years.append(int(float(n["value"])))
    return sorted(set(years))


def money_compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("kind") != "MONEY" or b.get("kind") != "MONEY":
        return False
    if abs(float(a.get("value") or 0) - float(b.get("value") or 0)) > 1e-6:
        return False
    return (a.get("scale") or "NONE") == (b.get("scale") or "NONE")


def format_money(n: dict[str, Any]) -> str:
    raw = str(n.get("raw") or "").strip()
    if raw:
        return raw
    val = n.get("value")
    scale = (n.get("scale") or "NONE").lower()
    if scale == "none":
        return f"${val:g}"
    return f"${val:g} {scale}"


def _word_present(text: str, word: str) -> bool:
    return bool(re.search(rf"\b{re.escape(word)}\b", text or "", re.I))


def filter_comparable_claims(verification: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in verification.get("claims") or []:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "")
        if status not in COMPARABLE_STATUSES:
            continue
        cid = str(c.get("claim_id") or "").strip()
        text = str(c.get("claim") or "").strip()
        if not cid or not text:
            continue
        out.append(c)
    return out


def _pair_passes_overlap_filter(
    shared: set[str],
    *,
    min_shared: int = 1,
) -> bool:
    return len(shared) >= min_shared


def check_entity_money_year(
    claim_a: dict[str, Any],
    claim_b: dict[str, Any],
    shared: set[str],
) -> dict[str, Any] | None:
    if len(shared) < 2:
        return None
    monies_a = money_values(str(claim_a.get("claim") or ""))
    monies_b = money_values(str(claim_b.get("claim") or ""))
    years_a = year_values(str(claim_a.get("claim") or ""))
    years_b = year_values(str(claim_b.get("claim") or ""))
    if not monies_a or not monies_b or not years_a or not years_b:
        return None

    matched_money: dict[str, Any] | None = None
    for ma in monies_a:
        for mb in monies_b:
            if money_compatible(ma, mb):
                matched_money = ma
                break
        if matched_money is not None:
            break
    if matched_money is None:
        return None

    # Years differ (any year from A not in B or vice versa with no overlap).
    if set(years_a) == set(years_b):
        return None
    if set(years_a) & set(years_b) and not (set(years_a) - set(years_b) or set(years_b) - set(years_a)):
        return None
    # Require at least one year unique to each side (classic 2024 vs 2025).
    only_a = set(years_a) - set(years_b)
    only_b = set(years_b) - set(years_a)
    if not only_a or not only_b:
        return None

    money_label = format_money(matched_money)
    ya = sorted(only_a)[0]
    yb = sorted(only_b)[0]
    shared_anchors = sorted(shared)[:8]
    if money_label not in shared_anchors:
        shared_anchors.append(money_label)

    return {
        "severity": "POTENTIAL_CONFLICT",
        "rule": "ENTITY_MONEY_YEAR_MISMATCH",
        "claim_ids": [str(claim_a["claim_id"]), str(claim_b["claim_id"])],
        "shared_anchors": shared_anchors,
        "conflicting_values": [str(ya), str(yb)],
        "reason": (
            "Both claims share strong entity anchors and a compatible monetary amount "
            f"({money_label}) but contain different years ({ya} vs {yb}); "
            "this may represent announcement versus closing/completion, which current "
            "code cannot reliably distinguish."
        ),
    }


def check_antonym_rule(
    claim_a: dict[str, Any],
    claim_b: dict[str, Any],
    shared: set[str],
    *,
    rule: str,
    pairs: tuple[tuple[str, str], ...],
) -> dict[str, Any] | None:
    if len(shared) < 1:
        return None
    text_a = str(claim_a.get("claim") or "")
    text_b = str(claim_b.get("claim") or "")
    low_a = text_a.lower()
    low_b = text_b.lower()

    hit: tuple[str, str] | None = None
    for left, right in pairs:
        a_has_l = _word_present(low_a, left)
        a_has_r = _word_present(low_a, right)
        b_has_l = _word_present(low_b, left)
        b_has_r = _word_present(low_b, right)
        if (a_has_l and b_has_r) or (a_has_r and b_has_l):
            hit = (left, right)
            break
    if hit is None:
        return None

    return {
        "severity": "LIKELY_CONFLICT",
        "rule": rule,
        "claim_ids": [str(claim_a["claim_id"]), str(claim_b["claim_id"])],
        "shared_anchors": sorted(shared)[:8],
        "conflicting_values": [hit[0], hit[1]],
        "reason": (
            f"Claims share strong entity anchor(s) but use opposing event language "
            f"({hit[0]} vs {hit[1]})."
        ),
    }


def compare_pair(
    claim_a: dict[str, Any],
    claim_b: dict[str, Any],
    *,
    story_ctx: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    ents_a = strong_entities(str(claim_a.get("claim") or ""), story_ctx=story_ctx)
    ents_b = strong_entities(str(claim_b.get("claim") or ""), story_ctx=story_ctx)
    shared = ents_a & ents_b
    if not _pair_passes_overlap_filter(shared, min_shared=1):
        return None

    # Prefer LIKELY antonym rules first.
    for rule, pairs in ANTONYM_RULES:
        issue = check_antonym_rule(claim_a, claim_b, shared, rule=rule, pairs=pairs)
        if issue:
            return issue

    return check_entity_money_year(claim_a, claim_b, shared)


def check_consistency(
    verification: dict[str, Any],
    *,
    verification_source: str = "research/verification.json",
    story_ctx: dict[str, str] | None = None,
) -> dict[str, Any]:
    claims = filter_comparable_claims(verification)
    issues: list[dict[str, Any]] = []
    pairs_checked = 0
    n = len(claims)

    for i in range(n):
        for j in range(i + 1, n):
            ents_a = strong_entities(str(claims[i].get("claim") or ""), story_ctx=story_ctx)
            ents_b = strong_entities(str(claims[j].get("claim") or ""), story_ctx=story_ctx)
            shared = ents_a & ents_b
            if not _pair_passes_overlap_filter(shared, min_shared=1):
                continue
            pairs_checked += 1
            issue = compare_pair(claims[i], claims[j], story_ctx=story_ctx)
            if issue:
                issues.append(issue)

    # Stable order: LIKELY first, then POTENTIAL; then claim_ids
    sev_rank = {"LIKELY_CONFLICT": 0, "POTENTIAL_CONFLICT": 1}
    issues.sort(
        key=lambda x: (
            sev_rank.get(str(x.get("severity")), 9),
            x.get("claim_ids") or [],
            x.get("rule") or "",
        )
    )
    for idx, issue in enumerate(issues, start=1):
        issue["issue_id"] = f"consistency_{idx:03d}"

    potential = sum(1 for x in issues if x.get("severity") == "POTENTIAL_CONFLICT")
    likely = sum(1 for x in issues if x.get("severity") == "LIKELY_CONFLICT")
    none = max(0, pairs_checked - potential - likely)

    return {
        "consistency_version": 1,
        "status": "ok",
        "mode": "deterministic",
        "verification_source": verification_source,
        "summary": {
            "claims_compared": n,
            "pairs_checked": pairs_checked,
            "none": none,
            "potential_conflict": potential,
            "likely_conflict": likely,
        },
        "issues": issues,
    }


def consistency_ok(doc: dict[str, Any], expected_claim_ids: list[str] | None = None) -> bool:
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("consistency_version") != 1:
        return False
    summary = doc.get("summary")
    issues = doc.get("issues")
    if not isinstance(summary, dict) or not isinstance(issues, list):
        return False
    try:
        claims_compared = int(summary.get("claims_compared", -1))
        pairs_checked = int(summary.get("pairs_checked", -1))
        none = int(summary.get("none", -1))
        potential = int(summary.get("potential_conflict", -1))
        likely = int(summary.get("likely_conflict", -1))
    except (TypeError, ValueError):
        return False
    if claims_compared < 0 or pairs_checked < 0:
        return False
    if none + potential + likely != pairs_checked:
        return False
    if potential + likely != len(issues):
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        if issue.get("severity") not in ("POTENTIAL_CONFLICT", "LIKELY_CONFLICT"):
            return False
        if not issue.get("issue_id") or not issue.get("rule"):
            return False
        ids = issue.get("claim_ids")
        if not isinstance(ids, list) or len(ids) != 2:
            return False
    if expected_claim_ids is not None:
        # Soft check: claims_compared should match comparable set size.
        if claims_compared != len(expected_claim_ids):
            return False
    return True


def run_consistency(
    verification_path: str,
    output_path: str,
    *,
    verification_source: str = "research/verification.json",
) -> tuple[int, dict]:
    try:
        with open(verification_path, encoding="utf-8") as f:
            verification = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        err = {
            "consistency_version": 1,
            "status": "error",
            "reason": f"read_verification: {e}",
        }
        atomic_write_json(output_path, err)
        print(f"CONSISTENCY_ERROR: {e}", file=sys.stderr)
        return 1, err

    if not isinstance(verification, dict) or verification.get("status") != "ok":
        err = {
            "consistency_version": 1,
            "status": "error",
            "reason": "verification_invalid",
        }
        atomic_write_json(output_path, err)
        print("CONSISTENCY_ERROR: verification_invalid", file=sys.stderr)
        return 1, err

    comparable = filter_comparable_claims(verification)
    expected_ids = [str(c.get("claim_id")) for c in comparable]

    if os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as f:
                existing = json.load(f)
            if consistency_ok(existing, expected_ids):
                print("CONSISTENCY_RESUME: claim_consistency.json already valid", file=sys.stderr)
                return 0, existing
        except (OSError, json.JSONDecodeError):
            pass

    doc = check_consistency(
        verification,
        verification_source=verification_source,
    )
    if not consistency_ok(doc, expected_ids):
        doc["status"] = "error"
        doc["reason"] = "invalid_consistency"
        atomic_write_json(output_path, doc)
        print("CONSISTENCY_ERROR: invalid_consistency", file=sys.stderr)
        return 1, doc

    atomic_write_json(output_path, doc)
    s = doc["summary"]
    print(
        f"CONSISTENCY_OK: compared={s['claims_compared']} pairs={s['pairs_checked']} "
        f"potential={s['potential_conflict']} likely={s['likely_conflict']}",
        file=sys.stderr,
    )
    return 0, doc


def main() -> int:
    ap = argparse.ArgumentParser(description="Story-level cross-claim consistency (Phase 3)")
    ap.add_argument("--verification", required=True, help="path to verification.json")
    ap.add_argument("--output", required=True, help="path to claim_consistency.json")
    args = ap.parse_args()
    code, _ = run_consistency(
        os.path.realpath(args.verification),
        os.path.realpath(args.output),
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
