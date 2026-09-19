"""Freeze a live Top-1 batch into an immutable writer-bake-off fixture. No network."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from newsagent_v2.article.expand import expand_provider_article, evidence_id_index
from newsagent_v2.article.prompt_evidence import compact_story_evidence
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY, WARN_SENTENCE_SIMILARITY
from newsagent_v2.article.render import materialize_article
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.bench.writer_bakeoff.contract import (
    EVENT_ID,
    HARD_MIN_WORDS,
    SOURCE_BATCH_ID,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.benchmark.input import write_json_utf8

REPO_ROOT = Path(__file__).resolve().parents[4]


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_payload(payload: Any) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _story_from_batch(batch: dict[str, Any]) -> dict[str, Any]:
    viability = batch.get("viability") or (batch.get("telemetry") or {}).get("viability") or {}
    stories = viability.get("stories") if isinstance(viability, dict) else None
    if isinstance(stories, list) and stories:
        return deepcopy(stories[0])
    raise ValueError("batch has no viability.stories")


def freeze_top1_batch(
    *,
    batch_path: Path,
    dest_dir: Path,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    batch_path = Path(batch_path).resolve()
    dest_dir = Path(dest_dir)
    if repo_root is not None:
        repo_root = Path(repo_root).resolve()
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    batch_id = str(batch.get("batch_id") or "")
    if batch_id != SOURCE_BATCH_ID:
        raise ValueError(f"expected batch {SOURCE_BATCH_ID}, got {batch_id}")
    telemetry = batch.get("telemetry") if isinstance(batch.get("telemetry"), dict) else {}
    editorial = telemetry.get("editorial") if isinstance(telemetry.get("editorial"), dict) else {}
    viability = batch.get("viability") if isinstance(batch.get("viability"), dict) else {}
    story = _story_from_batch(batch)
    if str(story.get("event_id")) != EVENT_ID:
        raise ValueError(f"expected {EVENT_ID}, got {story.get('event_id')}")
    article_input = deepcopy(story["article_input"])
    article = deepcopy(story["article"])
    expand_provider_article(article, article_input)
    materialize_article(article)
    qa = run_article_qa(deepcopy(article), deepcopy(article_input), article_mode="normal")
    compact = compact_story_evidence({"event_id": EVENT_ID, "article_input": article_input})
    evidence_ids = sorted(evidence_id_index(article_input).keys())
    dest_dir.mkdir(parents=True, exist_ok=True)
    qa_config = {
        "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
        "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
        "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        "exact_phrase_n": EXACT_PHRASE_N,
        "critical_similarity": HIGH_SENTENCE_SIMILARITY,
        "warning_similarity": WARN_SENTENCE_SIMILARITY,
        "make_story_count": MAKE_STORY_COUNT,
    }
    evidence_hash = sha256_payload(article_input)
    compact_hash = sha256_payload(compact)
    article_hash = sha256_payload(article)
    manifest = {
        "schema_version": "writer-bakeoff-fixture-v1",
        "source_batch_id": batch_id,
        "source_batch_path": str(batch_path.relative_to(repo_root)) if repo_root and repo_root in batch_path.parents else str(batch_path),
        "timestamp_utc": telemetry.get("timestamp_utc"),
        "event_id": EVENT_ID,
        "original_rank": story.get("original_rank"),
        "backfill": story.get("backfill"),
        "viability_status": story.get("viability_status"),
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "category": article.get("category"),
        "representative_title": article_input.get("representative_title"),
        "evidence_sufficiency": article_input.get("evidence_sufficiency") or story.get("evidence_sufficiency"),
        "evidence_ids": evidence_ids,
        "sources": article_input.get("sources"),
        "source_urls": [
            row.get("url")
            for row in (article_input.get("evidence") or [])
            if isinstance(row, dict)
        ],
        "hashes": {
            "article_input_sha256": evidence_hash,
            "compact_writer_input_sha256": compact_hash,
            "baseline_article_sha256": article_hash,
        },
        "top1_proof": {
            "expected_count": telemetry.get("expected_count"),
            "selected_count": telemetry.get("selected_count") or viability.get("selected_count"),
            "selected_event_ids": batch.get("selected_event_ids") or telemetry.get("selected_stories"),
            "viability_target": viability.get("target"),
            "scanned_count": viability.get("scanned_count"),
            "original_top_ids": viability.get("original_top_ids"),
            "backfilled_ids": viability.get("backfilled_ids"),
            "make_story_count": MAKE_STORY_COUNT,
            "completion_summary": batch.get("summary"),
            "image_request_count": telemetry.get("image_request_count"),
            "editorial_requested_event_ids": editorial.get("requested_event_ids"),
        },
        "baseline_a": {
            "provider": editorial.get("provider"),
            "model": editorial.get("model"),
            "http_status": editorial.get("http_status"),
            "latency_ms": editorial.get("latency_ms"),
            "retries": editorial.get("retries"),
            "prompt_tokens": editorial.get("prompt_tokens"),
            "completion_tokens": editorial.get("completion_tokens"),
            "total_tokens": editorial.get("total_tokens"),
            "provider_reported_cost_usd": editorial.get("provider_reported_cost_usd"),
            "estimated_list_price_usd": editorial.get("estimated_list_price_usd"),
            "max_completion_tokens": (editorial.get("request_diagnostics") or {}).get("max_completion_tokens"),
            "rendered_words": (qa.get("metrics") or {}).get("article_word_count"),
            "qa_publishable": qa.get("publishable"),
        },
        "refetch": False,
        "rediscover": False,
        "notes": (
            "Frozen from the live Top-1 /make artifact. Do not refetch URLs. "
            "Every writer must receive this same article_input."
        ),
    }
    write_json_utf8(dest_dir / "manifest.json", manifest)
    write_json_utf8(dest_dir / "article_input.json", article_input)
    write_json_utf8(dest_dir / "compact_writer_input.json", compact)
    write_json_utf8(dest_dir / "baseline_a_groq_article.json", article)
    write_json_utf8(dest_dir / "baseline_a_qa.json", qa)
    write_json_utf8(dest_dir / "qa_config.json", qa_config)
    write_json_utf8(
        dest_dir / "baseline_a_editorial_telemetry.json",
        {
            "provider": editorial.get("provider"),
            "model": editorial.get("model"),
            "http_status": editorial.get("http_status"),
            "latency_ms": editorial.get("latency_ms"),
            "retries": editorial.get("retries"),
            "prompt_tokens": editorial.get("prompt_tokens"),
            "completion_tokens": editorial.get("completion_tokens"),
            "total_tokens": editorial.get("total_tokens"),
            "provider_reported_cost_usd": editorial.get("provider_reported_cost_usd"),
            "estimated_list_price_usd": editorial.get("estimated_list_price_usd"),
            "request_diagnostics": editorial.get("request_diagnostics") or {},
            "provider_error": editorial.get("provider_error"),
        },
    )
    if int((qa.get("metrics") or {}).get("article_word_count") or 0) != 283:
        raise ValueError("frozen QA word count is not 283")
    if HARD_MIN_WORDS != 350 or TARGET_MIN_WORDS != 450 or TARGET_MAX_WORDS != 800:
        raise ValueError("QA thresholds drifted while freezing fixture")
    return manifest
