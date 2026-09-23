"""V4 compile: Evidence Packet â†’ free writer â†’ verify â†’ repair â†’ full QA."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import active_normal_depth_policy
from newsagent_v2.article.qa.result import build_qa_result
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.controlled.failures import classify_qa_failure
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.sanitize import sanitize_editorial_artifacts
from newsagent_v2.article.writer.v4.evidence_depth import (
    ARTICLE_FULL,
    check_v4_article_depth,
    assess_evidence_capacity,
)
from newsagent_v2.article.writer.v4.event_research import research_event
from newsagent_v2.article.writer.v4.expand import (
    BELOW_EDITORIAL_TARGET_WARNING,
    EDITORIAL_TARGET_MAX_WORDS,
    EDITORIAL_TARGET_MIN_WORDS,
    realize_editorial_length,
)
from newsagent_v2.article.writer.v4.factbank import build_fact_bank, fact_bank_to_writer_packet
from newsagent_v2.article.writer.v4.persist import persist_v4_attempt
from newsagent_v2.article.writer.v4.repair import (
    MAX_REPAIR_ROUNDS,
    run_targeted_repairs,
)
from newsagent_v2.article.writer.v4.verify import verification_failure_codes, verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_WRITER_MODEL,
    V4NaturalProseWriter,
    V4WriterResult,
    assert_v4_writer_is_free,
)


@dataclass
class V4CompileResult:
    ok: bool
    event_id: str
    article: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    article_input: dict[str, Any] | None = None
    failure_class: str | None = None
    notes: str = ""
    native_words: int = 0
    final_words: int = 0
    initial_words_after_repair: int = 0
    supported: int = 0
    ambiguous: int = 0
    unsupported: int = 0
    repair_calls: int = 0
    writer_calls: int = 0
    expansion_calls: int = 0
    expansion: dict[str, Any] = field(default_factory=dict)
    editorial_target_met: bool = False
    writer_model: str = V4_WRITER_MODEL
    writer_provider: str = "groq"
    attempt_path: str | None = None
    verification: dict[str, Any] = field(default_factory=dict)
    repair_log: dict[str, Any] = field(default_factory=dict)
    critical_codes: list[str] = field(default_factory=list)
    latency_ms: int | None = None
    evidence_capacity: str | None = None
    article_type: str | None = None
    evidence_limited: bool = False
    unique_propositions: int = 0
    independent_sources: int = 0
    depth: dict[str, Any] = field(default_factory=dict)
    factbank: dict[str, Any] = field(default_factory=dict)
    research: dict[str, Any] = field(default_factory=dict)
    kimi_usage: dict[str, Any] | None = None  # Structured Kimi usage summary


def compile_v4_article(
    story: dict[str, Any],
    *,
    writer: V4NaturalProseWriter | Any,
    attempts_root: Path | None = None,
    rank: int = 1,
    research: bool = True,
) -> V4CompileResult:
    """Full V4 candidate compile. Bypasses ArticlePlan / fact_ids_used / relationships."""
    started = perf_counter()
    event_id = str(story.get("event_id") or "")
    working = dict(story)
    pack: dict[str, Any] = {}
    ledgers = None
    research_meta: dict[str, Any] = {}
    factbank_meta: dict[str, Any] = {}
    depth = None
    if research:
        try:
            if not event_id:
                event_id = str(
                    (working.get("article_input") or {}).get("event_id")
                    or working.get("event_id")
                    or f"rank-{rank}"
                )
            from newsagent_v2.v5_generation.source_expansion_adapter import default_search_fn_for_story
            researched = research_event(working, search_fn=default_search_fn_for_story(working))
            pack = researched.pack
            working = researched.story if isinstance(researched.story, dict) else working
            research_meta = researched.as_dict()
            event_id = str(pack.get("event_id") or event_id)
            bank = build_fact_bank(event_id=event_id, pack=pack)
            ledgers = bank.to_ledgers()
            factbank_meta = bank.as_dict()
            depth = assess_evidence_capacity(bank, research=researched)
            packet = fact_bank_to_writer_packet(
                bank,
                story_topic=str(working.get("representative_title") or ""),
                source_names=list(
                    {
                        str(row.get("source") or "")
                        for row in (pack.get("evidence") or [])
                        if isinstance(row, dict) and row.get("source")
                    }
                ),
            )
        except Exception as exc:  # noqa: BLE001
            return V4CompileResult(
                ok=False,
                event_id=event_id,
                failure_class="RESEARCH_FAILED",
                notes=str(exc)[:300],
            )
    else:
        raw = working.get("article_input") if isinstance(working.get("article_input"), dict) else {}
        pack = article_input_for_ledgers(raw)
        if not event_id:
            event_id = str(pack.get("event_id") or working.get("event_id") or f"rank-{rank}")
        bank = build_fact_bank(event_id=event_id, pack=pack)
        ledgers = bank.to_ledgers()
        factbank_meta = bank.as_dict()
        depth = assess_evidence_capacity(bank, research=None)
        # CRITICAL: source_names must come from evidence_units when research=False
        # evidence_units contains the source info from V1 revision evidence
        source_names = []
        for unit in (pack.get("evidence_units") or []):
            if isinstance(unit, dict):
                source = unit.get("source", "").strip()
                if source:
                    source_names.append(source)

        packet = fact_bank_to_writer_packet(
            bank,
            story_topic=str(working.get("representative_title") or ""),
            source_names=source_names,
        )

    if not event_id:
        event_id = str(pack.get("event_id") or working.get("event_id") or f"rank-{rank}")
    if ledgers is None:
        ledgers = build_evidence_ledgers(pack)
    depth_dict = depth.as_dict() if depth is not None else {}
    if not packet.authorized_facts:
        attempt = {
            "event_id": event_id,
            "candidate_rank": rank,
            "failure_class": "INSUFFICIENT_EVIDENCE",
            "claim_count": len(ledgers.claims),
            "evidence_unit_count": len(pack.get("evidence_units") or [])
            if isinstance(pack.get("evidence_units"), list)
            else 0,
            "depth": depth_dict,
            "research": research_meta,
            "architecture": "v4",
            "kimi_calls": 0,
            "paid_private_qwen_calls": 0,
        }
        path = None
        if attempts_root is not None:
            path = persist_v4_attempt(
                attempts_root=attempts_root,
                event_id=event_id,
                attempt=attempt,
                evidence_packet=packet.as_dict(),
            )
        return V4CompileResult(
            ok=False,
            event_id=event_id,
            article_input=pack,
            failure_class="INSUFFICIENT_EVIDENCE",
            notes="no authorized facts in evidence packet",
            attempt_path=str(path) if path else None,
            evidence_capacity=depth.evidence_capacity if depth else None,
            article_type=depth.article_type if depth else None,
            evidence_limited=bool(depth.evidence_limited) if depth else False,
            unique_propositions=depth.unique_propositions if depth else 0,
            independent_sources=depth.independent_sources if depth else 0,
            depth=depth_dict,
            factbank=factbank_meta,
            research=research_meta,
        )

    assert_v4_writer_is_free(getattr(writer, "model", V4_WRITER_MODEL))
    rendered: V4WriterResult = writer.render(packet)
    writer_calls = int(getattr(writer, "generation_calls", 1) or 1)
    if not rendered.ok or rendered.native is None:
        attempt = {
            "event_id": event_id,
            "candidate_rank": rank,
            "writer_provider": rendered.provider,
            "writer_model": rendered.model,
            "writer_calls": writer_calls,
            "repair_calls": 0,
            "failure_class": "WRITER_PROVIDER_ERROR" if rendered.provider_error else "WRITER_OUTPUT_INVALID",
            "error": rendered.error,
            "architecture": "v4",
        }
        path = None
        if attempts_root is not None:
            path = persist_v4_attempt(
                attempts_root=attempts_root,
                event_id=event_id,
                attempt=attempt,
                evidence_packet=packet.as_dict(),
                writer_request_diagnostic=rendered.request_diagnostic,
                native=None,
            )
        return V4CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=attempt["failure_class"],
            notes=str(rendered.error or ""),
            writer_calls=writer_calls,
            writer_model=rendered.model,
            attempt_path=str(path) if path else None,
            latency_ms=rendered.latency_ms,
        )

    native = rendered.native
    native_words = native.word_count
    pre_article = native.as_dict()
    report = verify_v4_native(native, packet=packet, ledgers=ledgers)
    hard_min = active_normal_depth_policy().hard_minimum_words
    repaired, report2, repair_log = run_targeted_repairs(
        native,
        packet=packet,
        ledgers=ledgers,
        article_input=pack,
        writer=writer,
        min_words=hard_min,
        max_rounds=MAX_REPAIR_ROUNDS,
        allow_destructive_length_pad=False,
    )
    words_after_repair = word_count(repaired.article_body)
    copyright_recovery = {"active": False, "disabled": True}
    depth_loss_origin = repair_log.depth_loss_origin

    expanded, report3, expansion = realize_editorial_length(
        repaired,
        packet=packet,
        ledgers=ledgers,
        article_input=pack,
        report=report2,
        writer=writer,
        target_min=(depth.recommended_word_min if depth else EDITORIAL_TARGET_MIN_WORDS),
        depth_loss_origin=depth_loss_origin,
        native_words=native_words,
        depth=depth,
    )
    if expansion.rejected and expansion.provider_infrastructure_error:
        attempt = {
            "event_id": event_id,
            "candidate_rank": rank,
            "writer_provider": rendered.provider,
            "writer_model": rendered.model,
            "writer_calls": writer_calls + int(expansion.model_calls or 0),
            "native_word_count": native_words,
            "failure_class": "PROVIDER_RATE_LIMITED",
            "critical_failure_codes": ["provider_rate_limited"],
            "depth": depth_dict,
            "architecture": "v4",
            "kimi_calls": 0,
            "paid_private_qwen_calls": 0,
        }
        path = None
        if attempts_root is not None:
            path = persist_v4_attempt(
                attempts_root=attempts_root,
                event_id=event_id,
                attempt=attempt,
                evidence_packet=packet.as_dict(),
                writer_request_diagnostic=rendered.request_diagnostic,
                article_pre_verification=pre_article,
                native=expanded.as_dict(),
                assertions=report3.as_dict(),
                repair_log={**repair_log.as_dict(), "expansion": expansion.as_dict()},
            )
        return V4CompileResult(
            ok=False,
            event_id=event_id,
            article_input=pack,
            failure_class="PROVIDER_RATE_LIMITED",
            notes=str(expansion.reject_reason or "provider rate limited"),
            native_words=native_words,
            final_words=word_count(expanded.article_body),
            writer_calls=writer_calls + int(expansion.model_calls or 0),
            expansion_calls=expansion.model_calls,
            expansion=expansion.as_dict(),
            writer_model=rendered.model or V4_WRITER_MODEL,
            writer_provider=str(rendered.provider or "groq"),
            attempt_path=str(path) if path else None,
            critical_codes=["provider_rate_limited"],
            evidence_capacity=depth.evidence_capacity if depth else None,
            article_type=depth.article_type if depth else None,
            evidence_limited=bool(depth.evidence_limited) if depth else False,
            unique_propositions=depth.unique_propositions if depth else 0,
            independent_sources=depth.independent_sources if depth else 0,
            depth=depth_dict,
            factbank=factbank_meta,
            research=research_meta,
            latency_ms=rendered.latency_ms,
        )
    if expansion.rejected and str(expansion.reject_reason or "").startswith("WRITER_UNDERPRODUCED"):
        attempt = {
            "event_id": event_id,
            "candidate_rank": rank,
            "writer_provider": rendered.provider,
            "writer_model": rendered.model,
            "writer_calls": writer_calls + int(expansion.model_calls or 0),
            "repair_calls": repair_log.model_calls,
            "native_word_count": native_words,
            "words_after_repair": words_after_repair,
            "regeneration_used": bool(expansion.attempted),
            "regenerated_words": expansion.validated_words or expansion.generated_words,
            "failure_class": "WRITER_UNDERPRODUCED",
            "critical_failure_codes": ["writer_underproduced"],
            "depth": depth_dict,
            "architecture": "v4",
            "kimi_calls": 0,
            "paid_private_qwen_calls": 0,
            "writer_request_diagnostic": getattr(rendered, "request_diagnostic", None),
        }
        path = None
        if attempts_root is not None:
            path = persist_v4_attempt(
                attempts_root=attempts_root,
                event_id=event_id,
                attempt=attempt,
                evidence_packet=packet.as_dict(),
                writer_request_diagnostic=rendered.request_diagnostic,
                article_pre_verification=pre_article,
                native=expanded.as_dict(),
                assertions=report3.as_dict(),
                repair_log={**repair_log.as_dict(), "expansion": expansion.as_dict()},
            )
        return V4CompileResult(
            ok=False,
            event_id=event_id,
            article_input=pack,
            failure_class="WRITER_UNDERPRODUCED",
            notes=str(expansion.reject_reason or "WRITER_UNDERPRODUCED"),
            native_words=native_words,
            final_words=word_count(expanded.article_body),
            initial_words_after_repair=words_after_repair,
            repair_calls=repair_log.model_calls,
            writer_calls=writer_calls + int(expansion.model_calls or 0),
            expansion_calls=expansion.model_calls,
            expansion=expansion.as_dict(),
            writer_model=rendered.model or V4_WRITER_MODEL,
            writer_provider=str(rendered.provider or "groq"),
            attempt_path=str(path) if path else None,
            verification=report3.as_dict(),
            repair_log=repair_log.as_dict(),
            critical_codes=["writer_underproduced"],
            latency_ms=rendered.latency_ms,
            evidence_capacity=depth.evidence_capacity if depth else None,
            article_type=depth.article_type if depth else None,
            evidence_limited=bool(depth.evidence_limited) if depth else False,
            unique_propositions=depth.unique_propositions if depth else 0,
            independent_sources=depth.independent_sources if depth else 0,
            depth=depth_dict,
            factbank=factbank_meta,
            research=research_meta,
        )
    # Legacy expansion append path removed from active V4 success path.
    # Fresh regeneration replaces the whole article when underproduced.

    assemble_input = dict(pack)
    if depth is not None:
        assemble_input["article_type"] = depth.article_type
    article = assemble_v4_article(
        event_id=event_id,
        native=expanded,
        ledgers=ledgers,
        article_input=assemble_input,
        report=report3,
    )
    article = sanitize_editorial_artifacts(article)
    qa_mode = depth.qa_article_mode if depth else "normal"
    # Keep production/demo hard minimum (normally 500). Never replace it with
    # depth.recommended_word_min — that previously let ~284-word articles QA-pass.
    _base_depth_policy = active_normal_depth_policy()
    qa_depth_policy = (
        replace(
            _base_depth_policy,
            target_min_words=depth.recommended_word_min,
            target_max_words=depth.recommended_word_max,
        )
        if depth
        else _base_depth_policy
    )
    qa = run_article_qa(
        article,
        pack,
        article_mode=qa_mode,
        depth_policy=qa_depth_policy,
        skip_copyright_similarity=True,
    )
    # V4 type-aware depth gate (absolute 120; FULL 250; STANDARD 150; LIMITED 120).
    v4_depth_issues = check_v4_article_depth(article, depth)
    if v4_depth_issues:
        merged = list(qa.get("critical_failures") or []) + list(qa.get("warnings") or [])
        merged.extend(v4_depth_issues)
        metrics = dict(qa.get("metrics") or {})
        qa = build_qa_result(event_id=event_id, issues=merged, metrics=metrics)
        qa.update(
            {
                k: v
                for k, v in {
                    "editorial_target_met": qa.get("editorial_target_met"),
                }.items()
            }
        )

    # Strip any residual copyright codes from critical path (belt-and-suspenders).
    copyright_codes = {
        "exact_phrase_overlap",
        "high_sentence_similarity",
        "copyright_recovery_rejected",
        "copyright_unresolved_no_safe_drop",
    }
    filtered_critical = [
        item
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict) and str(item.get("code") or "") not in copyright_codes
    ]
    if len(filtered_critical) != len(qa.get("critical_failures") or []):
        merged = filtered_critical + list(qa.get("warnings") or [])
        qa = build_qa_result(
            event_id=event_id,
            issues=merged,
            metrics=dict(qa.get("metrics") or {}),
        )

    final_words = int(
        (qa.get("metrics") or {}).get("article_word_count") or word_count(expanded.article_body)
    )
    target_min = depth.recommended_word_min if depth else EDITORIAL_TARGET_MIN_WORDS
    target_max = depth.recommended_word_max if depth else EDITORIAL_TARGET_MAX_WORDS
    editorial_target_met = bool(expansion.editorial_target_met) or (
        target_min <= final_words <= target_max
    )
    if depth and depth.evidence_limited and final_words >= target_min:
        editorial_target_met = True
    expansion.editorial_target_met = editorial_target_met
    if not editorial_target_met and not (depth and depth.evidence_limited):
        expansion.warning = BELOW_EDITORIAL_TARGET_WARNING
        warnings_list = qa.setdefault("warnings", [])
        if isinstance(warnings_list, list) and not any(
            isinstance(item, dict) and item.get("code") == BELOW_EDITORIAL_TARGET_WARNING
            for item in warnings_list
        ):
            warnings_list.append(
                {
                    "code": BELOW_EDITORIAL_TARGET_WARNING,
                    "message": (
                        f"article has {final_words} words; recommended for "
                        f"{depth.article_type if depth else ARTICLE_FULL} is "
                        f"{target_min}-{target_max}"
                    ),
                    "severity": "warning",
                }
            )
            qa["warning_count"] = len(warnings_list)
            qa["warning_codes"] = [
                str(item.get("code") or "")
                for item in warnings_list
                if isinstance(item, dict)
            ]
    qa["editorial_target_met"] = editorial_target_met
    qa["editorial_target_min_words"] = target_min
    qa["editorial_target_max_words"] = target_max
    qa["depth_loss_origin"] = depth_loss_origin
    qa["copyright_recovery"] = copyright_recovery
    qa["copyright_in_publishability_path"] = False
    qa["evidence_capacity"] = depth.evidence_capacity if depth else None
    qa["article_type"] = depth.article_type if depth else None
    qa["evidence_limited"] = bool(depth.evidence_limited) if depth else False
    qa["unique_propositions"] = depth.unique_propositions if depth else 0
    qa["independent_sources"] = depth.independent_sources if depth else 0
    qa["critical_count"] = int(qa.get("critical_count") or len(qa.get("critical_failures") or []))
    qa["warning_count"] = int(qa.get("warning_count") or len(qa.get("warnings") or []))
    qa["warning_codes"] = list(
        qa.get("warning_codes")
        or [
            str(item.get("code") or "")
            for item in (qa.get("warnings") or [])
            if isinstance(item, dict)
        ]
    )
    publishable = bool(qa.get("publishable") and qa.get("qa_passed"))
    qa["qa_publishable"] = publishable
    critical = [
        str(item.get("code") or "")
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict)
    ]
    # Never classify copyright as the failure class.
    failure = None if publishable else (classify_qa_failure(qa) or "qa_publishable=false")
    if failure in {
        "COPYRIGHT_SIMILARITY_FAILED",
        "COPYRIGHT_RECOVERY_REJECTED",
    }:
        failure = "qa_publishable=false"
    if not report3.ok and not publishable:
        for code in verification_failure_codes(report3):
            if code not in critical and code not in copyright_codes:
                critical.append(code)

    kimi_calls = int(writer_calls or 0) if str(rendered.provider or "").lower() == "kimi" else 0
    groq_calls = int(writer_calls or 0) if str(rendered.provider or "").lower() == "groq" else 0

    attempt = {
        "event_id": event_id,
        "candidate_rank": rank,
        "writer_provider": rendered.provider,
        "writer_model": rendered.model,
        "writer_calls": writer_calls,
        "repair_calls": repair_log.model_calls,
        "repair_rounds": repair_log.rounds,
        "expansion_calls": expansion.model_calls,
        "expansion": expansion.as_dict(),
        "usage": rendered.usage,
        "native_word_count": native_words,
        "words_after_repair": words_after_repair,
        "final_word_count": final_words,
        "editorial_target_met": editorial_target_met,
        "editorial_target_min_words": target_min,
        "editorial_target_max_words": target_max,
        "depth": depth_dict,
        "factbank": {
            "unique_proposition_count": factbank_meta.get("unique_proposition_count"),
            "dedup_merged_count": factbank_meta.get("dedup_merged_count"),
            "conflict_pairs": factbank_meta.get("conflict_pairs"),
        },
        "research": research_meta,
        "evidence_capacity": depth.evidence_capacity if depth else None,
        "article_type": qa.get("article_type"),
        "evidence_limited": bool(depth.evidence_limited) if depth else False,
        "unique_propositions": depth.unique_propositions if depth else 0,
        "independent_sources": depth.independent_sources if depth else 0,
        "depth_loss_origin": depth_loss_origin,
        "copyright_recovery": copyright_recovery,
        "copyright_in_publishability_path": False,
        "supported_assertions": report3.supported,
        "ambiguous_assertions": report3.ambiguous,
        "unsupported_assertions": report3.unsupported,
        "grounding_coverage": (qa.get("metrics") or {}).get("body_claim_coverage"),
        "qa_publishable": publishable,
        "critical_count": qa.get("critical_count"),
        "warning_count": qa.get("warning_count"),
        "warning_codes": qa.get("warning_codes"),
        "critical_failure_codes": critical,
        "warnings": [
            str(item.get("code") or "")
            for item in (qa.get("warnings") or [])
            if isinstance(item, dict)
        ],
        "failure_class": failure,
        "latency_ms": rendered.latency_ms,
        "architecture": "v4",
        "kimi_calls": kimi_calls,
        "groq_calls": groq_calls,
        "paid_private_qwen_calls": 0,
    }
    path = None
    if attempts_root is not None:
        path = persist_v4_attempt(
            attempts_root=attempts_root,
            event_id=event_id,
            attempt=attempt,
            evidence_packet=packet.as_dict(),
            writer_request_diagnostic=rendered.request_diagnostic,
            native=native.as_dict(),
            article_pre_verification=pre_article,
            assertions=report3.as_dict(),
            repair_log={**repair_log.as_dict(), "expansion": expansion.as_dict()},
            article=article,
            qa=qa,
        )
    # Build Kimi usage summary if Kimi was the provider
    kimi_usage = None
    if str(rendered.provider or "").lower() == "kimi" and event_id:
        from newsagent_v2.providers.kimi_budget import get_budget_manager

        budget_manager = get_budget_manager()
        kimi_usage = budget_manager.build_story_usage_summary(event_id)
        if "error" not in kimi_usage:
            if 35 < len(kimi_usage.get("percent_used", "0%")) < 50:  # >10K starts HIGH
                pass  # status already set by build_story_usage_summary

    return V4CompileResult(
        ok=publishable,
        event_id=event_id,
        article=article,
        qa=qa,
        article_input=pack,
        failure_class=failure,
        notes="v4_ok" if publishable else "v4_qa_failed",
        native_words=native_words,
        final_words=final_words,
        initial_words_after_repair=words_after_repair,
        supported=report3.supported,
        ambiguous=report3.ambiguous,
        unsupported=report3.unsupported,
        repair_calls=repair_log.model_calls,
        writer_calls=writer_calls,
        expansion_calls=expansion.model_calls,
        expansion=expansion.as_dict(),
        editorial_target_met=editorial_target_met,
        writer_model=rendered.model,
        writer_provider=rendered.provider,
        attempt_path=str(path) if path else None,
        verification=report3.as_dict(),
        repair_log=repair_log.as_dict(),
        critical_codes=critical,
        latency_ms=int((perf_counter() - started) * 1000),
        evidence_capacity=depth.evidence_capacity if depth else None,
        article_type=qa.get("article_type") or (depth.article_type if depth else None),
        evidence_limited=bool(depth.evidence_limited) if depth else False,
        unique_propositions=depth.unique_propositions if depth else 0,
        independent_sources=depth.independent_sources if depth else 0,
        depth=depth_dict,
        factbank=factbank_meta,
        research=research_meta,
        kimi_usage=kimi_usage,
    )
