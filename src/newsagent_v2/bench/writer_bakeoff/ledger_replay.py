"""Offline diagnosis and ledger replay of persisted writer bodies. No model calls."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.grounding import (
    content_tokens,
    sentence_covered_by_claims,
)
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID

CLASS_UNSUPPORTED = "UNSUPPORTED"
CLASS_SUPPORTED_UNMAPPED = "SUPPORTED_BUT_UNMAPPED"
CLASS_SEGMENTATION = "SEGMENTATION_OR_MATCHER"
CLASS_AMBIGUOUS = "AMBIGUOUS"
PM_END_RE = re.compile(r"\b(?:a|p)\.m\.$", re.IGNORECASE)
ET_START_RE = re.compile(r"^ET\b")
LOW_OVERLAP = 0.35

HISTORICAL_RUNS = (
    (
        "gemini_3_6_flash",
        FIXTURE / "live_runs" / "20260915T092107Z" / "gemini_3_6_flash_article_first",
    ),
    (
        "groq_gpt_oss_120b",
        FIXTURE / "live_runs" / "20260915T085821Z" / "groq_gpt_oss_120b_article_first",
    ),
    (
        "groq_qwen_3_8_27b",
        FIXTURE / "live_runs" / "20260915T131315Z" / "groq_qwen_3_8_27b_article_first",
    ),
    (
        "kimi_k2_5",
        FIXTURE / "live_runs" / "20260915T141219Z" / "bedrock_mantle_kimi_k2_5_article_first",
    ),
)
WINNER_DIR = FIXTURE / "DEVELOPMENT_WINNER_CANDIDATE"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _max_overlap(sentence: str, evidence_texts: list[str]) -> float:
    sent = set(content_tokens(sentence))
    if not sent:
        return 0.0
    best = 0.0
    for text in evidence_texts:
        other = set(content_tokens(text))
        if not other:
            continue
        best = max(best, len(sent & other) / len(sent))
    return best


def _segmentation_pair(prev: str, current: str) -> bool:
    return bool(PM_END_RE.search(prev.strip()) and ET_START_RE.match(current.strip()))


def classify_uncovered_sentence(
    sentence: str,
    *,
    evidence_texts: list[str],
    all_uncovered: list[str],
) -> str:
    if sentence_covered_by_claims(sentence, evidence_texts):
        return CLASS_SUPPORTED_UNMAPPED
    idx = all_uncovered.index(sentence) if sentence in all_uncovered else -1
    prev = all_uncovered[idx - 1] if idx > 0 else ""
    nxt = all_uncovered[idx + 1] if 0 <= idx < len(all_uncovered) - 1 else ""
    if _segmentation_pair(prev, sentence) or _segmentation_pair(sentence, nxt) or ET_START_RE.match(sentence.strip()):
        return CLASS_SEGMENTATION
    if _max_overlap(sentence, evidence_texts) < LOW_OVERLAP:
        return CLASS_UNSUPPORTED
    return CLASS_AMBIGUOUS


def diagnose_run(name: str, run_dir: Path, article_input: dict[str, Any]) -> dict[str, Any]:
    qa = _load_json(run_dir / "qa.json")
    metrics = qa.get("metrics") or {}
    uncovered = list(metrics.get("uncovered_assertive_sentences") or [])
    ledgers = build_evidence_ledgers(article_input)
    evidence_texts = ledgers.claim_texts() + [row.text for row in ledgers.quotes]
    rows = []
    counts = {
        CLASS_UNSUPPORTED: 0,
        CLASS_SUPPORTED_UNMAPPED: 0,
        CLASS_SEGMENTATION: 0,
        CLASS_AMBIGUOUS: 0,
    }
    for sentence in uncovered:
        bucket = classify_uncovered_sentence(
            sentence,
            evidence_texts=evidence_texts,
            all_uncovered=uncovered,
        )
        counts[bucket] += 1
        rows.append({"sentence": sentence, "class": bucket})
    return {
        "model": name,
        "assertive_sentence_count": metrics.get("assertive_sentence_count"),
        "claim_covered_sentence_count": metrics.get("claim_covered_sentence_count"),
        "body_claim_coverage": metrics.get("body_claim_coverage"),
        "uncovered_count": len(uncovered),
        "counts": counts,
        "rows": rows,
        "qa_publishable": bool(qa.get("publishable")),
        "critical_failures": [item.get("code") for item in qa.get("critical_failures") or []],
    }


def replay_run(name: str, run_dir: Path, article_input: dict[str, Any]) -> dict[str, Any]:
    original_article = _load_json(run_dir / "article.json")
    original_qa = _load_json(run_dir / "qa.json")
    original_metrics = original_qa.get("metrics") or {}
    original_body = original_article.get("article_body")
    diagnosis = diagnose_run(name, run_dir, article_input)
    ledgers = build_evidence_ledgers(article_input)
    mapped = resolve_grounding(original_article, article_input, ledgers=ledgers)
    assert mapped.get("article_body") == original_body
    replay_qa = run_article_qa(deepcopy(mapped), article_input, article_mode="normal")
    replay_metrics = replay_qa.get("metrics") or {}
    uncovered = list(replay_metrics.get("uncovered_assertive_sentences") or [])
    return {
        "model": name,
        "original_assertive_sentence_count": original_metrics.get("assertive_sentence_count"),
        "original_covered_count": original_metrics.get("claim_covered_sentence_count"),
        "original_coverage": original_metrics.get("body_claim_coverage"),
        "unsupported_count": diagnosis["counts"][CLASS_UNSUPPORTED],
        "supported_but_unmapped_count": diagnosis["counts"][CLASS_SUPPORTED_UNMAPPED],
        "matcher_segmentation_count": diagnosis["counts"][CLASS_SEGMENTATION],
        "ambiguous_count": diagnosis["counts"][CLASS_AMBIGUOUS],
        "new_deterministic_mapped_count": replay_metrics.get("claim_covered_sentence_count"),
        "new_coverage": replay_metrics.get("body_claim_coverage"),
        "remaining_unmapped_assertions": uncovered,
        "remaining_unmapped_count": len(uncovered),
        "quote_mappings": mapped.get("_ledger_mapped_quote_ids") or [],
        "mapped_claim_ids": mapped.get("_ledger_mapped_claim_ids") or [],
        "qa_publishable": "YES" if replay_qa.get("publishable") else "NO",
        "critical_failures": [item.get("code") for item in replay_qa.get("critical_failures") or []],
        "prose_unchanged": mapped.get("article_body") == original_body,
        "ledger_claim_ids": [row.claim_id for row in ledgers.claims],
    }


def run_offline_ledger_study() -> dict[str, Any]:
    fixture = load_fixture(FIXTURE)
    article_input = fixture["article_input"]
    diagnoses = []
    replays = []
    totals = {
        CLASS_UNSUPPORTED: 0,
        CLASS_SUPPORTED_UNMAPPED: 0,
        CLASS_SEGMENTATION: 0,
        CLASS_AMBIGUOUS: 0,
    }
    analyzed = 0
    for name, run_dir in HISTORICAL_RUNS:
        diagnosis = diagnose_run(name, run_dir, article_input)
        diagnoses.append(diagnosis)
        analyzed += diagnosis["uncovered_count"]
        for key, value in diagnosis["counts"].items():
            totals[key] += value
        replays.append(replay_run(name, run_dir, article_input))
    winner = None
    if (WINNER_DIR / "qa.json").is_file():
        winner = diagnose_run("DEVELOPMENT_WINNER_CANDIDATE", WINNER_DIR, article_input)
    return {
        "analyzed_body_assertion_not_in_claims": analyzed,
        "totals": totals,
        "diagnoses": diagnoses,
        "replays": replays,
        "development_winner_comparison_only": winner,
        "qa_changed": "NO",
        "evidence_changed": "NO",
        "article_prose_changed": "NO",
        "external_model_calls": 0,
        "kimi_calls": 0,
        "make_invoked": False,
        "telegram": "NO",
        "wordpress": "NO",
    }
