"""One-call Qwen3.8-27B writer audition on frozen event-005. No /make. No image."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.article.writer.normalize import normalize_provider_result
from newsagent_v2.article.writer.protocol import FrozenStoryPackage
from newsagent_v2.article.writer.qwen_vllm import (
    BASE_ENV,
    KEY_ENV,
    QWEN_MODEL,
    QwenVLLMWriterProvider,
    credential_presence,
    extract_usage,
    inspect_models,
    load_qwen_config,
    sanitize_error,
    strip_reasoning_from_payload,
)
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_QWEN_ARTICLE_FIRST,
    EVENT_ID,
    GROUNDING_CODES,
    HARD_MIN_WORDS,
    MODE_ARTICLE_FIRST,
    PROVIDER_QWEN_VLLM,
    QUOTE_CODES,
    SIMILARITY_CODES,
    SOURCE_BATCH_ID,
    STRUCTURE_CODES,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.credentials import load_environ
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
WINNER_DIR = FIXTURE / "QWEN_AUTONOMOUS_WINNER_CANDIDATE"
SEO_CODES = frozenset(
    {
        "meta_description_length",
        "seo_title_length",
        "slug_invalid",
        "empty_meta_description",
        "empty_seo_title",
    }
)


def _story(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }


def classify_failures(
    *,
    qa: dict[str, Any] | None,
    normalize_ok: bool,
    parse_ok: bool,
    provider_error: str | None,
) -> list[str]:
    if provider_error and not parse_ok and qa is None:
        return ["PROVIDER/API"]
    if not parse_ok:
        return ["PROVIDER/API"]
    if not normalize_ok:
        return ["NORMALIZATION"]
    codes = [str(item.get("code")) for item in (qa or {}).get("critical_failures") or [] if item.get("code")]
    warnings = [str(item.get("code")) for item in (qa or {}).get("warnings") or [] if item.get("code")]
    buckets: list[str] = []
    if any(code == "below_article_minimum_length" for code in codes):
        buckets.append("LENGTH")
    if any(code in STRUCTURE_CODES for code in codes):
        buckets.append("STRUCTURE")
    mapping = {
        "paragraph_missing_claim_ids",
        "unknown_claim_id",
        "orphan_claim",
        "claim_missing_evidence",
        "unknown_evidence_ref",
        "foreign_evidence_ref",
    }
    if any(code in mapping for code in codes):
        buckets.append("CLAIM_MAPPING")
    if any(code in GROUNDING_CODES and code not in mapping for code in codes):
        buckets.append("GROUNDING")
    if any(code in QUOTE_CODES for code in codes):
        buckets.append("QUOTE_MAPPING")
    if any(code in SIMILARITY_CODES for code in codes):
        buckets.append("SIMILARITY")
    if any(code in SEO_CODES for code in codes) or (
        not buckets and any(code in SEO_CODES for code in warnings) and not (qa or {}).get("publishable")
    ):
        buckets.append("SEO")
    if not buckets and codes:
        buckets.append("OTHER")
    return buckets


def persist_run(
    dest: Path,
    *,
    article: dict[str, Any] | None,
    qa: dict[str, Any] | None,
    score: dict[str, Any],
    native: dict[str, Any] | None,
    raw_payload: dict[str, Any] | None,
    resolver: dict[str, Any] | None,
    fixture_hashes: dict[str, Any],
    secrets: tuple[str, ...],
    winner: bool,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "QWEN_AUTONOMOUS_WINNER_CANDIDATE" if winner else "QWEN_AUTONOMOUS_FAIL",
        "autonomous_writer_pass": winner,
        "development_corrected_candidate": False,
        "writer_provider": PROVIDER_QWEN_VLLM,
        "writer_model": QWEN_MODEL,
        "event_id": EVENT_ID,
        "source_batch_id": SOURCE_BATCH_ID,
        "qa_publishable": bool((qa or {}).get("publishable")),
        "image_generated": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "make_invoked": False,
        "fixture_hashes": fixture_hashes,
    }
    write_json_utf8(dest / "manifest.json", manifest)
    if article is not None:
        write_json_utf8(dest / "article.json", article)
        (dest / "article_body.txt").write_text(str(article.get("article_body") or ""), encoding="utf-8")
        write_json_utf8(dest / "evidence_mapping.json", evidence_mapping(article))
    if qa is not None:
        write_json_utf8(dest / "qa.json", qa)
    write_json_utf8(dest / "score.json", score)
    if native is not None:
        write_json_utf8(dest / "native.json", redact_secrets(native, secrets))
    if raw_payload is not None:
        write_json_utf8(dest / "raw_payload.json", redact_secrets(raw_payload, secrets))
    if resolver is not None:
        write_json_utf8(dest / "resolver.json", resolver)
    write_json_utf8(
        dest / "telemetry.json",
        redact_secrets(
            {
                "autonomous_writer_pass": winner,
                "development_corrected_candidate": False,
                "writer_provider": PROVIDER_QWEN_VLLM,
                "writer_model": QWEN_MODEL,
                "qa_publishable": bool((qa or {}).get("publishable")),
                "live_http_calls": score.get("retries", 0) + 1 if score.get("http_status") else 1,
                "image_generated": False,
                "make_invoked": False,
            },
            secrets,
        ),
    )
    return str(dest)


def run_audition(
    *,
    environ: dict[str, str] | None = None,
    transport: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    env = environ if environ is not None else load_environ(REPO_ROOT)
    presence = credential_presence(env)
    fixture = load_fixture(FIXTURE)
    hashes = verify_fixture_hashes(fixture)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = FIXTURE / "live_runs" / stamp / "qwen_vllm_article_first"
    base_report: dict[str, Any] = {
        "qwen_api_key_present": "YES" if presence["qwen_api_key_present"] else "NO",
        "qwen_base_url_present": "YES" if presence["qwen_base_url_present"] else "NO",
        "exact_model_used": QWEN_MODEL,
        "generation_calls": 0,
        "writer_calls": 0,
        "groq_calls": 0,
        "gemini_calls": 0,
        "image_calls": 0,
        "make_invoked": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "qa_unchanged": True,
        "frozen_evidence_unchanged": bool(hashes.get("hashes_match")),
        "autonomous_writer_pass": False,
        "development_corrected_candidate": False,
        "aadi_hermes_anime_untouched": True,
        "fixture_hashes": hashes,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
    }
    if not presence["qwen_api_key_present"] or not presence["qwen_base_url_present"]:
        report = {
            **base_report,
            "ok": False,
            "stopped": True,
            "stage": "credentials",
            "endpoint_connectivity": "FAIL",
            "requested_model_available": "NO",
        }
        if persist:
            write_json_utf8(run_dir / "report.json", report)
        return report

    config = load_qwen_config(env)
    secrets = config.secrets()
    models = inspect_models(config, transport=transport)
    if not models.get("ok"):
        report = {
            **base_report,
            "ok": False,
            "stopped": True,
            "stage": "models_inspect",
            "endpoint_connectivity": "FAIL" if models.get("http_status") != 200 else "PASS",
            "requested_model_available": "YES" if models.get("requested_model_available") else "NO",
            "models_http_status": models.get("http_status"),
            "error": models.get("error"),
        }
        if persist:
            write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        return report

    story = FrozenStoryPackage.from_story(_story(fixture))
    compact = fixture["compact_writer_input"]
    enable_thinking = False if models.get("thinking_controls_advertised") else None
    provider = QwenVLLMWriterProvider(config, enable_thinking=enable_thinking)
    started = perf_counter()
    generated = provider.generate(story, compact=compact, transport=transport)
    latency_ms = int((perf_counter() - started) * 1000)
    http = generated["http"]
    parsed = generated["parsed"]
    usage = extract_usage(http.get("payload") if isinstance(http.get("payload"), dict) else None)
    raw = strip_reasoning_from_payload(http.get("payload") if isinstance(http.get("payload"), dict) else None)
    parse_ok = bool(parsed and parsed.ok and isinstance(parsed.native, dict))
    native = parsed.native if parse_ok else None

    article = None
    normalize_ok = False
    qa = None
    resolver = None
    normalize_failure = None
    if parse_ok:
        normalized = normalize_provider_result(native, story)
        normalize_ok = bool(normalized.get("ok") and isinstance(normalized.get("article"), dict))
        article = normalized.get("article") if normalize_ok else None
        normalize_failure = normalized.get("failure")
        if article is not None:
            resolved = resolve_grounding(article, fixture["article_input"])
            resolver = {
                "article_body_unchanged_by_resolver": resolved.get("article_body") == article.get("article_body"),
                "claims_unchanged_by_resolver": resolved.get("claims") == article.get("claims"),
            }
            qa = run_article_qa(deepcopy(resolved), fixture["article_input"], article_mode="normal")
            article = resolved

    score = score_result(
        provider=PROVIDER_QWEN_VLLM,
        model=QWEN_MODEL,
        mode=MODE_ARTICLE_FIRST,
        qa=qa,
        article=article,
        article_input=fixture["article_input"],
        http_status=http.get("http_status") if isinstance(http.get("http_status"), int) else None,
        latency_ms=latency_ms,
        retries=0,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        estimated_list_price_usd=None,
        provider_reported_cost_usd=None,
        candidate_id=CANDIDATE_QWEN_ARTICLE_FIRST,
        replay=False,
    )
    score["native_parse"] = "PASS" if parse_ok else "FAIL"
    score["normalization"] = "PASS" if normalize_ok else "FAIL"
    if not parse_ok or not normalize_ok:
        score["winner_eligible"] = False
        score["provider_error"] = sanitize_error(
            (parsed.error if parsed else None) or http.get("error") or str((normalize_failure or {}).get("reason") or ""),
            secrets,
        )

    publishable = bool((qa or {}).get("publishable"))
    winner = publishable and parse_ok and normalize_ok
    if persist:
        persist_run(
            run_dir,
            article=article,
            qa=qa,
            score=score,
            native=native,
            raw_payload=raw,
            resolver=resolver,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=False,
        )
    winner_path = None
    if winner and persist:
        winner_path = persist_run(
            WINNER_DIR,
            article=article,
            qa=qa,
            score=score,
            native=native,
            raw_payload=raw,
            resolver=resolver,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=True,
        )

    metrics = (qa or {}).get("metrics") or {}
    words = int(metrics.get("article_word_count") or (word_count(str((article or {}).get("article_body") or "")) if article else 0))
    target = "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING"
    classifications = [] if winner else classify_failures(
        qa=qa,
        normalize_ok=normalize_ok,
        parse_ok=parse_ok,
        provider_error=http.get("error") if isinstance(http.get("error"), str) else None,
    )
    report = {
        **base_report,
        "ok": winner,
        "stopped": True,
        "endpoint_connectivity": "PASS",
        "requested_model_available": "YES",
        "generation_calls": 1,
        "http_status": http.get("http_status"),
        "latency_ms": latency_ms,
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "total_tokens": usage["total_tokens"],
        "native_parse": "PASS" if parse_ok else "FAIL",
        "normalization": "PASS" if normalize_ok else "FAIL",
        "headline": None if article is None else article.get("headline"),
        "word_count": words,
        "hard_min_350": "PASS" if words >= HARD_MIN_WORDS else "FAIL",
        "target_450_800": target,
        "structure": score.get("structure"),
        "assertive_sentences": metrics.get("assertive_sentence_count"),
        "covered_assertions": metrics.get("claim_covered_sentence_count"),
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "grounding_criticals": score.get("grounding_criticals") or [],
        "quote_criticals": score.get("quote_criticals") or [],
        "contextual_criticals": score.get("contextual_absence_criticals") or [],
        "exact_phrase_overlap": metrics.get("exact_overlap_count"),
        "ngram_hits": metrics.get("exact_overlap_ngram_hits"),
        "max_similarity": metrics.get("max_similarity"),
        "headline_qa": score.get("headline_integrity"),
        "mechanics_qa": score.get("mechanics"),
        "seo_qa": score.get("seo"),
        "qa_publishable": "YES" if publishable else "NO",
        "critical_failures": [item.get("code") for item in (qa or {}).get("critical_failures") or []],
        "failure_classifications": classifications,
        "winner_path": winner_path,
        "autonomous_writer_pass": winner,
        "run_dir": str(run_dir),
        "http_error": None if http.get("ok") else http.get("error"),
        "normalize_failure": normalize_failure,
    }
    if persist:
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
    return report


def main() -> dict[str, Any]:
    return run_audition()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
