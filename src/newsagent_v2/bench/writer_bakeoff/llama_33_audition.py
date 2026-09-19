"""One-call Groq Llama 3.3 70B writer test on frozen event-005. No /make. No image."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
    EVENT_ID,
    GROQ_LLAMA_33_MODEL,
    GROUNDING_CODES,
    HARD_MIN_WORDS,
    MODE_ARTICLE_FIRST,
    PROVIDER_GROQ,
    QUOTE_CODES,
    SIMILARITY_CODES,
    SOURCE_BATCH_ID,
    STRUCTURE_CODES,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV, load_environ
from newsagent_v2.bench.writer_bakeoff.execute import execute_groq_llama_33_article_first
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
WINNER_DIR = FIXTURE / "LLAMA_3_3_70B_AUTONOMOUS_WINNER_CANDIDATE"
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
    resolver: dict[str, Any] | None,
    fixture_hashes: dict[str, Any],
    secrets: tuple[str, ...],
    winner: bool,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "LLAMA_3_3_70B_AUTONOMOUS_WINNER_CANDIDATE" if winner else "LLAMA_3_3_70B_AUTONOMOUS_FAIL",
        "autonomous_writer_pass": winner,
        "development_corrected_candidate": False,
        "writer_provider": "groq",
        "writer_model": GROQ_LLAMA_33_MODEL,
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
        {
            "autonomous_writer_pass": winner,
            "development_corrected_candidate": False,
            "writer_provider": "groq",
            "writer_model": GROQ_LLAMA_33_MODEL,
            "qa_publishable": bool((qa or {}).get("publishable")),
            "image_generated": False,
            "make_invoked": False,
        },
    )
    return str(dest)


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
    run_dir = FIXTURE / "live_runs" / stamp / CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST
    base_report: dict[str, Any] = {
        "exact_model": GROQ_LLAMA_33_MODEL,
        "chat_completions_url": GROQ_CHAT_COMPLETIONS_URL,
        "generation_calls": 0,
        "groq_calls": 0,
        "gemini_calls": 0,
        "qwen_calls": 0,
        "image_calls": 0,
        "make_invoked": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "qa_unchanged": True,
        "frozen_evidence_unchanged": bool(hashes.get("hashes_match")),
        "autonomous_writer_pass": False,
        "development_corrected_candidate": False,
        "aadi_hermes_anime_untouched": True,
        "development_winner_exposed": False,
        "fixture_hashes": hashes,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
    }
    if not hashes.get("hashes_match"):
        report = {**base_report, "ok": False, "stopped": True, "stage": "fixture", "error": "frozen evidence hash mismatch"}
        if persist:
            write_json_utf8(run_dir / "report.json", report)
        return report
    if not key_present:
        report = {
            **base_report,
            "ok": False,
            "stopped": True,
            "stage": "credentials",
            "groq_key_present": "NO",
            "failure_classifications": ["PROVIDER/API"],
        }
        if persist:
            write_json_utf8(run_dir / "report.json", report)
        return report

    secrets = (str(env[GROQ_KEY_ENV]).strip(),)
    try:
        row = execute_groq_llama_33_article_first(fixture, environ=env, http_post=http_post)
    except Exception as exc:
        report = {
            **base_report,
            "ok": False,
            "stopped": True,
            "stage": "provider",
            "groq_key_present": "YES",
            "generation_calls": 1,
            "groq_calls": 1,
            "gemini_calls": 0,
            "qwen_calls": 0,
            "native_parse": "FAIL",
            "normalization": "FAIL",
            "qa_publishable": "NO",
            "failure_classifications": ["PROVIDER/API"],
            "provider_error": str(redact_secrets(str(exc), secrets)),
        }
        if persist:
            write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        return report

    score = dict(row.get("score") or {})
    article = row.get("article") if isinstance(row.get("article"), dict) else None
    native = row.get("native") if isinstance(row.get("native"), dict) else None
    parse_ok = score.get("native_parse") == "PASS" or native is not None
    normalize_ok = score.get("normalization") == "PASS" or article is not None
    resolver = None
    qa = row.get("qa") if isinstance(row.get("qa"), dict) else None
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
        score = score_result(
            provider=PROVIDER_GROQ,
            model=GROQ_LLAMA_33_MODEL,
            mode=MODE_ARTICLE_FIRST,
            qa=qa,
            article=article,
            article_input=fixture["article_input"],
            http_status=score.get("http_status"),
            latency_ms=score.get("latency_ms"),
            retries=0,
            prompt_tokens=score.get("prompt_tokens"),
            completion_tokens=score.get("completion_tokens"),
            total_tokens=score.get("total_tokens"),
            estimated_list_price_usd=score.get("estimated_list_price_usd"),
            provider_reported_cost_usd=score.get("provider_reported_cost_usd"),
            candidate_id=CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST,
            replay=False,
        )
        score["native_parse"] = "PASS" if parse_ok else "FAIL"
        score["normalization"] = "PASS" if normalize_ok else "FAIL"

    publishable = bool((qa or {}).get("publishable"))
    winner = bool(publishable and parse_ok and normalize_ok and article is not None)
    raw = row.get("raw_payload")
    generation_calls = int(row.get("live_http_calls") or 0)
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
        )

    metrics = (qa or {}).get("metrics") or {}
    words = int(
        metrics.get("article_word_count")
        or (word_count(str((article or {}).get("article_body") or "")) if article else 0)
    )
    target = "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING"
    classifications = [] if winner else classify_failures(
        qa=qa,
        normalize_ok=bool(normalize_ok and article is not None),
        parse_ok=bool(parse_ok and native is not None) or score.get("http_status") == 200,
        provider_error=score.get("provider_error") or row.get("generation_failure"),
    )
    if score.get("http_status") not in {200, None} or row.get("generation_failure"):
        if "PROVIDER/API" not in classifications:
            classifications = ["PROVIDER/API"] + classifications
    report = {
        **base_report,
        "ok": winner,
        "stopped": True,
        "groq_key_present": "YES",
        "generation_calls": generation_calls,
        "groq_calls": generation_calls,
        "gemini_calls": 0,
        "qwen_calls": 0,
        "http_status": score.get("http_status"),
        "latency_ms": score.get("latency_ms"),
        "prompt_tokens": score.get("prompt_tokens"),
        "completion_tokens": score.get("completion_tokens"),
        "total_tokens": score.get("total_tokens"),
        "native_parse": "PASS" if native is not None else "FAIL",
        "normalization": "PASS" if article is not None else "FAIL",
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
        "provider_error": score.get("provider_error") or row.get("generation_failure"),
        "resolver": resolver,
        "model_generation_calls_by_provider": {
            "groq": generation_calls,
            "gemini": 0,
            "qwen": 0,
        },
    }
    if persist:
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
    return report


def main() -> dict[str, Any]:
    return run_audition()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
