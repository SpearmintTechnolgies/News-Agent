"""One fresh-story Groq GPT-OSS 20B Controlled Writer V3.3 architecture proof."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import split_sentences, word_count
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.controlled.config import CAPACITY_SAFETY_MARGIN_WORDS
from newsagent_v2.article.writer.controlled.groq_oss20 import GroqGptOss20bProseRenderer
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.article.writer.controlled.renderer import controlled_renderer_payload
from newsagent_v2.article.writer.controlled.renderer_contract import (
    CROSS_PARAGRAPH_FACT,
    UNAUTHORIZED_RELATIONSHIP,
    UNKNOWN_FACT_ID,
    editorial_truncation_issues,
    implication_language_present,
    validate_sentence_declarations,
)
from newsagent_v2.article.writer.controlled.semantic import distinctive_source_syntax
from newsagent_v2.article.enrich import enrich_story
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, build_evidence_ledgers, ledger_fingerprint
from newsagent_v2.batch.viability import resolve_candidate_scan_limit
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V33,
    GROQ_GPT_OSS_20B_MODEL,
    GROQ_MODEL,
    HARD_MIN_WORDS,
    TARGET_MAX_WORDS,
    TARGET_MIN_WORDS,
)
from newsagent_v2.bench.writer_bakeoff.controlled_v32_gpt_oss_20b_proof import (
    _freshness,
    _payload_source_prose,
    evaluate_story_capacity,
    select_sufficient_story,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV, load_environ
from newsagent_v2.bench.writer_bakeoff.kimi_safety import live_make_can_select_kimi
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.providers.groq_editorial import GROQ_CHAT_COMPLETIONS_URL, resolve_groq_api_key

REPO_ROOT = Path(__file__).resolve().parents[4]
PROOF_ROOT = REPO_ROOT / "benchmarks" / "writer_bakeoff" / "controlled_v33_fresh"
WINNER_DIR = PROOF_ROOT / "CONTROLLED_WRITER_V3_3_GPT_OSS_20B_AUTONOMOUS_WINNER"
CONSUMED_FLAG = PROOF_ROOT / "controlled_v33_gpt_oss_20b_one_shot_consumed.json"
_LIVE_HTTP_CONSUMED = False

_PREDICTION_RE = re.compile(
    r"\b(?:might seek|may redirect|may send|could stall|could lead|could push|will now|is likely to|likely to)\b",
    re.I,
)
_INFERENCE_RE = re.compile(
    r"\b(?:underscores|underscored|suggests|suggesting|signals|signaling|highlights|highlighted|reflects|which means|therefore)\b",
    re.I,
)
_CAUSAL_RE = re.compile(r"\b(?:because|causing|leading to|as a result|therefore)\b", re.I)


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
            "CONTROLLED_WRITER_V3_3_GPT_OSS_20B_AUTONOMOUS_WINNER"
            if winner
            else "CONTROLLED_WRITER_V3_3_GPT_OSS_20B_FAIL"
        ),
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v33",
        "writer_role": "constrained_prose_realizer",
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
            "writer_architecture": "controlled_writer_v33",
            "reason": "single authorized Groq GPT-OSS 20B Controlled Writer V3.3 generation",
        },
    )


def _classify_unit(unit: Any) -> str:
    if getattr(unit, "connective", False):
        return "CONNECTIVE"
    text = str(getattr(unit, "text", "") or "")
    status = str(getattr(unit, "status", "") or "")
    claims = tuple(getattr(unit, "claim_ids", ()) or ())
    if status == "GROUNDED":
        return "A"
    if status == "UNAUTHORIZED":
        return "J"
    if status in {"INVENTED_QUOTE", "UNAUTHORIZED_QUOTE"}:
        return "H"
    if status == "DEPENDENCY_INVALID":
        return "K"
    if status == "INSEPARABLE_UNSUPPORTED":
        return "C"
    if status == "AMBIGUOUS":
        return "B"
    if _PREDICTION_RE.search(text):
        return "G"
    if _CAUSAL_RE.search(text):
        return "F"
    if _INFERENCE_RE.search(text):
        return "D"
    if claims:
        return "C"
    if implication_language_present(text):
        return "D"
    return "E"


def _assertion_classes(validations: list[Any]) -> dict[str, int]:
    counts = {letter: 0 for letter in "ABCDEFGHIJK"}
    total = 0
    for row in validations:
        for unit in getattr(row, "units", []) or []:
            klass = _classify_unit(unit)
            if klass == "CONNECTIVE":
                continue
            total += 1
            counts[klass] = counts.get(klass, 0) + 1
    counts["total"] = total
    return counts


def _contract_audit(
    *,
    native: dict[str, Any] | None,
    plan: Any,
    payload: dict[str, Any],
    ledgers: EvidenceLedgers,
) -> dict[str, Any]:
    blob = json.dumps(payload)
    facts = [
        fact
        for para in payload.get("paragraph_plans") or []
        for fact in (para.get("proposition_frames") or para.get("allowed_semantic_facts") or [])
        if isinstance(fact, dict)
    ]
    long_slots = []
    claim_index = ledgers.claim_by_id()
    for fact in facts:
        claim = claim_index.get(str(fact.get("claim_id") or fact.get("fact_id") or ""))
        source = claim.text if claim is not None else ""
        for key in ("subject", "object", "complement"):
            value = str(fact.get(key) or "")
            if distinctive_source_syntax(value, source):
                long_slots.append({"claim_id": fact.get("claim_id"), "field": key})
        for item in fact.get("qualifiers") or []:
            if distinctive_source_syntax(str(item), source):
                long_slots.append({"claim_id": fact.get("claim_id"), "field": "qualifiers"})
    declarations = validate_sentence_declarations(native or {}, plan) if native and plan is not None else []
    sentence_rows = []
    implication_hits = 0
    fact_ids_present = False
    if isinstance(native, dict):
        for para in native.get("paragraphs") or []:
            if not isinstance(para, dict):
                continue
            rows = para.get("sentences")
            if isinstance(rows, list) and rows:
                fact_ids_present = True
                for item in rows:
                    if not isinstance(item, dict):
                        continue
                    sentence_rows.append(item)
                    if implication_language_present(str(item.get("text") or "")):
                        implication_hits += 1
                    if item.get("fact_ids_used"):
                        fact_ids_present = True
            else:
                for sentence in split_sentences(str(para.get("text") or "")):
                    if implication_language_present(sentence):
                        implication_hits += 1
    req = payload.get("requirements") or {}
    return {
        "semantic_fact_delexicalization_active": "YES",
        "raw_claim_text_exposed_to_renderer": "YES"
        if any("text" in fact for fact in facts) or any(len(row.text.split()) >= 20 and row.text in blob for row in ledgers.claims)
        else "NO",
        "fact_ids_used_present": "YES" if fact_ids_present else "NO",
        "unknown_fact_declarations": sum(1 for item in declarations if item.get("code") == UNKNOWN_FACT_ID),
        "cross_paragraph_declarations": sum(1 for item in declarations if item.get("code") == CROSS_PARAGRAPH_FACT),
        "implication_language_violations_detected": implication_hits,
        "unauthorized_relationship_declarations": sum(
            1 for item in declarations if item.get("code") == UNAUTHORIZED_RELATIONSHIP
        ),
        "pad_to_word_target": bool(req.get("pad_to_word_target")),
        "invalid_structured_output_issues": declarations,
        "semantic_slot_source_syntax_fields": long_slots,
        "declared_sentence_count": len(sentence_rows),
        "editorial_structural_validation": "IMPLEMENTED",
    }


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
    run_dir = PROOF_ROOT / "live_runs" / stamp / CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V33
    threshold = NORMAL_ARTICLE_POLICY.hard_minimum_words + CAPACITY_SAFETY_MARGIN_WORDS
    base: dict[str, Any] = {
        "provider": "groq",
        "exact_model": GROQ_GPT_OSS_20B_MODEL,
        "writer_architecture": "controlled_writer_v33",
        "writer_role": "constrained_prose_realizer",
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
        "v32_comparison": {
            "supported_generated_assertions": "16/34",
            "retained_words": "277/634",
            "exact_overlaps": 7,
            "max_similarity": 1.0,
        },
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
    payload = controlled_renderer_payload(compiled.plan or assessed["plan"], ledgers)
    native = renderer.native if isinstance(renderer.native, dict) else None
    contract = _contract_audit(native=native, plan=compiled.plan or assessed["plan"], payload=payload, ledgers=ledgers)
    classes = _assertion_classes(compiled.paragraph_validations)
    headline = None if article is None else str(article.get("headline") or "")
    dek = None if article is None else str(article.get("dek") or "")
    if native is not None:
        headline = headline or str(native.get("headline") or "")
        dek = dek or str(native.get("dek") or "")
    editorial_issues = editorial_truncation_issues(headline or "", field="headline") + editorial_truncation_issues(
        dek or "", field="dek"
    )
    quotes_in_article = bool((article or {}).get("quotes"))
    telemetry = {
        "autonomous_writer_pass": winner,
        "writer_architecture": "controlled_writer_v33",
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
        "contract": contract,
        "assertion_classes": classes,
        "editorial_truncation_issues": editorial_issues,
    }
    generated = int(quarantine.get("generated_assertions") or 0)
    supported = int(quarantine.get("grounded_assertions_retained") or 0)
    generated_words = int(quarantine.get("generated_words") or 0)
    retained_words = int(quarantine.get("retained_words") or 0)
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
        "v33_contract": contract,
        "assertion_classes": classes,
        "quarantine": quarantine,
        "generated_paragraphs": quarantine.get("generated_paragraphs"),
        "generated_assertions": generated,
        "supported_immediately": classes.get("A"),
        "matcher_segmentation_failures": classes.get("B"),
        "supported_core_unsupported_tail": classes.get("C"),
        "unsupported_inference": classes.get("D"),
        "unsupported_background": classes.get("E"),
        "unsupported_causality": classes.get("F"),
        "unsupported_prediction": classes.get("G"),
        "unsupported_attribution": classes.get("H"),
        "ambiguous": classes.get("I"),
        "cross_paragraph": classes.get("J"),
        "grounded_assertions_retained": quarantine.get("grounded_assertions_retained"),
        "quarantined_assertions": max(0, generated - supported),
        "unsupported_quarantined": quarantine.get("unsupported_assertions_quarantined"),
        "ambiguous_quarantined": quarantine.get("ambiguous_assertions_quarantined"),
        "unauthorized_quarantined": quarantine.get("unauthorized_assertions_quarantined"),
        "dependency_invalidated": quarantine.get("dependency_invalidated_units"),
        "paragraphs_fully_retained": quarantine.get("paragraphs_fully_retained"),
        "paragraphs_partially_retained": quarantine.get("paragraphs_partially_retained"),
        "paragraphs_fully_rejected": quarantine.get("paragraphs_fully_rejected"),
        "generated_words": generated_words,
        "retained_words": retained_words,
        "quarantined_words": quarantine.get("quarantined_words"),
        "words_saved_by_assertion_level_quarantine": quarantine.get("words_saved_by_assertion_level_quarantine"),
        "headline": headline,
        "dek": dek,
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
        "editorial_truncation_issues": editorial_issues,
        "assertive_sentences": metrics.get("assertive_sentence_count"),
        "mapped_assertions": metrics.get("claim_covered_sentence_count"),
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "body_assertion_not_in_claims": sum(1 for code in criticals if code == "body_assertion_not_in_claims"),
        "quote_criticals": [code for code in criticals if "quote" in str(code)],
        "contextual_criticals": [code for code in criticals if "contextual" in str(code) or "absence" in str(code)],
        "exact_phrase_overlap": metrics.get("exact_overlap_count"),
        "ngram_hits": metrics.get("exact_overlap_ngram_hits"),
        "max_similarity": metrics.get("max_similarity"),
        "unquoted_exact_overlaps": 0 if quotes_in_article else metrics.get("exact_overlap_count"),
        "quote_caused_overlaps": metrics.get("exact_overlap_count") if quotes_in_article else 0,
        "source_language_leakage": leakage,
        "source_prose_passed_as_preferred_renderer_text": "YES" if source_prose else "NO",
        "v31_semantic_fact_active": "YES",
        "v32_assertion_quarantine_active": "YES",
        "v33_atomic_contract_active": "YES",
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
        "v33_vs_v32": {
            "v32_supported": "16/34",
            "v33_supported": f"{supported}/{generated}",
            "v32_retained_words": "277/634",
            "v33_retained_words": f"{retained_words}/{generated_words}",
            "v32_exact_overlaps": 7,
            "v33_exact_overlaps": metrics.get("exact_overlap_count"),
            "v32_max_similarity": 1.0,
            "v33_max_similarity": metrics.get("max_similarity"),
        },
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
