"""Deterministic bake-off scoring from existing article QA. No LLM."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.structure import check_structure
from newsagent_v2.bench.writer_bakeoff.contract import (
    GROUNDING_CODES,
    HARD_MIN_WORDS,
    HEADLINE_CODES,
    MECHANICS_CODES,
    PREFERRED_MAX_WORDS,
    PREFERRED_MIN_WORDS,
    QUOTE_CODES,
    SIMILARITY_CODES,
    STRUCTURE_CODES,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)


def _codes(qa: dict[str, Any] | None, *, module: str | None = None) -> list[str]:
    found: list[str] = []
    for item in (qa or {}).get("critical_failures") or []:
        if not isinstance(item, dict) or not item.get("code"):
            continue
        if module and item.get("module") != module:
            continue
        found.append(str(item["code"]))
    return found


def _pass_fail(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def score_result(
    *,
    provider: str,
    model: str,
    mode: str,
    qa: dict[str, Any] | None,
    article: dict[str, Any] | None,
    article_input: dict[str, Any],
    http_status: int | None,
    latency_ms: int | None,
    retries: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    total_tokens: int | None,
    estimated_list_price_usd: float | None,
    provider_reported_cost_usd: float | None,
    candidate_id: str,
    replay: bool = False,
) -> dict[str, Any]:
    qa = qa or {}
    metrics = qa.get("metrics") if isinstance(qa.get("metrics"), dict) else {}
    words = int(metrics.get("article_word_count") or 0)
    if not isinstance(article, dict):
        structure_ok = False
    else:
        structure_issues = check_structure(article, article_input)
        structure_ok = not any(item.get("code") in STRUCTURE_CODES for item in structure_issues)
    hard_ok = words >= HARD_MIN_WORDS and "below_article_minimum_length" not in _codes(qa)
    target_ok = TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS
    quote_codes = [code for code in _codes(qa) if code in QUOTE_CODES]
    ground_codes = [code for code in _codes(qa) if code in GROUNDING_CODES]
    sim_codes = [code for code in _codes(qa) if code in SIMILARITY_CODES]
    headline_ok = not any(code in HEADLINE_CODES for code in _codes(qa))
    mechanics_ok = not any(code in MECHANICS_CODES for code in _codes(qa))
    seo_ok = int(metrics.get("seo_issue_count") or 0) == 0
    publishable = bool(qa.get("publishable"))
    eligible = bool(
        publishable
        and hard_ok
        and structure_ok
        and not ground_codes
        and not quote_codes
        and not sim_codes
    )
    preferred_depth = PREFERRED_MIN_WORDS <= words <= PREFERRED_MAX_WORDS
    return {
        "candidate_id": candidate_id,
        "provider": provider,
        "model": model,
        "mode": mode,
        "replay": replay,
        "http_status": http_status,
        "latency_ms": latency_ms,
        "retries": retries,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "rendered_word_count": words,
        "hard_length": _pass_fail(hard_ok),
        "target_length": _pass_fail(target_ok),
        "preferred_depth": _pass_fail(preferred_depth),
        "structure": _pass_fail(structure_ok),
        "claim_coverage": metrics.get("body_claim_coverage"),
        "assertive_sentence_count": metrics.get("assertive_sentence_count"),
        "covered_assertions": metrics.get("claim_covered_sentence_count"),
        "uncovered_assertions": metrics.get("uncovered_assertive_sentence_count"),
        "uncovered_assertive_sentences": metrics.get("uncovered_assertive_sentences") or [],
        "grounding_criticals": ground_codes,
        "depth_criticals": [code for code in _codes(qa) if code == "below_article_minimum_length"],
        "quote_criticals": quote_codes,
        "contextual_absence_criticals": [
            code
            for code in _codes(qa)
            if code in {"ungrounded_contextual_assertion", "unsupported_absence_claim"}
        ],
        "exact_overlap": metrics.get("exact_overlap_count"),
        "ngram_hits": metrics.get("exact_overlap_ngram_hits"),
        "max_similarity": metrics.get("max_similarity"),
        "headline_integrity": _pass_fail(headline_ok),
        "mechanics": _pass_fail(mechanics_ok),
        "seo": _pass_fail(seo_ok),
        "qa_publishable": publishable,
        "estimated_list_price_usd": estimated_list_price_usd,
        "provider_reported_cost_usd": provider_reported_cost_usd,
        "winner_eligible": eligible,
        "native_parse": None,
        "normalization": None,
        "provider_error": None,
        "qa_thresholds": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
    }


def pick_winner(scores: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [
        row
        for row in scores
        if row.get("winner_eligible")
        and not row.get("replay")
        and row.get("normalization") != "FAIL"
    ]
    if not eligible:
        return {"winner": None, "reason": "NO WINNER", "eligible_count": 0}
    def key(row: dict[str, Any]) -> tuple:
        coverage = float(row.get("claim_coverage") or 0.0)
        words = int(row.get("rendered_word_count") or 0)
        depth_penalty = 0 if PREFERRED_MIN_WORDS <= words <= PREFERRED_MAX_WORDS else abs(words - 575)
        latency = int(row.get("latency_ms") or 10**9)
        cost = float(row.get("provider_reported_cost_usd") or row.get("estimated_list_price_usd") or 10**9)
        return (-coverage, depth_penalty, latency, cost)

    winner = sorted(eligible, key=key)[0]
    return {
        "winner": winner["candidate_id"],
        "reason": "qa_publishable_with_stronger_grounding_then_depth_latency_cost",
        "eligible_count": len(eligible),
        "score": winner,
    }
