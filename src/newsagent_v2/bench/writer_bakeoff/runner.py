"""Writer bake-off runner. Default dry-run makes zero live calls."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.writer.normalize import normalize_provider_result
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.article.writer.schema import (
    failing_gemini_schema_from_groq_article_contract,
    gemini_article_first_schema,
    schema_contains_additional_properties,
)
from newsagent_v2.article.writer.validate import validate_gemini_generate_content_body
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY, WARN_SENTENCE_SIMILARITY
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GEMINI_36_ARTICLE_FIRST,
    CANDIDATE_GEMINI_ARTICLE_FIRST,
    CANDIDATE_GEMINI_STRUCTURED,
    CANDIDATE_GROQ_ARTICLE_FIRST,
    CANDIDATE_GROQ_STRUCTURED,
    EVENT_ID,
    GEMINI_NEXT_TEXT_MODEL,
    GEMINI_TEXT_MODEL,
    GROQ_MODEL,
    PROVIDER_GEMINI,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.credentials import (
    GEMINI_KEY_ENV,
    GROQ_KEY_ENV,
    OPENROUTER_KEY_ENV,
    credential_flags,
)
from newsagent_v2.bench.writer_bakeoff.execute import (
    execute_gemini_36_article_first,
    execute_gemini_article_first,
    execute_groq_article_first,
)
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.freeze import sha256_payload
from newsagent_v2.bench.writer_bakeoff.providers import (
    estimated_live_calls,
    gemini_article_first_request,
    next_writer_candidate,
    planned_candidates,
    replay_groq_structured,
    request_diagnostics_for_plan,
)
from newsagent_v2.image.providers.gemini import generate_content_url
from newsagent_v2.bench.writer_bakeoff.score import pick_winner, score_result
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS

REPO_ROOT = Path(__file__).resolve().parents[4]
EXPECTED_LIVE_CALLS = 2
EXPECTED_ONE_LIVE_CALL = 1

CASE_TEXT = {
    "A": "Groq article-first passes while Groq structured fails → current structured generation contract is primary problem.",
    "B": "Gemini passes in both modes while Groq fails → writer/model is primary problem.",
    "C": "Gemini article-first passes but Gemini structured fails → structured contract is primary problem across models.",
    "D": "Both article-first modes pass, structured modes fail → article-first generation + post-generation grounding normalization.",
    "E": "Gemini structured passes → current canonical architecture is viable; replace writer adapter.",
    "F": "Nobody passes → inspect shared writer/normalization/QA contract. Do not blame providers automatically.",
}


def dry_run(
    fixture_path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    fixture = load_fixture(fixture_path)
    flags = credential_flags(environ)
    gemini_configured = bool(flags.get(GEMINI_KEY_ENV))
    groq_configured = bool(flags.get(GROQ_KEY_ENV))
    candidates = planned_candidates()
    diagnostics = request_diagnostics_for_plan(fixture)
    baseline = replay_groq_structured(fixture)
    hash_check = verify_fixture_hashes(fixture)
    gemini_body = next(
        row for row in diagnostics if row["candidate_id"] == CANDIDATE_GEMINI_ARTICLE_FIRST
    )
    gemini_validation = (gemini_body.get("diagnostics") or {}).get("schema_validation") or {}
    failing_schema = failing_gemini_schema_from_groq_article_contract()
    native_sample = sample_article_first_native(
        event_id=fixture["manifest"]["event_id"],
        evidence_id="event-005-e01",
    )
    groq_norm = normalize_provider_result(
        native_sample,
        {
            "event_id": fixture["manifest"]["event_id"],
            "article_input": fixture["article_input"],
        },
    )
    return {
        "ok": bool(gemini_validation.get("ok") and groq_norm.get("ok") and hash_check.get("hashes_match")),
        "dry_run": True,
        "live_http_calls": 0,
        "image_calls": 0,
        "telegram_calls": 0,
        "wordpress_calls": 0,
        "make_invoked": False,
        "repair_calls": 0,
        "fixture": str(fixture_path),
        "event_id": fixture["manifest"]["event_id"],
        "source_batch_id": fixture["manifest"]["source_batch_id"],
        "fixture_confirmation": hash_check,
        "providers_models_that_would_run": candidates,
        "modes": sorted({row["mode"] for row in candidates}),
        "request_count_if_live": estimated_live_calls(),
        "expected_next_live_calls": 2,
        "structured_current_rerun": False,
        "credentials_configured": {
            GROQ_KEY_ENV: groq_configured,
            GEMINI_KEY_ENV: gemini_configured,
        },
        "estimated_admission": {
            row["candidate_id"]: (row.get("diagnostics") or {}).get("estimated_admission_tokens")
            for row in diagnostics
        },
        "output_caps": {
            "groq_max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
            "gemini_max_output_tokens": BATCH_MAX_COMPLETION_TOKENS,
            "groq_model": GROQ_MODEL,
            "gemini_text_model": GEMINI_TEXT_MODEL,
        },
        "qa_configuration": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
            "evidence_min_words": MIN_EXTRACTED_WORDS,
            "evidence_min_facts": MIN_DISTINCT_FACTS,
            "exact_phrase_n": EXACT_PHRASE_N,
            "critical_similarity": HIGH_SENTENCE_SIMILARITY,
            "warning_similarity": WARN_SENTENCE_SIMILARITY,
            "unchanged": True,
        },
        "gemini_request_validation": {
            "ok": bool(gemini_validation.get("ok")),
            "has_additionalProperties": bool(gemini_validation.get("has_additionalProperties")),
            "violations": gemini_validation.get("violations") or [],
            "previous_400_schema_had_additionalProperties": schema_contains_additional_properties(
                failing_schema
            ),
            "current_schema_has_additionalProperties": schema_contains_additional_properties(
                gemini_article_first_schema()
            ),
        },
        "groq_normalization_validation": {
            "ok": bool(groq_norm.get("ok")),
            "code": (groq_norm.get("failure") or {}).get("code"),
            "canonical_claim_ids": [
                claim.get("claim_id")
                for claim in ((groq_norm.get("article") or {}).get("claims") or [])
            ],
            "provider_used_id": True,
        },
        "historical_groq_structured_baseline": baseline,
        "winner_if_only_baseline": pick_winner([baseline]),
        "request_diagnostics": diagnostics,
    }


def refuse_live() -> dict[str, Any]:
    return {
        "ok": False,
        "dry_run": False,
        "refused": True,
        "reason": "article-first adapter live calls are not authorized in this session",
        "live_http_calls": 0,
    }


def verify_fixture_hashes(fixture: dict[str, Any]) -> dict[str, Any]:
    manifest = fixture["manifest"]
    expected = manifest.get("hashes") or {}
    actual = {
        "article_input_sha256": sha256_payload(fixture["article_input"]),
        "compact_writer_input_sha256": sha256_payload(fixture["compact_writer_input"]),
        "baseline_article_sha256": sha256_payload(fixture["baseline_article"]),
    }
    matched = actual == {
        "article_input_sha256": expected.get("article_input_sha256"),
        "compact_writer_input_sha256": expected.get("compact_writer_input_sha256"),
        "baseline_article_sha256": expected.get("baseline_article_sha256"),
    }
    return {
        "source_batch_id": manifest.get("source_batch_id"),
        "expected_source_batch_id": SOURCE_BATCH_ID,
        "event_id": manifest.get("event_id"),
        "expected_event_id": EVENT_ID,
        "refetch": False,
        "rediscover": False,
        "hashes_match": matched,
        "expected": expected,
        "actual": actual,
    }


def evidence_mapping(article: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(article, dict):
        return {"claims": [], "paragraphs": []}
    claims = []
    for claim in article.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        refs = claim.get("evidence_refs") if isinstance(claim.get("evidence_refs"), list) else []
        evidence_ids = claim.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            evidence_ids = [
                str(row.get("evidence_id"))
                for row in refs
                if isinstance(row, dict) and row.get("evidence_id")
            ]
        claims.append(
            {
                "claim_id": claim.get("claim_id") or claim.get("id"),
                "text": claim.get("text"),
                "claim_type": claim.get("claim_type"),
                "evidence_ids": evidence_ids,
            }
        )
    paragraphs = []
    for section in article.get("article_sections") or []:
        if not isinstance(section, dict):
            continue
        for para in section.get("paragraphs") or []:
            if not isinstance(para, dict):
                continue
            paragraphs.append(
                {
                    "section_id": section.get("id"),
                    "text": para.get("text"),
                    "claim_ids": para.get("claim_ids") or [],
                }
            )
    return {"claims": claims, "paragraphs": paragraphs}


def diagnose_architecture(scores: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {row["candidate_id"]: row for row in scores}

    def ok(candidate_id: str) -> bool:
        return bool((by_id.get(candidate_id) or {}).get("winner_eligible"))

    groq_s = ok(CANDIDATE_GROQ_STRUCTURED)
    groq_af = ok(CANDIDATE_GROQ_ARTICLE_FIRST)
    gem_s = ok(CANDIDATE_GEMINI_STRUCTURED)
    gem_af = ok(CANDIDATE_GEMINI_ARTICLE_FIRST)
    matching: list[str] = []
    if not any((groq_s, groq_af, gem_s, gem_af)):
        matching.append("F")
    if groq_af and not groq_s:
        matching.append("A")
    if gem_s and gem_af and not groq_s and not groq_af:
        matching.append("B")
    if gem_af and not gem_s:
        matching.append("C")
    if groq_af and gem_af and not groq_s and not gem_s:
        matching.append("D")
    if gem_s:
        matching.append("E")
    if "F" in matching:
        primary = "F"
    elif "D" in matching:
        primary = "D"
    elif "B" in matching:
        primary = "B"
    elif "E" in matching and not groq_s:
        primary = "E"
    elif "A" in matching:
        primary = "A"
    elif "C" in matching:
        primary = "C"
    elif "E" in matching:
        primary = "E"
    else:
        primary = "none"
    groq_status = "remove_as_primary"
    gemini_status = "not_selected"
    architecture = "inspect_shared_contract"
    if primary == "D":
        architecture = "article_first_plus_grounding_normalization"
        groq_status = "fallback_if_article_first_eligible" if groq_af else "remove_as_primary"
        gemini_status = "primary_if_article_first_eligible" if gem_af else "not_selected"
    elif primary == "B":
        architecture = "keep_canonical_structured_replace_writer_with_gemini"
        groq_status = "remove_as_primary"
        gemini_status = "primary"
    elif primary == "E":
        architecture = "keep_canonical_structured_replace_writer_adapter"
        groq_status = "fallback" if groq_af or groq_s else "remove_as_primary"
        gemini_status = "primary"
    elif primary == "A":
        architecture = "keep_groq_switch_to_article_first"
        groq_status = "primary_article_first"
        gemini_status = "fallback" if gem_af or gem_s else "not_selected"
    elif primary == "C":
        architecture = "article_first_across_models"
        groq_status = "fallback_if_article_first_eligible" if groq_af else "remove_as_primary"
        gemini_status = "primary_article_first"
    elif primary == "F":
        architecture = "inspect_shared_writer_normalization_qa_contract"
        groq_status = "do_not_upgrade_tier"
        gemini_status = "not_proven"
    return {
        "primary_case": primary,
        "matching_cases": matching,
        "explanation": CASE_TEXT.get(primary, "mixed or incomplete eligibility"),
        "recommended_production_writer_architecture": architecture,
        "groq_role": groq_status,
        "gemini_role": gemini_status,
        "eligibility": {
            CANDIDATE_GROQ_STRUCTURED: groq_s,
            CANDIDATE_GROQ_ARTICLE_FIRST: groq_af,
            CANDIDATE_GEMINI_STRUCTURED: gem_s,
            CANDIDATE_GEMINI_ARTICLE_FIRST: gem_af,
        },
    }


def _sum_numeric(rows: list[dict[str, Any]], field: str) -> int | float | None:
    values = [row.get(field) for row in rows if isinstance(row.get(field), (int, float))]
    if not values:
        return None
    total = sum(values)
    return int(total) if all(isinstance(value, int) and not isinstance(value, bool) for value in values) else round(float(total), 8)


def _persist_candidate(dest: Path, row: dict[str, Any]) -> None:
    write_json_utf8(dest / "score.json", row["score"])
    if row.get("qa") is not None:
        write_json_utf8(dest / "qa.json", row["qa"])
    if row.get("article") is not None:
        write_json_utf8(dest / "article.json", row["article"])
        write_json_utf8(dest / "evidence_mapping.json", evidence_mapping(row["article"]))
    if row.get("raw_payload") is not None:
        write_json_utf8(dest / "raw_payload.json", row["raw_payload"])
    if row.get("native") is not None:
        write_json_utf8(dest / "native.json", row["native"])
    write_json_utf8(
        dest / "telemetry.json",
        {
            "candidate_id": row["score"]["candidate_id"],
            "provider": row["score"]["provider"],
            "model": row["score"]["model"],
            "mode": row["score"]["mode"],
            "replay": row["score"]["replay"],
            "http_status": row["score"]["http_status"],
            "latency_ms": row["score"]["latency_ms"],
            "retries": row["score"]["retries"],
            "prompt_tokens": row["score"]["prompt_tokens"],
            "completion_tokens": row["score"]["completion_tokens"],
            "total_tokens": row["score"]["total_tokens"],
            "estimated_list_price_usd": row["score"]["estimated_list_price_usd"],
            "provider_reported_cost_usd": row["score"]["provider_reported_cost_usd"],
            "generation_failure": row.get("generation_failure"),
            "normalized_from": row.get("normalized_from"),
            "live_http_calls": row.get("live_http_calls") or 0,
        },
    )


def persist_winner_candidate(*, fixture_root: Path, winner_id: str, row: dict[str, Any], run_dir: Path, fixture_hashes: dict[str, Any] | None = None) -> str:
    dest = fixture_root / "WINNER_CANDIDATE"
    write_json_utf8(
        dest / "manifest.json",
        {
            "status": "WINNER_CANDIDATE",
            "image_generated": False,
            "telegram_sent": False,
            "wordpress_called": False,
            "make_invoked": False,
            "candidate_id": winner_id,
            "provider": row["score"]["provider"],
            "model": row["score"]["model"],
            "mode": row["score"]["mode"],
            "source_run": str(run_dir),
            "event_id": EVENT_ID,
            "source_batch_id": SOURCE_BATCH_ID,
            "fixture_hashes": fixture_hashes or {},
        },
    )
    write_json_utf8(dest / "article.json", row["article"])
    write_json_utf8(dest / "normalized.json", row["article"])
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "article_body.txt").write_text(str(row["article"].get("article_body") or ""), encoding="utf-8")
    write_json_utf8(dest / "qa.json", row["qa"])
    write_json_utf8(dest / "score.json", row["score"])
    write_json_utf8(dest / "evidence_mapping.json", evidence_mapping(row["article"]))
    if row.get("native") is not None:
        write_json_utf8(dest / "native.json", row["native"])
    if row.get("raw_payload") is not None:
        write_json_utf8(dest / "provider_native_response.json", row["raw_payload"])
    write_json_utf8(
        dest / "telemetry.json",
        {
            "provider": row["score"]["provider"],
            "model": row["score"]["model"],
            "mode": row["score"]["mode"],
            "http_status": row["score"]["http_status"],
            "latency_ms": row["score"]["latency_ms"],
            "retries": row["score"]["retries"],
            "prompt_tokens": row["score"]["prompt_tokens"],
            "completion_tokens": row["score"]["completion_tokens"],
            "total_tokens": row["score"]["total_tokens"],
            "estimated_list_price_usd": row["score"]["estimated_list_price_usd"],
            "provider_reported_cost_usd": row["score"]["provider_reported_cost_usd"],
            "qa_publishable": row["score"]["qa_publishable"],
            "native_parse": row["score"].get("native_parse"),
            "normalization": row["score"].get("normalization"),
        },
    )
    return str(dest)


def refuse_groq_retest() -> dict[str, Any]:
    return {
        "ok": False,
        "dry_run": False,
        "refused": True,
        "reason": "Groq testing is closed. Use --live-one for the next writer candidate.",
        "live_http_calls": 0,
        "image_calls": 0,
        "telegram_calls": 0,
        "wordpress_calls": 0,
        "make_invoked": False,
        "selected_model": GEMINI_NEXT_TEXT_MODEL,
    }


def classify_qa_failure(
    *,
    score: dict[str, Any],
    qa: dict[str, Any] | None,
    article: dict[str, Any] | None,
) -> dict[str, Any]:
    del qa
    uncovered = list(score.get("uncovered_assertive_sentences") or [])
    sentences: list[str] = []
    for row in uncovered:
        if isinstance(row, str) and row.strip():
            sentences.append(row.strip())
        elif isinstance(row, dict):
            text = row.get("sentence") or row.get("text")
            if text:
                sentences.append(str(text).strip())
    quote_codes = list(score.get("quote_criticals") or [])
    ground_codes = list(score.get("grounding_criticals") or [])
    contextual = list(score.get("contextual_absence_criticals") or [])
    words = int(score.get("rendered_word_count") or 0)
    parse = score.get("native_parse")
    normalize = score.get("normalization")
    http = score.get("http_status")
    if http != 200:
        return {
            "classification": None,
            "reason": "not_http_200_qa_failure",
            "http_status": http,
            "sentences": sentences,
            "provider_error": score.get("provider_error"),
        }
    if parse == "FAIL":
        return {
            "classification": "writer grounding failure",
            "reason": "native_json_parse_failed_after_http_200",
            "sentences": sentences,
            "provider_error": score.get("provider_error"),
        }
    if normalize == "FAIL":
        return {
            "classification": "canonical normalizer problem",
            "reason": "native_parse_passed_but_canonical_normalization_failed",
            "sentences": sentences,
            "provider_error": score.get("provider_error"),
        }
    claim_texts = []
    if isinstance(article, dict):
        for claim in article.get("claims") or []:
            if isinstance(claim, dict) and claim.get("text"):
                claim_texts.append(str(claim["text"]))
    matcher_hits = []
    for sent in sentences:
        lowered = sent.lower()
        for claim in claim_texts:
            claim_l = claim.lower()
            if claim_l and (claim_l in lowered or lowered in claim_l):
                matcher_hits.append({"sentence": sent, "claim": claim})
                break
    if quote_codes and not ground_codes and not contextual:
        return {
            "classification": "quote failure",
            "reason": "quote_criticals_without_grounding_criticals",
            "quote_criticals": quote_codes,
            "sentences": sentences[:12],
        }
    if words < 350 and not ground_codes and not quote_codes and not contextual:
        return {
            "classification": "insufficient evidence for required article depth",
            "reason": "hard_length_fail_without_grounding_or_quote_criticals",
            "rendered_word_count": words,
            "sentences": sentences[:12],
        }
    if matcher_hits and sentences and len(matcher_hits) >= max(1, (len(sentences) + 1) // 2):
        return {
            "classification": "QA false positive / matcher problem",
            "reason": "uncovered_sentences_overlap_claim_text",
            "sentences": sentences[:12],
            "matcher_hits": matcher_hits[:8],
        }
    if ground_codes or contextual or sentences:
        return {
            "classification": "writer grounding failure",
            "reason": "assertive_body_not_represented_by_mapped_claims_or_ungrounded_context",
            "grounding_criticals": ground_codes,
            "contextual_absence_criticals": contextual,
            "claim_coverage": score.get("claim_coverage"),
            "sentences": sentences[:12],
        }
    if quote_codes:
        return {
            "classification": "quote failure",
            "reason": "quote_criticals",
            "quote_criticals": quote_codes,
            "sentences": sentences[:12],
        }
    if words < 350:
        return {
            "classification": "insufficient evidence for required article depth",
            "reason": "below_hard_minimum_words",
            "rendered_word_count": words,
            "sentences": sentences[:12],
        }
    return {
        "classification": "writer grounding failure",
        "reason": "qa_not_publishable_after_http_200",
        "structure": score.get("structure"),
        "sentences": sentences[:12],
    }


def dry_run_one(
    fixture_path: Path,
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    fixture = load_fixture(fixture_path)
    flags = credential_flags(environ)
    hash_check = verify_fixture_hashes(fixture)
    candidate = next_writer_candidate()
    body = gemini_article_first_request(fixture)
    schema_check = validate_gemini_generate_content_body(body)
    url = generate_content_url(GEMINI_NEXT_TEXT_MODEL)
    hashes_ok = bool(hash_check["hashes_match"] and hash_check["source_batch_id"] == SOURCE_BATCH_ID)
    schema_ok = bool(schema_check.get("ok")) and not schema_check.get("has_additionalProperties")
    return {
        "ok": hashes_ok and schema_ok,
        "dry_run": True,
        "selected_provider": PROVIDER_GEMINI,
        "selected_model": GEMINI_NEXT_TEXT_MODEL,
        "candidate_id": CANDIDATE_GEMINI_36_ARTICLE_FIRST,
        "selection_reason": candidate["selection_reason"],
        "credentials_configured": {
            GEMINI_KEY_ENV: bool(flags.get(GEMINI_KEY_ENV)),
            GROQ_KEY_ENV: bool(flags.get(GROQ_KEY_ENV)),
            OPENROUTER_KEY_ENV: bool(flags.get(OPENROUTER_KEY_ENV)),
            "openrouter_used_by_v2_article_writer": False,
        },
        "schema_validation": schema_check,
        "generate_content_url": url,
        "fixture_confirmation": hash_check,
        "evidence_refetched": False,
        "evidence_rediscovered": False,
        "frozen_fixture_modified": False,
        "live_http_calls": 0,
        "expected_live_calls": EXPECTED_ONE_LIVE_CALL,
        "image_calls": 0,
        "telegram_calls": 0,
        "wordpress_calls": 0,
        "make_invoked": False,
        "live_pipeline_changed": False,
        "qa_configuration": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
            "evidence_min_words": MIN_EXTRACTED_WORDS,
            "evidence_min_facts": MIN_DISTINCT_FACTS,
            "exact_phrase_n": EXACT_PHRASE_N,
            "critical_similarity": HIGH_SENTENCE_SIMILARITY,
            "warning_similarity": WARN_SENTENCE_SIMILARITY,
            "unchanged": True,
        },
        "repair_calls": 0,
        "groq_called": False,
    }


def run_one_writer(
    fixture_path: Path,
    *,
    environ: dict[str, str],
    gemini_transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    prepared = dry_run_one(fixture_path, environ=environ)
    fixture = load_fixture(fixture_path)
    flags = credential_flags(environ)
    gemini_configured = bool(flags.get(GEMINI_KEY_ENV))
    hash_check = prepared["fixture_confirmation"]
    base = {
        "dry_run": False,
        "fixture": str(fixture_path),
        "event_id": EVENT_ID,
        "source_batch_id": SOURCE_BATCH_ID,
        "selected_provider": PROVIDER_GEMINI,
        "selected_model": GEMINI_NEXT_TEXT_MODEL,
        "candidate_id": CANDIDATE_GEMINI_36_ARTICLE_FIRST,
        "selection_reason": prepared["selection_reason"],
        "fixture_confirmation": hash_check,
        "evidence_refetched": False,
        "evidence_rediscovered": False,
        "frozen_fixture_modified": False,
        "image_calls": 0,
        "telegram_calls": 0,
        "wordpress_calls": 0,
        "make_invoked": False,
        "live_pipeline_changed": False,
        "groq_called": False,
        "repair_calls": 0,
        "expected_live_calls": EXPECTED_ONE_LIVE_CALL,
        "qa_configuration": prepared["qa_configuration"],
        "schema_validation": prepared["schema_validation"],
        "credentials_configured": prepared["credentials_configured"],
        "winner_candidate_path": None,
        "failure_classification": None,
    }
    if not hash_check["hashes_match"] or hash_check["source_batch_id"] != SOURCE_BATCH_ID:
        return {
            **base,
            "ok": False,
            "refused": True,
            "reason": "fixture hash or source batch mismatch; refusing live call",
            "live_http_calls": 0,
        }
    schema_check = prepared["schema_validation"]
    if not schema_check.get("ok") or schema_check.get("has_additionalProperties"):
        return {
            **base,
            "ok": False,
            "refused": True,
            "reason": "gemini schema validation failed; refusing live call",
            "live_http_calls": 0,
        }
    if not gemini_configured:
        return {
            **base,
            "ok": False,
            "refused": True,
            "reason": "NEWSAGENT_V2_GEMINI_API_KEY is not configured",
            "live_http_calls": 0,
        }

    secrets = tuple(
        str(environ.get(name) or "").strip()
        for name in (GEMINI_KEY_ENV,)
        if str(environ.get(name) or "").strip()
    )
    wall_started = perf_counter()
    try:
        row = execute_gemini_36_article_first(fixture, environ=environ, transport=gemini_transport)
    except Exception as exc:
        row = {
            "score": score_from_exception(
                fixture,
                candidate_id=CANDIDATE_GEMINI_36_ARTICLE_FIRST,
                provider=PROVIDER_GEMINI,
                model=GEMINI_NEXT_TEXT_MODEL,
                mode="article_first_grounding_map",
                reason=str(redact_secrets(str(exc), secrets)),
            ),
            "article": None,
            "qa": None,
            "normalized_from": None,
            "generation_failure": str(redact_secrets(str(exc), secrets)),
            "raw_payload": None,
            "live_http_calls": 1,
        }
    wall_ms = int((perf_counter() - wall_started) * 1000)
    score = row["score"]
    winner = pick_winner([score])
    live_http_calls = int(row.get("live_http_calls") or 0)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(fixture_path) / "live_runs" / run_id
    _persist_candidate(run_dir / score["candidate_id"], row)
    winner_path = None
    if winner.get("winner") and row.get("article") is not None and row.get("qa") is not None:
        winner_path = persist_winner_candidate(
            fixture_root=Path(fixture_path),
            winner_id=str(winner["winner"]),
            row=row,
            run_dir=run_dir,
            fixture_hashes=hash_check,
        )
    classification = None
    if score.get("http_status") == 200 and not score.get("qa_publishable"):
        classification = classify_qa_failure(score=score, qa=row.get("qa"), article=row.get("article"))
    summary = {
        **base,
        "ok": live_http_calls == EXPECTED_ONE_LIVE_CALL,
        "refused": False,
        "live_http_calls": live_http_calls,
        "score": score,
        "winner": winner,
        "winner_candidate_path": winner_path,
        "failure_classification": classification,
        "run_dir": str(run_dir),
        "wall_clock_ms": wall_ms,
        "generation_failure": row.get("generation_failure"),
        "qa_publishable": score.get("qa_publishable"),
    }
    write_json_utf8(run_dir / "summary.json", summary)
    return summary


def run_live(
    fixture_path: Path,
    *,
    environ: dict[str, str],
    groq_http_post: Callable[..., Any] | None = None,
    gemini_transport: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    fixture = load_fixture(fixture_path)
    flags = credential_flags(environ)
    groq_configured = bool(flags.get(GROQ_KEY_ENV))
    gemini_configured = bool(flags.get(GEMINI_KEY_ENV))
    hash_check = verify_fixture_hashes(fixture)
    if not hash_check["hashes_match"] or hash_check["source_batch_id"] != SOURCE_BATCH_ID:
        return {
            "ok": False,
            "dry_run": False,
            "refused": True,
            "reason": "fixture hash or source batch mismatch; refusing live calls",
            "live_http_calls": 0,
            "image_calls": 0,
            "telegram_calls": 0,
            "wordpress_calls": 0,
            "make_invoked": False,
            "fixture_confirmation": hash_check,
        }
    if not groq_configured or not gemini_configured:
        return {
            "ok": False,
            "dry_run": False,
            "refused": True,
            "reason": "required writer credentials are not configured",
            "live_http_calls": 0,
            "image_calls": 0,
            "telegram_calls": 0,
            "wordpress_calls": 0,
            "make_invoked": False,
            "credentials_configured": {
                GROQ_KEY_ENV: groq_configured,
                GEMINI_KEY_ENV: gemini_configured,
            },
            "fixture_confirmation": hash_check,
        }

    secrets = tuple(
        str(environ.get(name) or "").strip()
        for name in (GROQ_KEY_ENV, GEMINI_KEY_ENV)
        if str(environ.get(name) or "").strip()
    )
    wall_started = perf_counter()
    baseline_score = replay_groq_structured(fixture)
    baseline_row = {
        "score": baseline_score,
        "article": fixture["baseline_article"],
        "qa": run_article_qa(fixture["baseline_article"], fixture["article_input"], article_mode="normal"),
        "normalized_from": "structured_batch_replay",
        "generation_failure": None,
        "raw_payload": None,
        "live_http_calls": 0,
    }

    live_rows: list[dict[str, Any]] = []
    try:
        live_rows.append(execute_gemini_article_first(fixture, environ=environ, transport=gemini_transport))
    except Exception as exc:
        live_rows.append(
            {
                "score": score_from_exception(
                    fixture,
                    candidate_id=CANDIDATE_GEMINI_ARTICLE_FIRST,
                    provider="google_gemini",
                    model=GEMINI_TEXT_MODEL,
                    mode="article_first_grounding_map",
                    reason=str(redact_secrets(str(exc), secrets)),
                ),
                "article": None,
                "qa": None,
                "normalized_from": None,
                "generation_failure": str(redact_secrets(str(exc), secrets)),
                "raw_payload": None,
                "live_http_calls": 1,
            }
        )
    try:
        live_rows.append(execute_groq_article_first(fixture, environ=environ, http_post=groq_http_post))
    except Exception as exc:
        live_rows.append(
            {
                "score": score_from_exception(
                    fixture,
                    candidate_id=CANDIDATE_GROQ_ARTICLE_FIRST,
                    provider="groq",
                    model=GROQ_MODEL,
                    mode="article_first_grounding_map",
                    reason=str(redact_secrets(str(exc), secrets)),
                ),
                "article": None,
                "qa": None,
                "normalized_from": None,
                "generation_failure": str(redact_secrets(str(exc), secrets)),
                "raw_payload": None,
                "live_http_calls": 1,
            }
        )

    all_rows = [baseline_row, *live_rows]
    scores = [row["score"] for row in all_rows]
    winner = pick_winner(scores)
    diagnosis = diagnose_architecture(scores)
    live_scores = [row["score"] for row in live_rows]
    live_http_calls = sum(int(row.get("live_http_calls") or 0) for row in live_rows)
    wall_ms = int((perf_counter() - wall_started) * 1000)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(fixture_path) / "live_runs" / run_id
    for row in all_rows:
        _persist_candidate(run_dir / row["score"]["candidate_id"], row)
    winner_path = None
    if winner.get("winner"):
        winner_row = next(row for row in all_rows if row["score"]["candidate_id"] == winner["winner"])
        if winner_row.get("article") is not None and winner_row.get("qa") is not None:
            winner_path = persist_winner_candidate(
                fixture_root=Path(fixture_path),
                winner_id=str(winner["winner"]),
                row=winner_row,
                run_dir=run_dir,
                fixture_hashes=hash_check,
            )
    summary = {
        "ok": live_http_calls == EXPECTED_LIVE_CALLS,
        "dry_run": False,
        "fixture": str(fixture_path),
        "event_id": EVENT_ID,
        "source_batch_id": SOURCE_BATCH_ID,
        "fixture_confirmation": hash_check,
        "evidence_refetched": False,
        "evidence_rediscovered": False,
        "frozen_fixture_modified": False,
        "live_http_calls": live_http_calls,
        "expected_live_calls": EXPECTED_LIVE_CALLS,
        "image_calls": 0,
        "telegram_calls": 0,
        "wordpress_calls": 0,
        "make_invoked": False,
        "live_pipeline_changed": False,
        "credentials_configured": {
            GROQ_KEY_ENV: True,
            GEMINI_KEY_ENV: True,
        },
        "scores": scores,
        "winner": winner,
        "architecture_diagnosis": diagnosis,
        "winner_candidate_path": winner_path,
        "run_dir": str(run_dir),
        "live_latency_ms_sum": _sum_numeric(live_scores, "latency_ms"),
        "wall_clock_ms": wall_ms,
        "live_prompt_tokens": _sum_numeric(live_scores, "prompt_tokens"),
        "live_completion_tokens": _sum_numeric(live_scores, "completion_tokens"),
        "live_total_tokens": _sum_numeric(live_scores, "total_tokens"),
        "live_estimated_list_price_usd": _sum_numeric(live_scores, "estimated_list_price_usd"),
        "live_provider_reported_cost_usd": _sum_numeric(live_scores, "provider_reported_cost_usd"),
        "baseline_estimated_list_price_usd": baseline_score.get("estimated_list_price_usd"),
        "baseline_provider_reported_cost_usd": baseline_score.get("provider_reported_cost_usd"),
        "qa_configuration": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
            "evidence_min_words": MIN_EXTRACTED_WORDS,
            "evidence_min_facts": MIN_DISTINCT_FACTS,
            "exact_phrase_n": EXACT_PHRASE_N,
            "critical_similarity": HIGH_SENTENCE_SIMILARITY,
            "warning_similarity": WARN_SENTENCE_SIMILARITY,
        },
        "generation_failures": {
            row["score"]["candidate_id"]: row.get("generation_failure")
            for row in all_rows
            if row.get("generation_failure")
        },
    }
    write_json_utf8(run_dir / "summary.json", summary)
    return summary


def score_from_exception(
    fixture: dict[str, Any],
    *,
    candidate_id: str,
    provider: str,
    model: str,
    mode: str,
    reason: str,
) -> dict[str, Any]:
    score = score_result(
        provider=provider,
        model=model,
        mode=mode,
        qa=None,
        article=None,
        article_input=fixture["article_input"],
        http_status=None,
        latency_ms=None,
        retries=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        estimated_list_price_usd=None,
        provider_reported_cost_usd=None,
        candidate_id=candidate_id,
        replay=False,
    )
    score["generation_failure"] = reason
    return score
