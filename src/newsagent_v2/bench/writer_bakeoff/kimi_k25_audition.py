"""Exactly-one authorized Kimi K2.5 writer call on frozen event-005. No /make. No image."""

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
from newsagent_v2.article.writer.bedrock_mantle import (
    FALLBACK_CALL_TOTAL,
    HTTP_RETRY_TOTAL,
    KEY_ENV,
    KIMI_MODEL,
    ONE_SHOT_AUTHORIZATION_TOKEN,
    PROVIDER_NAME,
    QUALITY_RETRY_TOTAL,
    REAL_INFERENCE_AUTHORIZED,
    REPAIR_CALL_TOTAL,
    BedrockMantleKimiWriterProvider,
    KimiCallLedger,
    credential_presence,
    extract_usage,
    load_bedrock_mantle_config,
    one_shot_remaining,
    sanitize_error,
    strip_reasoning_from_payload,
)
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.article.writer.normalize import normalize_provider_result
from newsagent_v2.article.writer.protocol import FrozenStoryPackage
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_KIMI_K25_ARTICLE_FIRST,
    EVENT_ID,
    GROUNDING_CODES,
    HARD_MIN_WORDS,
    KIMI_HARD_MAX_GENERATION_CALLS,
    MODE_ARTICLE_FIRST,
    PROVIDER_BEDROCK_MANTLE,
    QUOTE_CODES,
    SIMILARITY_CODES,
    SOURCE_BATCH_ID,
    STRUCTURE_CODES,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.credentials import load_environ
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.kimi_safety import (
    assert_frozen_fixture_only,
    live_make_can_select_kimi,
    sanitized_telemetry,
)
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
WINNER_DIR = FIXTURE / "KIMI_K2_5_BEDROCK_AUTONOMOUS_WINNER_CANDIDATE"
CONSUMED_FLAG = FIXTURE / "kimi_one_shot_consumed.json"
ONE_SHOT_TIMEOUT_SECONDS = 300
SEO_CODES = frozenset(
    {
        "meta_description_length",
        "seo_title_length",
        "slug_invalid",
        "empty_meta_description",
        "empty_seo_title",
    }
)


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


def _story(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }


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
    telemetry: dict[str, Any],
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "KIMI_K2_5_BEDROCK_AUTONOMOUS_WINNER_CANDIDATE" if winner else "KIMI_K2_5_BEDROCK_AUTONOMOUS_FAIL",
        "autonomous_writer_pass": winner,
        "development_corrected_candidate": False,
        "writer_provider": PROVIDER_BEDROCK_MANTLE,
        "writer_model": KIMI_MODEL,
        "event_id": EVENT_ID,
        "source_batch_id": SOURCE_BATCH_ID,
        "qa_publishable": bool((qa or {}).get("publishable")),
        "image_generated": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "make_invoked": False,
        "fixture_hashes": fixture_hashes,
    }
    write_json_utf8(dest / "manifest.json", redact_secrets(manifest, secrets))
    if article is not None:
        write_json_utf8(dest / "article.json", redact_secrets(article, secrets))
        (dest / "article_body.txt").write_text(str(article.get("article_body") or ""), encoding="utf-8")
        write_json_utf8(dest / "evidence_mapping.json", redact_secrets(evidence_mapping(article), secrets))
    if qa is not None:
        write_json_utf8(dest / "qa.json", redact_secrets(qa, secrets))
    write_json_utf8(dest / "score.json", redact_secrets(score, secrets))
    if native is not None:
        write_json_utf8(dest / "native.json", redact_secrets(native, secrets))
    if raw_payload is not None:
        write_json_utf8(dest / "raw_payload.json", redact_secrets(raw_payload, secrets))
    if resolver is not None:
        write_json_utf8(dest / "resolver.json", redact_secrets(resolver, secrets))
    write_json_utf8(dest / "telemetry.json", redact_secrets(telemetry, secrets))
    return str(dest)


def _mark_consumed() -> None:
    write_json_utf8(
        CONSUMED_FLAG,
        {
            "consumed": True,
            "model": KIMI_MODEL,
            "provider": PROVIDER_NAME,
            "reason": "single authorized Kimi K2.5 generation attempt",
        },
    )


