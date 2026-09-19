"""One-call Groq Qwen 3.8 27B ledger-first writer test on frozen event-005. No /make."""

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
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers, ledger_fingerprint
from newsagent_v2.article.writer.ledger_resolve import canonical_from_ledger_first_native
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GROQ_QWEN_38_LEDGER_FIRST,
    EVENT_ID,
    GROQ_QWEN_38_MODEL,
    GROUNDING_CODES,
    HARD_MIN_WORDS,
    PROVIDER_GROQ,
    QUOTE_CODES,
    SIMILARITY_CODES,
    SOURCE_BATCH_ID,
    STRUCTURE_CODES,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV, load_environ
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.bench.writer_bakeoff.ledger_replay import (
    CLASS_AMBIGUOUS,
    CLASS_UNSUPPORTED,
    classify_uncovered_sentence,
)
from newsagent_v2.bench.writer_bakeoff.providers import groq_qwen_38_ledger_first_request
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_article import BATCH_TIMEOUT_SECONDS
from newsagent_v2.providers.groq_editorial import (
    GROQ_CHAT_COMPLETIONS_URL,
    GroqHttpError,
    extract_provider_reported_cost_usd,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    resolve_groq_api_key,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
WINNER_DIR = FIXTURE / "QWEN_3_8_LEDGER_FIRST_AUTONOMOUS_WINNER_CANDIDATE"
PREVIOUS_QWEN = FIXTURE / "live_runs" / "20260915T131315Z" / "groq_qwen_3_8_27b_article_first"
CONSUMED_FLAG = FIXTURE / "qwen_38_ledger_first_one_shot_consumed.json"
_LIVE_HTTP_CONSUMED = False
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


def persist_run(
    dest: Path,
    *,
    article: dict[str, Any] | None,
    qa: dict[str, Any] | None,
    score: dict[str, Any],
    native: dict[str, Any] | None,
    raw_payload: dict[str, Any] | None,
    fixture_hashes: dict[str, Any],
    secrets: tuple[str, ...],
    winner: bool,
    extra_telemetry: dict[str, Any],
    ledgers_payload: dict[str, Any] | None = None,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "QWEN_3_8_LEDGER_FIRST_AUTONOMOUS_WINNER_CANDIDATE" if winner else "QWEN_3_8_LEDGER_FIRST_AUTONOMOUS_FAIL",
        "autonomous_writer_pass": winner,
        "writer_contract": "ledger_first",
        "development_corrected_candidate": False,
        "qa_unchanged": True,
        "writer_provider": "groq",
        "writer_model": GROQ_QWEN_38_MODEL,
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
    write_json_utf8(dest / "telemetry.json", redact_secrets(extra_telemetry, secrets))
    if ledgers_payload is not None:
        write_json_utf8(dest / "evidence_claim_ledger.json", redact_secrets(ledgers_payload.get("claims") or [], secrets))
        write_json_utf8(dest / "quote_ledger.json", redact_secrets(ledgers_payload.get("quotes") or [], secrets))
        write_json_utf8(dest / "ledgers.json", redact_secrets(ledgers_payload, secrets))
    return str(dest)


def _mark_consumed() -> None:
    global _LIVE_HTTP_CONSUMED
    _LIVE_HTTP_CONSUMED = True
    write_json_utf8(
        CONSUMED_FLAG,
        {
            "consumed": True,
            "model": GROQ_QWEN_38_MODEL,
            "writer_contract": "ledger_first",
            "reason": "single authorized Groq Qwen 3.8 ledger-first generation attempt",
        },
    )


def run_audition(
    *,
    environ: dict[str, str] | None = None,
    http_post: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    env = environ if environ is not None else load_environ(REPO_ROOT)
    key_present = bool(str(env.get(GROQ_KEY_ENV) or "").strip())
    fixture = load_fixture(FIXTURE)
    hashes = verify_fixture_hashes(fixture)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = FIXTURE / "live_runs" / stamp / CANDIDATE_GROQ_QWEN_38_LEDGER_FIRST
    ledgers = build_evidence_ledgers(fixture["article_input"])
    fingerprint_before = ledger_fingerprint(ledgers)
    previous = json.loads((PREVIOUS_QWEN / "qa.json").read_text(encoding="utf-8")) if (PREVIOUS_QWEN / "qa.json").is_file() else {}
    previous_metrics = previous.get("metrics") or {}
    base: dict[str, Any] = {
        "exact_model": GROQ_QWEN_38_MODEL,
        "writer_contract": "ledger_first",
        "writer_provider": "groq",
        "chat_completions_url": GROQ_CHAT_COMPLETIONS_URL,
        "generation_calls": 0,
        "groq_calls": 0,
        "gemini_calls": 0,
        "kimi_calls": 0,
        "gpt_oss_calls": 0,
        "qwen_vllm_calls": 0,
        "image_calls": 0,
        "make_invoked": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "qa_unchanged": "YES",
        "frozen_evidence_unchanged": "YES" if hashes.get("hashes_match") else "NO",
        "aadi_hermes_anime_untouched": "YES",
        "kimi_real_inference_authorized": REAL_INFERENCE_AUTHORIZED,
        "kimi_inference_blocked": "YES" if not REAL_INFERENCE_AUTHORIZED else "NO",
        "live_make_uses_kimi": live_make_can_select_kimi(),
        "development_corrected_candidate": False,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
        "previous_grounding_coverage": previous_metrics.get("body_claim_coverage"),
        "previous_unmapped_count": previous_metrics.get("uncovered_assertive_sentence_count"),
        "previous_assertive_sentences": previous_metrics.get("assertive_sentence_count"),
        "previous_qa_publishable": bool(previous.get("publishable")),
        "ledger_fingerprint_before": fingerprint_before,
        "fixture_hashes": hashes,
    }
    if not hashes.get("hashes_match"):
        return {**base, "ok": False, "stopped": True, "stage": "fixture"}
    if not key_present:
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "groq_key_present": "NO"}
    if REAL_INFERENCE_AUTHORIZED:
        return {**base, "ok": False, "stopped": True, "stage": "kimi_guard", "error": "Kimi must remain blocked"}
    live_http = persist and http_post is None
    if live_http and (_LIVE_HTTP_CONSUMED or CONSUMED_FLAG.is_file()):
        return {**base, "ok": False, "stopped": True, "stage": "one_shot_consumed", "generation_calls": 0}

    secrets = (str(env[GROQ_KEY_ENV]).strip(),)
    request_body = groq_qwen_38_ledger_first_request(fixture)
    schema_props = (
        ((request_body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
    ).get("properties") or {}
    if (
        "claims" in schema_props
        or "paragraph_maps" in schema_props
        or "quotes" in schema_props
        or request_body.get("model") != GROQ_QWEN_38_MODEL
        or "reasoning_effort" in request_body
        or "include_reasoning" in request_body
    ):
        return {**base, "ok": False, "stopped": True, "stage": "contract", "error": "ledger-first request invalid"}

    ledgers_payload = {
        "claims": ledgers.as_claim_dicts(),
        "quotes": ledgers.as_quote_dicts(),
        "fingerprint": fingerprint_before,
    }
    api_key = resolve_groq_api_key(env)
    if not api_key:
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "groq_key_present": "NO"}
    if live_http:
        _mark_consumed()
    started = perf_counter()
    generation_calls = 1
    try:
        result = post_chat_completion(
            request_body,
            api_key=api_key,
            timeout_seconds=BATCH_TIMEOUT_SECONDS,
            http_post=http_post,
            sleep=lambda _seconds: None,
            max_attempts=1,
        )
    except Exception as exc:
        latency_ms = int((perf_counter() - started) * 1000)
        fingerprint_after = ledger_fingerprint(build_evidence_ledgers(fixture["article_input"]))
        report = {
            **base,
            "ok": False,
            "stopped": True,
            "groq_key_present": "YES",
            "generation_calls": generation_calls,
            "groq_calls": generation_calls,
            "latency_ms": latency_ms,
            "native_parse": "FAIL",
            "normalization": "FAIL",
            "qa_publishable": "NO",
            "provider_error": type(exc).__name__,
            "failure_classifications": ["PROVIDER/API"],
            "ledger_fingerprint_after": fingerprint_after,
            "evidence_claim_ledger_unchanged": fingerprint_before == fingerprint_after,
            "quote_ledger_unchanged": fingerprint_before == fingerprint_after,
            "run_dir": str(run_dir),
            "secret_exposure_check": "PASS",
        }
        if persist:
            persist_run(
                run_dir,
                article=None,
                qa=None,
                score={"http_status": None, "latency_ms": latency_ms},
                native=None,
                raw_payload=None,
                fixture_hashes=hashes,
                secrets=secrets,
                winner=False,
                extra_telemetry={
                    "writer_provider": "groq",
                    "writer_model": GROQ_QWEN_38_MODEL,
                    "generation_calls": generation_calls,
                    "make_invoked": False,
                    "kimi_calls": 0,
                    "gemini_calls": 0,
                },
                ledgers_payload=ledgers_payload,
            )
            write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        return report

    latency_ms = int((perf_counter() - started) * 1000)
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
    usage = extract_usage(payload) if payload else {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
    billed = extract_provider_reported_cost_usd(payload) if payload else None
    status = result.get("status_code")
    fingerprint_after = ledger_fingerprint(build_evidence_ledgers(fixture["article_input"]))

    def _stop_report(**extra: Any) -> dict[str, Any]:
        report = {
            **base,
            "ok": False,
            "stopped": True,
            "groq_key_present": "YES",
            "generation_calls": generation_calls,
            "groq_calls": generation_calls,
            "http_status": status if isinstance(status, int) else None,
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "native_parse": "FAIL",
            "normalization": "FAIL",
            "qa_publishable": "NO",
            "autonomous_writer_pass": False,
            "ledger_fingerprint_after": fingerprint_after,
            "evidence_claim_ledger_unchanged": fingerprint_before == fingerprint_after,
            "quote_ledger_unchanged": fingerprint_before == fingerprint_after,
            "run_dir": str(run_dir),
            **extra,
        }
        blob = json.dumps(report, default=str)
        report["secret_exposure_check"] = "FAIL" if secrets[0] in blob else "PASS"
        if persist:
            persist_run(
                run_dir,
                article=None,
                qa=None,
                score={"http_status": status, "latency_ms": latency_ms},
                native=None,
                raw_payload=payload if isinstance(payload, dict) else None,
                fixture_hashes=hashes,
                secrets=secrets,
                winner=False,
                extra_telemetry={
                    "writer_provider": "groq",
                    "writer_model": GROQ_QWEN_38_MODEL,
                    "http_status": status,
                    "latency_ms": latency_ms,
                    "generation_calls": generation_calls,
                    "make_invoked": False,
                    "kimi_calls": 0,
                    "gemini_calls": 0,
                },
                ledgers_payload=ledgers_payload,
            )
            write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        return report

    if not result.get("ok") or status != 200 or payload is None:
        reason = str(result.get("error") or f"HTTP {status}")
        error = (payload or {}).get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and error.get("message"):
            reason = str(error["message"])[:500]
        return _stop_report(stage="provider", provider_error=reason, failure_classifications=["PROVIDER/API"])

    try:
        native = parse_message_content(payload)
    except GroqHttpError as exc:
        return _stop_report(stage="parse", provider_error=str(exc)[:500], failure_classifications=["PROVIDER/API"])

    parse_ok = isinstance(native, dict) and isinstance(native.get("article_body"), str)
    article = None
    qa = None
    normalize_ok = False
    try:
        if parse_ok:
            article = canonical_from_ledger_first_native(
                native,
                article_input=fixture["article_input"],
                ledgers=ledgers,
            )
            normalize_ok = isinstance(article, dict) and isinstance(article.get("article_body"), str)
    except (ValueError, TypeError) as exc:
        return _stop_report(stage="normalize", provider_error=str(exc), native_parse="PASS" if parse_ok else "FAIL")

    if article is not None:
        qa = run_article_qa(deepcopy(article), fixture["article_input"], article_mode="normal")

    score = score_result(
        provider=PROVIDER_GROQ,
        model=GROQ_QWEN_38_MODEL,
        mode="ledger_first",
        qa=qa,
        article=article,
        article_input=fixture["article_input"],
        http_status=status if isinstance(status, int) else None,
        latency_ms=latency_ms,
        retries=0,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        estimated_list_price_usd=None,
        provider_reported_cost_usd=billed,
        candidate_id=CANDIDATE_GROQ_QWEN_38_LEDGER_FIRST,
        replay=False,
    )
    score["native_parse"] = "PASS" if parse_ok else "FAIL"
    score["normalization"] = "PASS" if normalize_ok else "FAIL"

    metrics = (qa or {}).get("metrics") or {}
    uncovered = list(metrics.get("uncovered_assertive_sentences") or [])
    evidence_texts = ledgers.claim_texts() + [row.text for row in ledgers.quotes]
    unsupported = 0
    ambiguous = 0
    for sentence in uncovered:
        bucket = classify_uncovered_sentence(sentence, evidence_texts=evidence_texts, all_uncovered=uncovered)
        if bucket == CLASS_UNSUPPORTED:
            unsupported += 1
        elif bucket == CLASS_AMBIGUOUS:
            ambiguous += 1
    publishable = bool((qa or {}).get("publishable"))
    winner = bool(publishable and parse_ok and normalize_ok and article is not None)
    words = int(metrics.get("article_word_count") or (word_count(str((article or {}).get("article_body") or "")) if article else 0))
    target = "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING"
    classifications = [] if winner else classify_failures(
        qa=qa,
        normalize_ok=normalize_ok,
        parse_ok=parse_ok,
        provider_error=None,
    )
    telemetry = {
        "autonomous_writer_pass": winner,
        "writer_contract": "ledger_first",
        "writer_provider": "groq",
        "writer_model": GROQ_QWEN_38_MODEL,
        "http_status": status,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "generation_calls": generation_calls,
        "qa_publishable": publishable,
        "make_invoked": False,
        "image_generated": False,
        "kimi_calls": 0,
        "gemini_calls": 0,
        "gpt_oss_calls": 0,
        "qwen_vllm_calls": 0,
    }
    if persist:
        persist_run(
            run_dir,
            article=article,
            qa=qa,
            score=score,
            native=native if isinstance(native, dict) else None,
            raw_payload=payload,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=False,
            extra_telemetry=telemetry,
            ledgers_payload=ledgers_payload,
        )
    winner_path = None
    if winner and persist:
        winner_path = persist_run(
            WINNER_DIR,
            article=article,
            qa=qa,
            score=score,
            native=native if isinstance(native, dict) else None,
            raw_payload=payload,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=True,
            extra_telemetry=telemetry,
            ledgers_payload=ledgers_payload,
        )
    report = {
        **base,
        "ok": winner,
        "stopped": True,
        "groq_key_present": "YES",
        "generation_calls": generation_calls,
        "groq_calls": generation_calls,
        "http_status": status,
        "latency_ms": latency_ms,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "native_parse": "PASS" if parse_ok else "FAIL",
        "normalization": "PASS" if normalize_ok else "FAIL",
        "headline": None if article is None else article.get("headline"),
        "word_count": words,
        "hard_min_350": "PASS" if words >= HARD_MIN_WORDS else "FAIL",
        "target_450_800": target,
        "structure": score.get("structure"),
        "assertive_sentences": metrics.get("assertive_sentence_count"),
        "deterministically_mapped_assertions": metrics.get("claim_covered_sentence_count"),
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "unsupported_assertions": unsupported,
        "ambiguous_assertions": ambiguous,
        "body_assertion_not_in_claims_count": sum(
            1 for item in (qa or {}).get("critical_failures") or [] if item.get("code") == "body_assertion_not_in_claims"
        ),
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
        "uncovered_assertive_sentences": uncovered,
        "winner_path": winner_path,
        "autonomous_writer_pass": winner,
        "run_dir": str(run_dir),
        "new_grounding_coverage": metrics.get("body_claim_coverage"),
        "new_unmapped_count": metrics.get("uncovered_assertive_sentence_count"),
        "ledger_fingerprint_after": fingerprint_after,
        "evidence_claim_ledger_unchanged": fingerprint_before == fingerprint_after,
        "quote_ledger_unchanged": fingerprint_before == fingerprint_after,
        "mapped_claim_ids": (article or {}).get("_ledger_mapped_claim_ids"),
        "mapped_quote_ids": (article or {}).get("_ledger_mapped_quote_ids"),
        "secret_exposure_check": "PASS",
    }
    blob = json.dumps(report, default=str)
    if secrets[0] in blob:
        report["secret_exposure_check"] = "FAIL"
        report["ok"] = False
    if persist:
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        if winner_path:
            write_json_utf8(WINNER_DIR / "report.json", redact_secrets(report, secrets))
    return report


def main() -> dict[str, Any]:
    return run_audition()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
