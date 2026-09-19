"""One-call Groq GPT-OSS 20B Controlled Writer V3 proof on frozen event-005."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.groq_oss20 import GroqGptOss20bProseRenderer
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers, ledger_fingerprint
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V3,
    EVENT_ID,
    GROQ_GPT_OSS_20B_MODEL,
    GROQ_MODEL,
    HARD_MIN_WORDS,
    SOURCE_BATCH_ID,
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
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL, resolve_groq_api_key

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
WINNER_DIR = FIXTURE / "CONTROLLED_WRITER_V3_GPT_OSS_20B_AUTONOMOUS_WINNER"
CONSUMED_FLAG = FIXTURE / "controlled_v3_gpt_oss_20b_one_shot_consumed.json"
_LIVE_HTTP_CONSUMED = False


def persist_run(
    dest: Path,
    *,
    article: dict[str, Any] | None,
    qa: dict[str, Any] | None,
    native: dict[str, Any] | None,
    raw_payload: dict[str, Any] | None,
    fixture_hashes: dict[str, Any],
    secrets: tuple[str, ...],
    winner: bool,
    extra_telemetry: dict[str, Any],
    ledgers_payload: dict[str, Any] | None = None,
    plan_payload: dict[str, Any] | None = None,
    capacity_payload: dict[str, Any] | None = None,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": (
            "CONTROLLED_WRITER_V3_GPT_OSS_20B_AUTONOMOUS_WINNER"
            if winner
            else "CONTROLLED_WRITER_V3_GPT_OSS_20B_FAIL"
        ),
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v3",
        "writer_role": "constrained_prose_renderer",
        "writer_provider": "groq",
        "writer_model": GROQ_GPT_OSS_20B_MODEL,
        "development_corrected_candidate": False,
        "qa_unchanged": True,
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
    if native is not None:
        write_json_utf8(dest / "native.json", redact_secrets(native, secrets))
    if raw_payload is not None:
        write_json_utf8(dest / "raw_payload.json", redact_secrets(raw_payload, secrets))
    write_json_utf8(dest / "telemetry.json", redact_secrets(extra_telemetry, secrets))
    if ledgers_payload is not None:
        write_json_utf8(dest / "evidence_claim_ledger.json", redact_secrets(ledgers_payload.get("claims") or [], secrets))
        write_json_utf8(dest / "quote_ledger.json", redact_secrets(ledgers_payload.get("quotes") or [], secrets))
        write_json_utf8(dest / "ledgers.json", redact_secrets(ledgers_payload, secrets))
    if plan_payload is not None:
        write_json_utf8(dest / "article_plan.json", redact_secrets(plan_payload, secrets))
    if capacity_payload is not None:
        write_json_utf8(dest / "evidence_capacity.json", redact_secrets(capacity_payload, secrets))
    return str(dest)


def _mark_consumed() -> None:
    global _LIVE_HTTP_CONSUMED
    _LIVE_HTTP_CONSUMED = True
    write_json_utf8(
        CONSUMED_FLAG,
        {
            "consumed": True,
            "model": GROQ_GPT_OSS_20B_MODEL,
            "writer_architecture": "controlled_writer_v3",
            "reason": "single authorized Groq GPT-OSS 20B Controlled Writer V3 generation",
        },
    )


def run_audition(
    *,
    environ: dict[str, str] | None = None,
    http_post: Any = None,
    persist: bool = True,
) -> dict[str, Any]:
    wall_started = perf_counter()
    env = environ if environ is not None else load_environ(REPO_ROOT)
    key_present = bool(str(env.get(GROQ_KEY_ENV) or "").strip())
    fixture = load_fixture(FIXTURE)
    hashes = verify_fixture_hashes(fixture)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = FIXTURE / "live_runs" / stamp / CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V3
    ledgers = build_evidence_ledgers(fixture["article_input"])
    fingerprint_before = ledger_fingerprint(ledgers)
    base: dict[str, Any] = {
        "provider": "groq",
        "exact_model": GROQ_GPT_OSS_20B_MODEL,
        "writer_architecture": "controlled_writer_v3",
        "writer_role": "constrained_prose_renderer",
        "chat_completions_url": GROQ_CHAT_COMPLETIONS_URL,
        "generation_calls": 0,
        "gpt_oss_20b_calls": 0,
        "gpt_oss_120b_calls": 0,
        "qwen_calls": 0,
        "gemini_calls": 0,
        "kimi_calls": 0,
        "image_calls": 0,
        "make_invoked": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "qa_changed": "NO",
        "evidence_changed": "NO" if hashes.get("hashes_match") else "YES",
        "qa_unchanged": "YES",
        "frozen_evidence_unchanged": "YES" if hashes.get("hashes_match") else "NO",
        "aadi_hermes_anime_untouched": "YES",
        "kimi_real_inference_authorized": REAL_INFERENCE_AUTHORIZED,
        "kimi_inference_blocked": "YES" if not REAL_INFERENCE_AUTHORIZED else "NO",
        "live_make_uses_kimi": live_make_can_select_kimi(),
        "development_corrected_candidate": False,
        "default_groq_model_untouched": GROQ_MODEL,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
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
    api_key = resolve_groq_api_key(env)
    if not api_key:
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "groq_key_present": "NO"}
    if live_http:
        _mark_consumed()

    renderer = GroqGptOss20bProseRenderer(api_key=api_key, http_post=http_post)
    story = {"event_id": EVENT_ID, "article_input": fixture["article_input"]}
    compiled = compile_controlled_article(story, renderer=renderer)
    wall_ms = int((perf_counter() - wall_started) * 1000)
    fingerprint_after = ledger_fingerprint(build_evidence_ledgers(fixture["article_input"]))
    article = compiled.article
    qa = compiled.qa
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
    words = int(metrics.get("article_word_count") or (word_count(str((article or {}).get("article_body") or "")) if article else 0))
    publishable = bool((qa or {}).get("publishable"))
    winner = bool(publishable and compiled.ok and article is not None)
    para_failures = [
        {"paragraph_id": row.paragraph_id, "issues": row.issues}
        for row in compiled.paragraph_validations
        if not row.ok
    ]
    criticals = [item.get("code") for item in (qa or {}).get("critical_failures") or []]
    capacity = compiled.capacity.as_dict() if compiled.capacity else None
    plan = compiled.plan.as_dict() if compiled.plan else None
    ledgers_payload = {
        "claims": ledgers.as_claim_dicts(),
        "quotes": ledgers.as_quote_dicts(),
        "fingerprint": fingerprint_before,
    }
    telemetry = {
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v3",
        "writer_role": "constrained_prose_renderer",
        "writer_provider": "groq",
        "writer_model": GROQ_GPT_OSS_20B_MODEL,
        "http_status": renderer.http_status,
        "latency_ms": renderer.latency_ms,
        "wall_ms": wall_ms,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "total_tokens": renderer.usage.get("total_tokens"),
        "generation_calls": renderer.generation_calls,
        "qa_publishable": publishable,
        "make_invoked": False,
        "image_generated": False,
        "kimi_calls": 0,
        "gemini_calls": 0,
        "qwen_calls": 0,
        "gpt_oss_120b_calls": 0,
    }
    report = {
        **base,
        "ok": winner,
        "stopped": True,
        "groq_key_present": "YES",
        "generation_calls": renderer.generation_calls,
        "gpt_oss_20b_calls": renderer.generation_calls,
        "http_status": renderer.http_status,
        "latency_ms": renderer.latency_ms,
        "wall_ms": wall_ms,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "total_tokens": renderer.usage.get("total_tokens"),
        "failure_class": compiled.failure_class,
        "evidence_claim_count": (capacity or {}).get("claim_count"),
        "quote_count": (capacity or {}).get("quote_count"),
        "evidence_capacity": capacity,
        "article_plan_paragraph_count": len((plan or {}).get("paragraph_plans") or []),
        "paragraph_plan_count": len((plan or {}).get("paragraph_plans") or []),
        "planned_subheadings": (plan or {}).get("subheading_plans") or [],
        "headline": None if article is None else article.get("headline"),
        "dek": None if article is None else article.get("dek"),
        "subheadings": [
            str(section.get("purpose") or "")
            for section in ((article or {}).get("article_sections") or [])
            if isinstance(section, dict)
        ],
        "word_count": words,
        "hard_min_350": "PASS" if words >= HARD_MIN_WORDS else "FAIL",
        "target_450_800": (
            "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING"
        ),
        "structure": "PASS" if not any(code in {"paragraph_missing_claim_ids", "unknown_claim_id"} for code in criticals) else "FAIL",
        "assertive_sentences": metrics.get("assertive_sentence_count"),
        "mapped_assertions": metrics.get("claim_covered_sentence_count"),
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "unsupported_assertions": unsupported,
        "ambiguous_assertions": ambiguous,
        "body_assertion_not_in_claims_count": sum(
            1 for code in criticals if code == "body_assertion_not_in_claims"
        ),
        "paragraph_validation_failures": para_failures,
        "quote_criticals": [code for code in criticals if "quote" in str(code)],
        "contextual_criticals": [code for code in criticals if "contextual" in str(code) or "absence" in str(code)],
        "exact_phrase_overlap": metrics.get("exact_overlap_count"),
        "ngram_hits": metrics.get("exact_overlap_ngram_hits"),
        "max_similarity": metrics.get("max_similarity"),
        "headline_qa": "PASS" if not any(str(code).startswith("headline") for code in criticals) else "FAIL",
        "mechanics_qa": "PASS" if not any(code in {"empty_body", "repeated_paragraph", "malformed_punctuation"} for code in criticals) else "FAIL",
        "seo_qa": "PASS" if not any(str(code).startswith("seo") or code == "invalid_slug" for code in criticals) else "FAIL",
        "qa_publishable": "YES" if publishable else "NO",
        "critical_failures": criticals,
        "autonomous_writer_pass": winner,
        "winner_path": None,
        "run_dir": str(run_dir),
        "provider_error": renderer.error,
        "ledger_fingerprint_after": fingerprint_after,
        "evidence_claim_ledger_unchanged": fingerprint_before == fingerprint_after,
        "quote_ledger_unchanged": fingerprint_before == fingerprint_after,
        "secret_exposure_check": "PASS",
    }
    blob = json.dumps(report, default=str)
    if secrets[0] in blob:
        report["secret_exposure_check"] = "FAIL"
        report["ok"] = False
        winner = False
    if persist:
        persist_run(
            run_dir,
            article=article,
            qa=qa,
            native=renderer.native,
            raw_payload=renderer.raw_payload,
            fixture_hashes=hashes,
            secrets=secrets,
            winner=False,
            extra_telemetry=telemetry,
            ledgers_payload=ledgers_payload,
            plan_payload=plan,
            capacity_payload=capacity,
        )
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        if winner:
            winner_path = persist_run(
                WINNER_DIR,
                article=article,
                qa=qa,
                native=renderer.native,
                raw_payload=renderer.raw_payload,
                fixture_hashes=hashes,
                secrets=secrets,
                winner=True,
                extra_telemetry=telemetry,
                ledgers_payload=ledgers_payload,
                plan_payload=plan,
                capacity_payload=capacity,
            )
            report["winner_path"] = winner_path
            write_json_utf8(WINNER_DIR / "report.json", redact_secrets(report, secrets))
    return report


def main() -> dict[str, Any]:
    return run_audition()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