def run_authorized_one_real_benchmark(
    *,
    authorization: str,
    environ: dict[str, str] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    env = environ if environ is not None else load_environ(REPO_ROOT)
    presence = credential_presence(env)
    fixture_path = assert_frozen_fixture_only()
    fixture = load_fixture(fixture_path)
    hashes = verify_fixture_hashes(fixture)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = fixture_path / "live_runs" / stamp / CANDIDATE_KIMI_K25_ARTICLE_FIRST
    base: dict[str, Any] = {
        "provider": PROVIDER_BEDROCK_MANTLE,
        "exact_model": KIMI_MODEL,
        "kimi_credential_present": "YES" if presence["bedrock_mantle_api_key_present"] else "NO",
        "hard_call_cap": KIMI_HARD_MAX_GENERATION_CALLS,
        "generation_calls": 0,
        "real_kimi_calls": 0,
        "retry_count": HTTP_RETRY_TOTAL,
        "repair_calls": REPAIR_CALL_TOTAL,
        "fallback_calls": FALLBACK_CALL_TOTAL,
        "quality_retries": QUALITY_RETRY_TOTAL,
        "groq_calls": 0,
        "gemini_calls": 0,
        "qwen_vllm_calls": 0,
        "image_calls": 0,
        "make_invoked": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "qa_unchanged": "YES",
        "frozen_evidence_unchanged": "YES" if hashes.get("hashes_match") else "NO",
        "aadi_hermes_anime_untouched": "YES",
        "real_inference_authorized_module_flag": REAL_INFERENCE_AUTHORIZED,
        "live_make_uses_kimi": "YES" if live_make_can_select_kimi() else "NO",
        "development_corrected_candidate": False,
        "fixture_hashes": hashes,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
    }
    if authorization != ONE_SHOT_AUTHORIZATION_TOKEN:
        return {**base, "ok": False, "stopped": True, "stage": "authorization", "secret_exposure_check": "PASS"}
    if CONSUMED_FLAG.is_file():
        return {
            **base,
            "ok": False,
            "stopped": True,
            "stage": "one_shot_consumed",
            "error_class": "call_cap",
            "secret_exposure_check": "PASS",
        }
    if not hashes.get("hashes_match"):
        return {**base, "ok": False, "stopped": True, "stage": "fixture", "error": "frozen evidence hash mismatch"}
    if not presence["bedrock_mantle_api_key_present"]:
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "secret_exposure_check": "PASS"}
    if REAL_INFERENCE_AUTHORIZED:
        return {**base, "ok": False, "stopped": True, "stage": "guard", "error": "module real-inference flag must stay false"}

    secrets = (str(env[KEY_ENV]).strip(),)
    _mark_consumed()
    config = load_bedrock_mantle_config(env)
    ledger = KimiCallLedger()
    provider = BedrockMantleKimiWriterProvider(config, ledger=ledger)
    story = FrozenStoryPackage.from_story(_story(fixture))
    compact = fixture["compact_writer_input"]
    started = perf_counter()
    try:
        generated = provider.generate(
            story,
            compact=compact,
            transport=None,
            timeout_seconds=ONE_SHOT_TIMEOUT_SECONDS,
            authorize_one_real_call=authorization,
        )
    except Exception as exc:
        generated = {
            "http": {
                "ok": False,
                "http_status": None,
                "payload": None,
                "error": sanitize_error(str(exc), secrets),
                "error_class": "unexpected_response",
                "retry_count": 0,
                "real_http_attempted": True,
            },
            "parsed": None,
        }
        ledger.stop("unexpected_response")
    latency_ms = int((perf_counter() - started) * 1000)
    http = generated.get("http") or {}
    parsed = generated.get("parsed")
    usage = extract_usage(http.get("payload") if isinstance(http.get("payload"), dict) else None)
    raw = strip_reasoning_from_payload(http.get("payload") if isinstance(http.get("payload"), dict) else None)
    parse_ok = bool(parsed and parsed.ok and isinstance(parsed.native, dict))
    native = parsed.native if parse_ok else None
    real_calls = 1 if http.get("real_http_attempted") else 0

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
            original_body = article.get("article_body")
            original_claims = deepcopy(article.get("claims"))
            resolved = resolve_grounding(article, fixture["article_input"])
            resolver = {
                "article_body_unchanged_by_resolver": resolved.get("article_body") == original_body,
                "claims_unchanged_by_resolver": resolved.get("claims") == original_claims,
            }
            qa = run_article_qa(deepcopy(resolved), fixture["article_input"], article_mode="normal")
            article = resolved
            if not (qa or {}).get("publishable"):
                ledger.stop("qa_failed_no_retry")

    score = score_result(
        provider=PROVIDER_BEDROCK_MANTLE,
        model=KIMI_MODEL,
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
        candidate_id=CANDIDATE_KIMI_K25_ARTICLE_FIRST,
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
    winner = bool(publishable and parse_ok and normalize_ok and article is not None)
    telemetry = sanitized_telemetry(
        {
            "provider": PROVIDER_NAME,
            "model": KIMI_MODEL,
            "http_status": http.get("http_status"),
            "latency_ms": latency_ms,
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "generation_calls": ledger.generation_calls,
            "retry_count": 0,
            "repair_calls": 0,
            "fallback_calls": 0,
            "quality_retries": 0,
            "qa_publishable": publishable,
            "stopped": True,
            "error_class": http.get("error_class"),
            "real_kimi_calls": real_calls,
            "real_http_attempted": bool(http.get("real_http_attempted")),
            "make_invoked": False,
            "image_generated": False,
            "telegram_sent": False,
            "wordpress_called": False,
        },
        secrets,
    )
    if persist:
        persist_run(
            run_dir,
            article=article,
            qa=qa,
            score=score,
            native=native,
            raw_payload=raw if isinstance(raw, dict) else None,
            resolver=resolver,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=False,
            telemetry=telemetry,
        )
    winner_path = None
    if winner and persist:
        winner_path = persist_run(
            WINNER_DIR,
            article=article,
            qa=qa,
            score=score,
            native=native,
            raw_payload=raw if isinstance(raw, dict) else None,
            resolver=resolver,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=True,
            telemetry=telemetry,
        )

    metrics = (qa or {}).get("metrics") or {}
    words = int(
        metrics.get("article_word_count")
        or (word_count(str((article or {}).get("article_body") or "")) if article else 0)
    )
    target = "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING"
    classifications = [] if winner else classify_failures(
        qa=qa,
        normalize_ok=normalize_ok,
        parse_ok=parse_ok,
        provider_error=http.get("error") if isinstance(http.get("error"), str) else None,
    )
    if http.get("http_status") not in {200, None} or http.get("error_class"):
        if not http.get("ok") and "PROVIDER/API" not in classifications:
            classifications = ["PROVIDER/API"] + classifications

    report = {
        **base,
        "ok": winner,
        "stopped": True,
        "stage": "authorized_one_shot",
        "generation_calls": ledger.generation_calls,
        "real_kimi_calls": real_calls,
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
        "provider_error": sanitize_error(str(http.get("error") or ""), secrets) if http.get("error") else None,
        "error_class": http.get("error_class"),
        "resolver": resolver,
        "one_shot_remaining_after": one_shot_remaining(),
        "real_inference_blocked_after": (not REAL_INFERENCE_AUTHORIZED) and (not one_shot_remaining()),
        "telemetry": telemetry,
        "secret_exposure_check": "PASS",
    }
    blob = json.dumps(report, default=str)
    if secrets[0] in blob or "Authorization" in blob or "Bearer " in blob:
        report["secret_exposure_check"] = "FAIL"
        report["ok"] = False
    if persist:
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        body_txt = fixture_path / "live_runs" / stamp / CANDIDATE_KIMI_K25_ARTICLE_FIRST / "article_body.txt"
        if body_txt.is_file() and secrets[0] in body_txt.read_text(encoding="utf-8"):
            report["secret_exposure_check"] = "FAIL"
    return report
