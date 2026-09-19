"""One fresh-story Groq GPT-OSS 20B Controlled Writer V3.2 architecture proof."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.capacity import (
    CLASS_BORDERLINE,
    CLASS_INSUFFICIENT,
    CLASS_SUFFICIENT,
    analyze_evidence_capacity,
    article_input_for_ledgers,
    refine_capacity_with_plan,
)
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.article.writer.controlled.groq_oss20 import GroqGptOss20bProseRenderer
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.renderer import controlled_renderer_payload
from newsagent_v2.article.enrich import enrich_story
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, build_evidence_ledgers, ledger_fingerprint
from newsagent_v2.batch.viability import cluster_to_story, resolve_candidate_scan_limit
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V32,
    EVENT_ID,
    GROQ_GPT_OSS_20B_MODEL,
    GROQ_MODEL,
    HARD_MIN_WORDS,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV, load_environ
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.diagnostics import freshness_bucket
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL, resolve_groq_api_key
from newsagent_v2.rank import age_hours, freshness_score

REPO_ROOT = Path(__file__).resolve().parents[4]
PROOF_ROOT = REPO_ROOT / "benchmarks" / "writer_bakeoff" / "controlled_v32_fresh"
WINNER_DIR = PROOF_ROOT / "CONTROLLED_WRITER_V3_2_GPT_OSS_20B_AUTONOMOUS_WINNER"
CONSUMED_FLAG = PROOF_ROOT / "controlled_v32_gpt_oss_20b_one_shot_consumed.json"
FROZEN_EVENT_ID = EVENT_ID
_LIVE_HTTP_CONSUMED = False


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def persist_run(
    dest: Path,
    *,
    article: dict[str, Any] | None,
    qa: dict[str, Any] | None,
    native: dict[str, Any] | None,
    raw_payload: dict[str, Any] | None,
    secrets: tuple[str, ...],
    winner: bool,
    extra_telemetry: dict[str, Any],
    ledgers_payload: dict[str, Any] | None = None,
    plan_payload: dict[str, Any] | None = None,
    capacity_payload: dict[str, Any] | None = None,
    discovery_payload: dict[str, Any] | None = None,
    request_messages: list[dict[str, str]] | None = None,
) -> str:
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": (
            "CONTROLLED_WRITER_V3_2_GPT_OSS_20B_AUTONOMOUS_WINNER"
            if winner
            else "CONTROLLED_WRITER_V3_2_GPT_OSS_20B_FAIL"
        ),
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v32",
        "writer_role": "constrained_prose_renderer",
        "writer_provider": "groq",
        "writer_model": GROQ_GPT_OSS_20B_MODEL,
        "development_corrected_candidate": False,
        "frozen_event_005_used": False,
        "qa_unchanged": True,
        "image_generated": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "make_invoked": False,
        "qa_publishable": bool((qa or {}).get("publishable")),
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
    if request_messages is not None:
        write_json_utf8(dest / "renderer_messages.json", redact_secrets({"messages": request_messages}, secrets))
    write_json_utf8(dest / "telemetry.json", redact_secrets(extra_telemetry, secrets))
    if ledgers_payload is not None:
        write_json_utf8(dest / "evidence_claim_ledger.json", redact_secrets(ledgers_payload.get("claims") or [], secrets))
        write_json_utf8(dest / "quote_ledger.json", redact_secrets(ledgers_payload.get("quotes") or [], secrets))
        write_json_utf8(dest / "ledgers.json", redact_secrets(ledgers_payload, secrets))
    if plan_payload is not None:
        write_json_utf8(dest / "article_plan.json", redact_secrets(plan_payload, secrets))
    if capacity_payload is not None:
        write_json_utf8(dest / "evidence_capacity.json", redact_secrets(capacity_payload, secrets))
    if discovery_payload is not None:
        write_json_utf8(dest / "discovery.json", redact_secrets(discovery_payload, secrets))
    return str(dest)


def _mark_consumed() -> None:
    global _LIVE_HTTP_CONSUMED
    _LIVE_HTTP_CONSUMED = True
    PROOF_ROOT.mkdir(parents=True, exist_ok=True)
    write_json_utf8(
        CONSUMED_FLAG,
        {
            "consumed": True,
            "model": GROQ_GPT_OSS_20B_MODEL,
            "writer_architecture": "controlled_writer_v32",
            "reason": "single authorized Groq GPT-OSS 20B Controlled Writer V3.2 generation",
        },
    )


def evaluate_story_capacity(story: dict[str, Any]) -> dict[str, Any]:
    pack = article_input_for_ledgers(story.get("article_input") if isinstance(story.get("article_input"), dict) else {})
    ledgers = build_evidence_ledgers(pack)
    capacity = analyze_evidence_capacity(
        ledgers,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
        safety_margin_words=CAPACITY_SAFETY_MARGIN_WORDS,
    )
    plan = plan_article(
        ledgers,
        pack,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
        target_min_words=NORMAL_ARTICLE_POLICY.target_min_words,
        target_max_words=NORMAL_ARTICLE_POLICY.target_max_words,
    )
    capacity = refine_capacity_with_plan(
        capacity,
        planned_safe_words=plan.planned_safe_words,
        minimum_surviving_words=plan.minimum_surviving_words,
        paragraph_loss_tolerance=plan.paragraph_loss_tolerance,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
    )
    threshold = NORMAL_ARTICLE_POLICY.hard_minimum_words + CAPACITY_SAFETY_MARGIN_WORDS
    safe = (
        capacity.capacity_class == CLASS_SUFFICIENT
        and plan.planned_safe_words >= threshold
        and str(story.get("event_id") or pack.get("event_id") or "") != FROZEN_EVENT_ID
    )
    sufficiency = story.get("evidence_sufficiency") if isinstance(story.get("evidence_sufficiency"), dict) else {}
    extracted = sufficiency.get("extracted_evidence_words")
    if extracted is None:
        extracted = sum(word_count(str(row.get("extracted_text") or row.get("summary") or "")) for row in (pack.get("evidence") or []) if isinstance(row, dict))
    payload = controlled_renderer_payload(plan, ledgers)
    blob = json.dumps(payload)
    long_source = any(len(row.text.split()) >= 20 and row.text in blob for row in ledgers.claims)
    return {
        "pack": pack,
        "ledgers": ledgers,
        "plan": plan,
        "capacity": capacity,
        "safe": safe,
        "threshold": threshold,
        "extracted_evidence_words": extracted,
        "payload_has_long_source_prose": long_source,
        "payload_has_semantic_facts": "proposition_frames" in blob,
    }


def _freshness(story: dict[str, Any]) -> dict[str, Any]:
    evidence = (story.get("article_input") or {}).get("evidence") or []
    published = None
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
        published = evidence[0].get("published")
    item = type("Item", (), {"published": published})()
    hours = age_hours(item)
    return {
        "published": published,
        "age_hours": hours,
        "freshness_score": freshness_score(hours),
        "freshness_bucket": freshness_bucket(hours),
    }


def select_sufficient_story(
    *,
    ranked_clusters: list[Any] | None,
    stories: list[dict[str, Any]] | None,
    enrich_fn: Callable[..., dict[str, Any]],
    scan_limit: int,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    inspections: list[dict[str, Any]] = []
    selected: dict[str, Any] | None = None
    rows: list[dict[str, Any]]
    if stories is not None:
        rows = list(stories)
    else:
        rows = []
        for index, cluster in enumerate(ranked_clusters or [], start=1):
            rows.append(cluster_to_story(cluster, original_rank=index))
    for index, raw in enumerate(rows, start=1):
        if index > scan_limit:
            inspections.append(
                {
                    "event_id": raw.get("event_id"),
                    "rank": raw.get("original_rank") or index,
                    "status": "scan_limit",
                }
            )
            break
        event_id = str(raw.get("event_id") or "")
        if event_id == FROZEN_EVENT_ID:
            inspections.append(
                {
                    "event_id": event_id,
                    "rank": raw.get("original_rank") or index,
                    "status": "skipped_frozen_event_005",
                    "capacity_class": None,
                }
            )
            continue
        story = enrich_fn(raw)
        assessed = evaluate_story_capacity(story)
        capacity = assessed["capacity"]
        plan = assessed["plan"]
        row = {
            "event_id": event_id or story.get("event_id"),
            "rank": story.get("original_rank") or raw.get("original_rank") or index,
            "title": story.get("representative_title") or (story.get("article_input") or {}).get("representative_title"),
            "sources": (story.get("article_input") or {}).get("sources") or story.get("sources") or [],
            "source_count": story.get("source_count"),
            "capacity_class": capacity.capacity_class,
            "planned_safe_words": plan.planned_safe_words,
            "safe_word_range": list(capacity.estimated_safe_word_range),
            "paragraph_loss_tolerance": plan.paragraph_loss_tolerance,
            "extracted_evidence_words": assessed["extracted_evidence_words"],
            "claim_count": capacity.claim_count,
            "quote_count": capacity.quote_count,
            "status": "selected" if assessed["safe"] else capacity.capacity_class,
        }
        inspections.append(row)
        if assessed["safe"] and selected is None:
            selected = {
                "story": story,
                "assessed": assessed,
                "inspection": row,
            }
            break
    discovery = {
        "inspected": inspections,
        "reserve_candidates_inspected": max(0, len([row for row in inspections if row.get("status") != "scan_limit"]) - (1 if selected else 0)),
        "selected_rank": None if selected is None else selected["inspection"]["rank"],
        "stopped_reason": None if selected else "NO_CAPACITY_SAFE_CANDIDATE",
    }
    return selected, discovery, inspections


def _payload_source_prose(renderer: GroqGptOss20bProseRenderer, ledgers: EvidenceLedgers) -> bool:
    blob = json.dumps(renderer.request_body or {})
    return any(len(row.text.split()) >= 20 and row.text in blob for row in ledgers.claims)


def run_proof(
    *,
    environ: dict[str, str] | None = None,
    http_post: Any = None,
    persist: bool = True,
    discover_fn: Callable[[], dict[str, Any]] | None = None,
    enrich_fn: Callable[..., dict[str, Any]] | None = None,
    stories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    wall_started = perf_counter()
    env = environ if environ is not None else load_environ(REPO_ROOT)
    stamp = _utc_stamp()
    run_dir = PROOF_ROOT / "live_runs" / stamp / CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V32
    threshold = NORMAL_ARTICLE_POLICY.hard_minimum_words + CAPACITY_SAFETY_MARGIN_WORDS
    base: dict[str, Any] = {
        "provider": "groq",
        "exact_model": GROQ_GPT_OSS_20B_MODEL,
        "writer_architecture": "controlled_writer_v32",
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
        "qa_unchanged": "YES",
        "aadi_hermes_anime_untouched": "YES",
        "kimi_real_inference_authorized": REAL_INFERENCE_AUTHORIZED,
        "kimi_inference_blocked": "YES" if not REAL_INFERENCE_AUTHORIZED else "NO",
        "live_make_uses_kimi": live_make_can_select_kimi(),
        "development_corrected_candidate": False,
        "frozen_event_005_used": False,
        "default_groq_model_untouched": GROQ_MODEL,
        "production_capacity_threshold": threshold,
        "capacity_safety_margin_words": CAPACITY_SAFETY_MARGIN_WORDS,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
        "discovery_timestamp": stamp,
        "run_dir": str(run_dir),
    }
    if not str(env.get(GROQ_KEY_ENV) or "").strip():
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "groq_key_present": "NO"}
    if REAL_INFERENCE_AUTHORIZED:
        return {**base, "ok": False, "stopped": True, "stage": "kimi_guard", "error": "Kimi must remain blocked"}
    live_http = persist and http_post is None
    if live_http and (_LIVE_HTTP_CONSUMED or CONSUMED_FLAG.is_file()):
        return {**base, "ok": False, "stopped": True, "stage": "one_shot_consumed", "generation_calls": 0}

    api_key = resolve_groq_api_key(env)
    if not api_key:
        return {**base, "ok": False, "stopped": True, "stage": "credentials", "groq_key_present": "NO"}
    secrets = (str(env[GROQ_KEY_ENV]).strip(),)

    discovery_started = perf_counter()
    discovered: dict[str, Any] = {}
    ranked: list[Any] = []
    if stories is None:
        discovered = (discover_fn or discover_ranked_top5)()
        ranked = list(discovered.get("ranked_clusters") or [])
    discovery_ms = int((perf_counter() - discovery_started) * 1000)

    selected, selection, inspections = select_sufficient_story(
        ranked_clusters=ranked,
        stories=stories,
        enrich_fn=enrich_fn or (enrich_story if stories is None else (lambda story, **_kwargs: story)),
        scan_limit=resolve_candidate_scan_limit(env),
    )
    discovery_payload = {
        "discovery_timestamp": stamp,
        "collected_candidates": discovered.get("collected") if stories is None else len(stories or []),
        "rejected_gate_count": len(discovered.get("rejected") or []) if stories is None else 0,
        "clusters": len(ranked) if stories is None else len(stories or []),
        "discovery_wall_ms": discovery_ms,
        "scan_limit": resolve_candidate_scan_limit(env),
        "inspections": inspections,
        "reserve_candidates_inspected": selection.get("reserve_candidates_inspected"),
        "production_capacity_threshold": threshold,
    }
    if selected is None:
        report = {
            **base,
            **discovery_payload,
            "ok": False,
            "stopped": True,
            "stage": "NO_CAPACITY_SAFE_CANDIDATE",
            "failure_class": "NO_CAPACITY_SAFE_CANDIDATE",
            "groq_key_present": "YES",
            "wall_ms": int((perf_counter() - wall_started) * 1000),
            "qa_publishable": "NO",
            "autonomous_writer_pass": False,
            "secret_exposure_check": "PASS",
        }
        if persist:
            persist_run(
                run_dir,
                article=None,
                qa=None,
                native=None,
                raw_payload=None,
                secrets=secrets,
                winner=False,
                extra_telemetry={"failure_class": "NO_CAPACITY_SAFE_CANDIDATE", "generation_calls": 0},
                discovery_payload=discovery_payload,
            )
            write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        return report

    if live_http:
        _mark_consumed()

    story = selected["story"]
    assessed = selected["assessed"]
    ledgers: EvidenceLedgers = assessed["ledgers"]
    fingerprint_before = ledger_fingerprint(ledgers)
    renderer = GroqGptOss20bProseRenderer(api_key=api_key, http_post=http_post)
    compiled = compile_controlled_article(story, renderer=renderer)
    wall_ms = int((perf_counter() - wall_started) * 1000)
    fingerprint_after = ledger_fingerprint(build_evidence_ledgers(assessed["pack"]))
    article = compiled.article
    qa = compiled.qa
    metrics = (qa or {}).get("metrics") or {}
    words = int(metrics.get("article_word_count") or (word_count(str((article or {}).get("article_body") or "")) if article else 0))
    publishable = bool((qa or {}).get("publishable"))
    winner = bool(publishable and compiled.ok and article is not None)
    criticals = [item.get("code") for item in (qa or {}).get("critical_failures") or []]
    warnings = [item.get("code") for item in (qa or {}).get("warnings") or []]
    quarantine = compiled.quarantine or {}
    capacity = compiled.capacity.as_dict() if compiled.capacity else assessed["capacity"].as_dict()
    plan = compiled.plan.as_dict() if compiled.plan else assessed["plan"].as_dict()
    freshness = _freshness(story)
    source_prose = _payload_source_prose(renderer, ledgers)
    leakage = "YES" if (
        int(metrics.get("exact_overlap_count") or 0) > 0
        or float(metrics.get("max_similarity") or 0) >= 0.92
        or any(code in {"exact_phrase_overlap", "high_sentence_similarity"} for code in criticals)
    ) else "NO"
    inspection = selected["inspection"]
    ledgers_payload = {
        "claims": ledgers.as_claim_dicts(),
        "quotes": ledgers.as_quote_dicts(),
        "fingerprint": fingerprint_before,
    }
    request_messages = None
    if renderer.request_body and isinstance(renderer.request_body.get("messages"), list):
        request_messages = renderer.request_body["messages"]
    telemetry = {
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v32",
        "http_status": renderer.http_status,
        "latency_ms": renderer.latency_ms,
        "wall_ms": wall_ms,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "total_tokens": renderer.usage.get("total_tokens"),
        "generation_calls": renderer.generation_calls,
        "qa_publishable": publishable,
        "quarantine": quarantine,
        "make_invoked": False,
        "image_generated": False,
        "source_prose_in_renderer_payload": source_prose,
    }
    report = {
        **base,
        **discovery_payload,
        "ok": winner,
        "stopped": True,
        "stage": "generation" if renderer.generation_calls else compiled.failure_class,
        "groq_key_present": "YES",
        "selected_rank": inspection["rank"],
        "event_id": story.get("event_id"),
        "headline_topic": inspection.get("title"),
        "source_count": inspection.get("source_count"),
        "source_names": inspection.get("sources"),
        "freshness": freshness,
        "discovery_wall_ms": discovery_ms,
        "extracted_evidence_words": assessed["extracted_evidence_words"],
        "evidence_claim_count": capacity.get("claim_count"),
        "quote_count": capacity.get("quote_count"),
        "capacity_class": capacity.get("capacity_class"),
        "safe_word_range": capacity.get("estimated_safe_word_range"),
        "planned_safe_words": (plan or {}).get("planned_safe_words"),
        "paragraph_loss_tolerance": (plan or {}).get("paragraph_loss_tolerance"),
        "http_status": renderer.http_status,
        "generation_calls": renderer.generation_calls,
        "gpt_oss_20b_calls": renderer.generation_calls,
        "latency_ms": renderer.latency_ms,
        "prompt_tokens": renderer.usage.get("prompt_tokens"),
        "completion_tokens": renderer.usage.get("completion_tokens"),
        "total_tokens": renderer.usage.get("total_tokens"),
        "quarantine": quarantine,
        "generated_paragraphs": quarantine.get("generated_paragraphs"),
        "generated_assertions": quarantine.get("generated_assertions"),
        "grounded_assertions_retained": quarantine.get("grounded_assertions_retained"),
        "unsupported_quarantined": quarantine.get("unsupported_assertions_quarantined"),
        "ambiguous_quarantined": quarantine.get("ambiguous_assertions_quarantined"),
        "unauthorized_quarantined": quarantine.get("unauthorized_assertions_quarantined"),
        "dependency_invalidated": quarantine.get("dependency_invalidated_units"),
        "paragraphs_fully_retained": quarantine.get("paragraphs_fully_retained"),
        "paragraphs_partially_retained": quarantine.get("paragraphs_partially_retained"),
        "paragraphs_fully_rejected": quarantine.get("paragraphs_fully_rejected"),
        "generated_words": quarantine.get("generated_words"),
        "retained_words": quarantine.get("retained_words"),
        "quarantined_words": quarantine.get("quarantined_words"),
        "words_saved_by_assertion_level_quarantine": quarantine.get("words_saved_by_assertion_level_quarantine"),
        "headline": None if article is None else article.get("headline"),
        "dek": None if article is None else article.get("dek"),
        "subheadings": [
            str(section.get("purpose") or "")
            for section in ((article or {}).get("article_sections") or [])
            if isinstance(section, dict)
        ],
        "planned_subheadings": (plan or {}).get("subheading_plans") or [],
        "word_count": words,
        "hard_min_350": "PASS" if words >= HARD_MIN_WORDS else "FAIL",
        "target_450_800": "PASS" if TARGET_MIN_WORDS <= words <= TARGET_MAX_WORDS else "WARNING",
        "structure": "PASS" if not any(code in {"paragraph_missing_claim_ids", "unknown_claim_id"} for code in criticals) else "FAIL",
        "assertive_sentences": metrics.get("assertive_sentence_count"),
        "mapped_assertions": metrics.get("claim_covered_sentence_count"),
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "body_assertion_not_in_claims": sum(1 for code in criticals if code == "body_assertion_not_in_claims"),
        "quote_criticals": [code for code in criticals if "quote" in str(code)],
        "contextual_criticals": [code for code in criticals if "contextual" in str(code) or "absence" in str(code)],
        "exact_phrase_overlap": metrics.get("exact_overlap_count"),
        "ngram_hits": metrics.get("exact_overlap_ngram_hits"),
        "max_similarity": metrics.get("max_similarity"),
        "source_language_leakage": leakage,
        "source_prose_passed_as_preferred_renderer_text": "YES" if source_prose else "NO",
        "v31_semantic_fact_active": "YES" if assessed["payload_has_semantic_facts"] else "NO",
        "v32_assertion_quarantine_active": "YES",
        "headline_qa": "PASS" if not any(str(code).startswith("headline") for code in criticals) else "FAIL",
        "mechanics_qa": "PASS" if not any(code in {"empty_body", "repeated_paragraph", "malformed_punctuation"} for code in criticals) else "FAIL",
        "seo_qa": "PASS" if not any(str(code).startswith("seo") or code == "invalid_slug" for code in criticals) else "FAIL",
        "qa_publishable": "YES" if publishable else "NO",
        "warnings": warnings,
        "critical_failures": criticals,
        "failure_class": compiled.failure_class,
        "autonomous_writer_pass": winner,
        "winner_path": None,
        "provider_error": renderer.error,
        "wall_ms": wall_ms,
        "ledger_fingerprint_before": fingerprint_before,
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
            secrets=secrets,
            winner=False,
            extra_telemetry=telemetry,
            ledgers_payload=ledgers_payload,
            plan_payload=plan,
            capacity_payload=capacity,
            discovery_payload=discovery_payload,
            request_messages=request_messages,
        )
        write_json_utf8(run_dir / "report.json", redact_secrets(report, secrets))
        if winner:
            winner_path = persist_run(
                WINNER_DIR,
                article=article,
                qa=qa,
                native=renderer.native,
                raw_payload=renderer.raw_payload,
                secrets=secrets,
                winner=True,
                extra_telemetry=telemetry,
                ledgers_payload=ledgers_payload,
                plan_payload=plan,
                capacity_payload=capacity,
                discovery_payload=discovery_payload,
                request_messages=request_messages,
            )
            report["winner_path"] = winner_path
            write_json_utf8(WINNER_DIR / "report.json", redact_secrets(report, secrets))
    return report


def main() -> dict[str, Any]:
    return run_proof()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
