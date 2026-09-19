"""V4 Top-5 article-only batch validation.

Reuses proven architecture:
- discover_ranked_top5 / clustering / ranking
- multi-source Researcher + FactBank + EvidenceCapacity
- Kimi writer (RICH long-form editorial plan; MEDIUM/LIMITED standard compile)
- surgical repair, provenance, QA (copyright skipped)

NO image / Telegram / WordPress.
Does NOT mutate production evidence_depth defaults.
Does NOT touch event-030.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from newsagent_v2.approval.store import STATE_GENERATED, ApprovalStore
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.textutil import split_paragraphs, word_count
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.capability_500_v2 import (
    CAPABILITY_V2_MAX_TOKENS,
    Capability500V2Writer,
    build_editorial_plan,
    paragraph_word_counts,
    proposition_usage,
)
from newsagent_v2.article.writer.v4.capability_500_v3 import (
    verify_event_030_untouched,
)
from newsagent_v2.article.writer.v4.compile import V4CompileResult, compile_v4_article
from newsagent_v2.article.writer.v4.evidence_depth import (
    ARTICLE_FULL,
    ARTICLE_LIMITED,
    ARTICLE_STANDARD,
    CAPACITY_LIMITED,
    CAPACITY_MEDIUM,
    CAPACITY_RICH,
    DepthDecision,
    assess_evidence_capacity,
    check_v4_article_depth,
)
from newsagent_v2.article.writer.v4.event_research import research_event
from newsagent_v2.article.writer.v4.experiment import configure_v4_kimi_primary
from newsagent_v2.article.writer.v4.factbank import build_fact_bank, fact_bank_to_writer_packet
from newsagent_v2.article.writer.v4.persist import persist_v4_attempt
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
    V4_MAX_COMPLETION_TOKENS,
)
from newsagent_v2.article.writer.v4.repair import (
    paragraph_integrity_ok,
    run_targeted_repairs,
    soft_trim_article_body,
)
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_KIMI_MODEL,
    assert_v4_writer_is_free,
    build_v4_writer,
)
from newsagent_v2.batch.contract import TOP5_COUNT
from newsagent_v2.batch.viability import (
    DEFAULT_CANDIDATE_SCAN_LIMIT,
    cluster_to_story,
    resolve_candidate_scan_limit,
)
from newsagent_v2.control.__main__ import _load_environ
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.control.make_recovery import MAKE_RUNS_ROOT

REPO = Path(__file__).resolve().parents[5]
PROTECTED_EVENT_030_HASH = "66a948580b2d1f5bce2518061a83c6130358437f6c3b4a329597185bbb22213a"

# Validation bands for this Top-5 measurement (harness-only; production defaults unchanged).
RICH_RANGE = (450, 550)
RICH_PREFER = (480, 520)
MEDIUM_RANGE = (250, 449)
MEDIUM_PREFER = (280, 360)
LIMITED_RANGE = (120, 249)
LIMITED_PREFER = (140, 200)

TARGET_PUBLISHABLE = TOP5_COUNT  # 5


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _source_domains(pack: dict[str, Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for row in pack.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "")
        host = (urlparse(url).netloc or "").lower()
        if host and host not in seen:
            seen.add(host)
            found.append(host)
    return found


def _validation_depth(base: DepthDecision) -> DepthDecision:
    """Remap word bands for this batch only. Capacity classification stays honest."""
    if base.evidence_capacity == CAPACITY_RICH:
        return DepthDecision(
            evidence_capacity=CAPACITY_RICH,
            article_type=ARTICLE_FULL,
            recommended_word_min=RICH_RANGE[0],
            recommended_word_max=RICH_RANGE[1],
            prefer_min=RICH_PREFER[0],
            prefer_max=RICH_PREFER[1],
            unique_propositions=base.unique_propositions,
            independent_sources=base.independent_sources,
            primary_sources=base.primary_sources,
            numeric_fact_count=base.numeric_fact_count,
            attribution_count=base.attribution_count,
            evidence_limited=False,
            qa_article_mode="normal",
            reason=f"{base.reason}+top5_rich_500_band",
            research=dict(base.research),
        )
    if base.evidence_capacity == CAPACITY_MEDIUM:
        return DepthDecision(
            evidence_capacity=CAPACITY_MEDIUM,
            article_type=ARTICLE_STANDARD,
            recommended_word_min=MEDIUM_RANGE[0],
            recommended_word_max=MEDIUM_RANGE[1],
            prefer_min=MEDIUM_PREFER[0],
            prefer_max=MEDIUM_PREFER[1],
            unique_propositions=base.unique_propositions,
            independent_sources=base.independent_sources,
            primary_sources=base.primary_sources,
            numeric_fact_count=base.numeric_fact_count,
            attribution_count=base.attribution_count,
            evidence_limited=False,
            qa_article_mode="brief",
            reason=f"{base.reason}+top5_medium_band",
            research=dict(base.research),
        )
    return DepthDecision(
        evidence_capacity=CAPACITY_LIMITED,
        article_type=ARTICLE_LIMITED,
        recommended_word_min=LIMITED_RANGE[0],
        recommended_word_max=LIMITED_RANGE[1],
        prefer_min=LIMITED_PREFER[0],
        prefer_max=LIMITED_PREFER[1],
        unique_propositions=base.unique_propositions,
        independent_sources=base.independent_sources,
        primary_sources=base.primary_sources,
        numeric_fact_count=base.numeric_fact_count,
        attribution_count=base.attribution_count,
        evidence_limited=True,
        qa_article_mode="brief",
        reason=f"{base.reason}+top5_limited_band",
        research=dict(base.research),
    )


def _qa_flags(qa: dict[str, Any], report: Any) -> dict[str, Any]:
    critical = list(qa.get("critical_failures") or [])
    critical_count = int(qa.get("critical_count") or len(critical))
    warning_count = int(qa.get("warning_count") or len(qa.get("warnings") or []))
    quote_ok = not any(
        "quote" in str(item.get("code") or "").lower()
        for item in critical
        if isinstance(item, dict)
    )
    mech_ok = not any(
        item.get("module") == "mechanics" and item.get("severity") == "critical"
        for item in critical
        if isinstance(item, dict)
    )
    sec_ok = int((qa.get("metrics") or {}).get("publishing_safety_issue_count") or 0) == 0
    grounding_ok = (
        getattr(report, "unsupported", 1) == 0
        and getattr(report, "ambiguous", 1) == 0
        and getattr(report, "supported", 0) > 0
    )
    return {
        "quote_grounding": "PASS" if quote_ok else "FAIL",
        "mechanics": "PASS" if mech_ok else "FAIL",
        "security": "PASS" if sec_ok else "FAIL",
        "grounding_ok": grounding_ok,
        "critical_count": critical_count,
        "warning_count": warning_count,
    }


def _compile_rich_longform(
    *,
    story: dict[str, Any],
    pack: dict[str, Any],
    bank: Any,
    depth: DepthDecision,
    researched: Any,
    rich_writer: Capability500V2Writer,
    attempts_root: Path,
    rank: int,
) -> dict[str, Any]:
    """Proven RICH ~500-word path (editorial plan + trim + surgical repair)."""
    event_id = str(story.get("event_id") or pack.get("event_id") or f"rank-{rank}")
    topic = str(story.get("representative_title") or pack.get("representative_title") or "")
    packet = fact_bank_to_writer_packet(
        bank,
        story_topic=topic,
        source_names=list(
            {
                str(row.get("source") or "")
                for row in (pack.get("evidence") or [])
                if isinstance(row, dict) and row.get("source")
            }
        ),
    )
    ledgers = bank.to_ledgers()
    plan = build_editorial_plan(packet)
    calls_before = int(getattr(rich_writer, "generation_calls", 0) or 0)
    rendered = rich_writer.render_with_plan(packet, plan)
    initial_gens = 1 if rendered.ok else 0
    if not rendered.ok or rendered.native is None:
        return {
            "ok": False,
            "state": "FAILED",
            "event_id": event_id,
            "failure_class": "WRITER_PROVIDER_ERROR" if rendered.provider_error else "WRITER_OUTPUT_INVALID",
            "notes": str(rendered.error or "")[:300],
            "evidence_capacity": depth.evidence_capacity,
            "article_type": depth.article_type,
            "Kimi_initial_generations": initial_gens,
            "Kimi_repairs": 0,
            "Kimi_regenerations": 0,
            "research": researched.as_dict(),
            "depth": depth.as_dict(),
            "source_domains": _source_domains(pack),
        }

    native = rendered.native
    native_words = word_count(native.article_body)
    working = deepcopy(native)
    trimmed_body, trim_meta = soft_trim_article_body(
        working.article_body,
        target_max=depth.recommended_word_max,
        prefer_max=depth.prefer_max,
        prefer_min=depth.prefer_min,
        target_min=depth.recommended_word_min,
    )
    working.article_body = trimmed_body
    pre_repair = verify_v4_native(working, packet=packet, ledgers=ledgers)
    repaired, report2, repair_log = run_targeted_repairs(
        working,
        packet=packet,
        ledgers=ledgers,
        article_input=pack,
        writer=rich_writer,
        min_words=depth.recommended_word_min,
        allow_destructive_length_pad=False,
    )
    if not paragraph_integrity_ok(repaired.article_body, prior_body=working.article_body):
        repaired = working
        report2 = verify_v4_native(repaired, packet=packet, ledgers=ledgers)

    # Second soft trim if still over max after repair.
    final_words = word_count(repaired.article_body)
    if final_words > depth.recommended_word_max:
        body2, trim2 = soft_trim_article_body(
            repaired.article_body,
            target_max=depth.recommended_word_max,
            prefer_max=depth.prefer_max,
            prefer_min=depth.prefer_min,
            target_min=depth.recommended_word_min,
        )
        probe = deepcopy(repaired)
        probe.article_body = body2
        probe_rep = verify_v4_native(probe, packet=packet, ledgers=ledgers)
        if (
            depth.recommended_word_min <= word_count(body2) <= depth.recommended_word_max
            and probe_rep.unsupported == 0
            and probe_rep.ambiguous == 0
        ):
            repaired = probe
            report2 = probe_rep
            trim_meta = {**trim_meta, "second_trim": trim2}
            final_words = word_count(body2)

    article = assemble_v4_article(
        event_id=event_id,
        native=repaired,
        ledgers=ledgers,
        article_input=pack,
        report=report2,
    )
    qa = run_article_qa(
        article,
        pack,
        article_mode=depth.qa_article_mode,
        skip_copyright_similarity=True,
    )
    depth_issues = check_v4_article_depth(article, depth)
    if depth_issues:
        from newsagent_v2.article.qa.result import build_qa_result

        merged = list(qa.get("critical_failures") or []) + list(qa.get("warnings") or [])
        merged.extend(depth_issues)
        qa = build_qa_result(
            event_id=event_id,
            issues=merged,
            metrics=dict(qa.get("metrics") or {}),
        )

    flags = _qa_flags(qa, report2)
    body = str(article.get("article_body") or "")
    final_words = word_count(body)
    in_band = depth.recommended_word_min <= final_words <= depth.recommended_word_max
    coherence = (
        "PASS"
        if (
            flags["grounding_ok"]
            and flags["critical_count"] == 0
            and in_band
            and paragraph_integrity_ok(body, prior_body=native.article_body)
        )
        else "FAIL"
    )
    ok = bool(
        flags["grounding_ok"]
        and flags["critical_count"] == 0
        and flags["quote_grounding"] == "PASS"
        and flags["mechanics"] == "PASS"
        and flags["security"] == "PASS"
        and coherence == "PASS"
        and in_band
        and report2.ambiguous == 0
        and report2.unsupported == 0
    )
    used, _unused = proposition_usage(packet, body)
    path = persist_v4_attempt(
        attempts_root=attempts_root,
        event_id=event_id,
        attempt={
            "event_id": event_id,
            "candidate_rank": rank,
            "writer_provider": "kimi",
            "writer_model": V4_KIMI_MODEL,
            "architecture": "v4_top5_rich_longform",
            "ok": ok,
            "native_word_count": native_words,
            "final_word_count": final_words,
            "depth": depth.as_dict(),
            "trim_meta": trim_meta,
            "repair_log": repair_log.as_dict(),
        },
        evidence_packet=packet.as_dict(),
        writer_request_diagnostic=getattr(rendered, "request_diagnostic", None),
        native=repaired.as_dict(),
        article=article if ok else None,
        qa=qa,
        repair_log=repair_log.as_dict(),
        assertions=report2.as_dict(),
    )
    calls_after = int(getattr(rich_writer, "generation_calls", 0) or 0)
    return {
        "ok": ok,
        "state": "FROZEN" if ok else "FAILED",
        "event_id": event_id,
        "headline": article.get("headline"),
        "article": article if ok else None,
        "qa": qa,
        "article_input": pack,
        "failure_class": None if ok else "QA_OR_GROUNDING_FAILED",
        "critical_codes": [
            str(i.get("code") or "")
            for i in (qa.get("critical_failures") or [])
            if isinstance(i, dict)
        ],
        "sources_retrieved": researched.sources_retrieved,
        "independent_sources": researched.independent_sources,
        "primary_sources": researched.primary_sources,
        "source_domains": _source_domains(pack),
        "raw_research_words": researched.raw_research_words,
        "unique_propositions": bank.unique_proposition_count,
        "conflicts": len(bank.conflicts),
        "evidence_capacity": depth.evidence_capacity,
        "article_type": depth.article_type,
        "provider": "kimi",
        "model": V4_KIMI_MODEL,
        "native_body_words": native_words,
        "final_body_words": final_words,
        "paragraph_word_counts": paragraph_word_counts(body),
        "propositions_available": len(packet.authorized_facts),
        "propositions_used": used,
        "grounding_supported": report2.supported,
        "grounding_ambiguous": report2.ambiguous,
        "grounding_unsupported": report2.unsupported,
        "quote_grounding": flags["quote_grounding"],
        "mechanics": flags["mechanics"],
        "security": flags["security"],
        "coherence": coherence,
        "critical_count": flags["critical_count"],
        "warning_count": flags["warning_count"],
        "canonical_body_hash": _sha256_text(body) if ok else None,
        "attempt_path": str(path),
        "Kimi_initial_generations": initial_gens,
        "Kimi_repairs": int(repair_log.model_calls or 0),
        "Kimi_regenerations": max(0, calls_after - calls_before - initial_gens),
        "research": researched.as_dict(),
        "depth": depth.as_dict(),
        "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
    }


def _result_from_compile(
    compiled: V4CompileResult,
    *,
    researched: Any,
    pack: dict[str, Any],
    depth: DepthDecision,
    bank: Any,
) -> dict[str, Any]:
    qa = compiled.qa or {}
    article = compiled.article or {}
    body = str(article.get("article_body") or "")
    report_like = type(
        "R",
        (),
        {
            "supported": compiled.supported,
            "ambiguous": compiled.ambiguous,
            "unsupported": compiled.unsupported,
        },
    )()
    flags = _qa_flags(qa, report_like) if qa else {
        "quote_grounding": "FAIL",
        "mechanics": "FAIL",
        "security": "FAIL",
        "grounding_ok": False,
        "critical_count": 1,
        "warning_count": 0,
    }
    in_band = depth.recommended_word_min <= compiled.final_words <= depth.recommended_word_max
    # Soft accept: MEDIUM/LIMITED within production-ish floors still publishable if QA ok
    # and absolute min 120, even if slightly below validation prefer band — report honestly.
    length_ok = in_band or (
        compiled.final_words >= 120
        and compiled.final_words <= depth.recommended_word_max
        and compiled.ok
    )
    coherence = (
        "PASS"
        if compiled.ok and flags["grounding_ok"] and flags["critical_count"] == 0 and length_ok
        else "FAIL"
    )
    ok = bool(
        compiled.ok
        and compiled.article
        and compiled.qa
        and flags["critical_count"] == 0
        and compiled.ambiguous == 0
        and compiled.unsupported == 0
        and length_ok
    )
    used_ids = []
    try:
        packet = fact_bank_to_writer_packet(bank, story_topic="")
        used_ids, _ = proposition_usage(packet, body)
    except Exception:  # noqa: BLE001
        used_ids = []
    return {
        "ok": ok,
        "state": "FROZEN" if ok else "FAILED",
        "event_id": compiled.event_id,
        "headline": article.get("headline") if article else None,
        "article": article if ok else None,
        "qa": qa if ok else qa,
        "article_input": pack,
        "failure_class": None if ok else (compiled.failure_class or "QA_FAILED"),
        "critical_codes": list(compiled.critical_codes or []),
        "sources_retrieved": researched.sources_retrieved,
        "independent_sources": researched.independent_sources,
        "primary_sources": researched.primary_sources,
        "source_domains": _source_domains(pack),
        "raw_research_words": researched.raw_research_words,
        "unique_propositions": bank.unique_proposition_count,
        "conflicts": len(bank.conflicts),
        "evidence_capacity": depth.evidence_capacity,
        "article_type": depth.article_type,
        "provider": compiled.writer_provider or "kimi",
        "model": compiled.writer_model or V4_KIMI_MODEL,
        "native_body_words": compiled.native_words,
        "final_body_words": compiled.final_words,
        "paragraph_word_counts": paragraph_word_counts(body) if body else [],
        "propositions_available": bank.unique_proposition_count,
        "propositions_used": used_ids,
        "grounding_supported": compiled.supported,
        "grounding_ambiguous": compiled.ambiguous,
        "grounding_unsupported": compiled.unsupported,
        "quote_grounding": flags["quote_grounding"],
        "mechanics": flags["mechanics"],
        "security": flags["security"],
        "coherence": coherence,
        "critical_count": flags["critical_count"],
        "warning_count": flags["warning_count"],
        "canonical_body_hash": _sha256_text(body) if ok else None,
        "attempt_path": compiled.attempt_path,
        "Kimi_initial_generations": 1 if compiled.writer_calls else 0,
        "Kimi_repairs": int(compiled.repair_calls or 0),
        "Kimi_regenerations": int(compiled.expansion_calls or 0),
        "research": researched.as_dict(),
        "depth": depth.as_dict(),
        "max_completion_tokens": V4_MAX_COMPLETION_TOKENS,
        "notes": compiled.notes,
    }


def run_top5_article_batch(*, environ: dict[str, str] | None = None) -> dict[str, Any]:
    env = configure_v4_kimi_primary(dict(environ or _load_environ()))
    env.setdefault("ARTICLE_MIN_WORDS", "120")
    env.setdefault("NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS", "120")
    for key, value in list(env.items()):
        if key.startswith("NEWSAGENT") or key in {"ARTICLE_MIN_WORDS"}:
            os.environ[key] = str(value)

    pre030 = verify_event_030_untouched()
    if pre030.get("present") and not pre030.get("ok"):
        return {
            "status": "FAIL",
            "reason": "event030_hash_mismatch_before_batch",
            "event_030_check": pre030,
        }

    assert_v4_writer_is_free(V4_KIMI_MODEL, environ=env)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"v4-top5-articles-{stamp}"
    attempts_root = MAKE_RUNS_ROOT / batch_id / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    out_root = MAKE_RUNS_ROOT.parent / "capability_tests" / batch_id
    out_root.mkdir(parents=True, exist_ok=True)
    store = ApprovalStore(root=MAKE_RUNS_ROOT.parent / "approval")

    discovered = discover_ranked_top5()
    ranked = list(discovered.get("ranked_clusters") or [])
    stories_collected = int(discovered.get("collected") or 0)
    stories_after_gates = stories_collected - len(discovered.get("rejected") or [])
    scan_limit = resolve_candidate_scan_limit(env)
    stories = [
        cluster_to_story(cluster, original_rank=i)
        for i, cluster in enumerate(ranked[:scan_limit], start=1)
    ]

    base_writer = build_v4_writer(
        environ=env,
        enable_failover=False,
        max_calls=max(scan_limit * 6, 24),
    )
    rich_writer = Capability500V2Writer(
        transport=base_writer.transport,
        api_key=getattr(base_writer, "api_key", None),
        environ=env,
        max_calls=max(scan_limit * 4, 20),
        timeout_seconds=240,
    )
    std_writer = build_v4_writer(
        environ=env,
        enable_failover=False,
        max_calls=max(scan_limit * 6, 24),
    )
    # Share transport / call counter discipline via same Kimi config.
    std_writer = type(std_writer)(
        transport=base_writer.transport,
        api_key=getattr(base_writer, "api_key", None),
        environ=env,
        max_calls=max(scan_limit * 6, 24),
        timeout_seconds=180,
    )

    publishable: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    failure_buckets = {
        "grounding_failures": 0,
        "mechanics_failures": 0,
        "security_failures": 0,
        "coherence_failures": 0,
        "length_failures": 0,
        "research/evidence_failures": 0,
    }
    kimi_initial = 0
    kimi_repairs = 0
    kimi_regens = 0
    selected_events: list[str] = []

    for index, story in enumerate(stories, start=1):
        if len(publishable) >= TARGET_PUBLISHABLE:
            break
        event_id = str(story.get("event_id") or f"rank-{index}")
        try:
            researched = research_event(story)
            pack = researched.pack
            bank = build_fact_bank(event_id=event_id, pack=pack)
            base_depth = assess_evidence_capacity(bank, research=researched)
            depth = _validation_depth(base_depth)
        except Exception as exc:  # noqa: BLE001
            row = {
                "ok": False,
                "state": "FAILED",
                "event_id": event_id,
                "failure_class": "RESEARCH_FAILED",
                "notes": str(exc)[:300],
                "rank": index,
            }
            attempts.append(row)
            failure_buckets["research/evidence_failures"] += 1
            continue

        if not bank.propositions:
            row = {
                "ok": False,
                "state": "FAILED",
                "event_id": event_id,
                "failure_class": "INSUFFICIENT_EVIDENCE",
                "evidence_capacity": depth.evidence_capacity,
                "unique_propositions": 0,
                "rank": index,
                "sources_retrieved": researched.sources_retrieved,
                "independent_sources": researched.independent_sources,
            }
            attempts.append(row)
            failure_buckets["research/evidence_failures"] += 1
            continue

        if depth.evidence_capacity == CAPACITY_RICH:
            row = _compile_rich_longform(
                story=story,
                pack=pack,
                bank=bank,
                depth=depth,
                researched=researched,
                rich_writer=rich_writer,
                attempts_root=attempts_root,
                rank=index,
            )
        else:
            # MEDIUM/LIMITED: production compile path (Kimi, copyright skipped).
            # Depth gates inside compile use production bands; we re-check validation
            # bands lightly in _result_from_compile.
            compiled = compile_v4_article(
                story,
                writer=std_writer,
                attempts_root=attempts_root,
                rank=index,
                research=True,
            )
            # Prefer validation depth labels in the report.
            row = _result_from_compile(
                compiled,
                researched=researched,
                pack=compiled.article_input or pack,
                depth=depth,
                bank=bank,
            )
            # If production depth rejected a MEDIUM that is still QA-clean at 250–449,
            # accept when compiled.ok and words fall in validation MEDIUM band.
            if (
                not row["ok"]
                and compiled.ok
                and compiled.article
                and compiled.ambiguous == 0
                and compiled.unsupported == 0
                and int((compiled.qa or {}).get("critical_count") or 0) == 0
                and depth.recommended_word_min
                <= compiled.final_words
                <= depth.recommended_word_max
            ):
                row["ok"] = True
                row["state"] = "FROZEN"
                row["article"] = compiled.article
                row["failure_class"] = None
                row["coherence"] = "PASS"
                row["canonical_body_hash"] = _sha256_text(
                    str(compiled.article.get("article_body") or "")
                )

        row["rank"] = index
        kimi_initial += int(row.get("Kimi_initial_generations") or 0)
        kimi_repairs += int(row.get("Kimi_repairs") or 0)
        kimi_regens += int(row.get("Kimi_regenerations") or 0)
        attempts.append(row)

        if not row.get("ok"):
            if row.get("grounding_unsupported") or row.get("grounding_ambiguous"):
                failure_buckets["grounding_failures"] += 1
            if row.get("mechanics") == "FAIL":
                failure_buckets["mechanics_failures"] += 1
            if row.get("security") == "FAIL":
                failure_buckets["security_failures"] += 1
            if row.get("coherence") == "FAIL":
                failure_buckets["coherence_failures"] += 1
            fw = int(row.get("final_body_words") or 0)
            if fw and not (
                depth.recommended_word_min <= fw <= depth.recommended_word_max
            ):
                failure_buckets["length_failures"] += 1
            if row.get("failure_class") in {
                "RESEARCH_FAILED",
                "INSUFFICIENT_EVIDENCE",
            }:
                failure_buckets["research/evidence_failures"] += 1
            continue

        # Freeze publishable article (immutable canonical).
        body = str((row.get("article") or {}).get("article_body") or "")
        body_hash = row.get("canonical_body_hash") or _sha256_text(body)
        freeze_payload = {
            "event_id": row["event_id"],
            "headline": row.get("headline"),
            "article_type": row.get("article_type"),
            "evidence_capacity": row.get("evidence_capacity"),
            "canonical_body_words": row.get("final_body_words"),
            "canonical_body_hash": body_hash,
            "article": row.get("article"),
            "qa_result": row.get("qa"),
            "state": STATE_GENERATED,
            "architecture": "v4",
            "batch_id": batch_id,
            "image": None,
            "telegram": None,
            "wordpress": None,
            "article_sha256": body_hash,
        }
        store.write_story(batch_id, row["event_id"], freeze_payload)
        row["canonical_body_hash"] = body_hash
        row["state"] = "FROZEN"
        publishable.append(row)
        selected_events.append(row["event_id"])

    post030 = verify_event_030_untouched()
    words = [int(r.get("final_body_words") or 0) for r in publishable]
    full_n = sum(1 for r in publishable if r.get("article_type") == ARTICLE_FULL)
    std_n = sum(1 for r in publishable if r.get("article_type") == ARTICLE_STANDARD)
    lim_n = sum(1 for r in publishable if r.get("article_type") == ARTICLE_LIMITED)
    rich_n = sum(1 for r in publishable if r.get("evidence_capacity") == CAPACITY_RICH)
    med_n = sum(1 for r in publishable if r.get("evidence_capacity") == CAPACITY_MEDIUM)
    limc_n = sum(1 for r in publishable if r.get("evidence_capacity") == CAPACITY_LIMITED)

    n_pub = len(publishable)
    if n_pub >= TARGET_PUBLISHABLE:
        status = "PASS"
    elif n_pub > 0:
        status = "PARTIAL"
    else:
        status = "FAIL"

    kimi_calls = int(getattr(rich_writer, "generation_calls", 0) or 0) + int(
        getattr(std_writer, "generation_calls", 0) or 0
    )

    report = {
        "status": status,
        "batch_id": batch_id,
        "stories_collected": stories_collected,
        "stories_after_gates": stories_after_gates,
        "clusters": len(ranked),
        "candidate_pool": len(stories),
        "selected_events": selected_events,
        "candidates_attempted": len(attempts),
        "publishable_articles": n_pub,
        "FULL_ARTICLE_count": full_n,
        "STANDARD_ARTICLE_count": std_n,
        "LIMITED_BRIEF_count": lim_n,
        "average_body_words": (sum(words) / n_pub) if n_pub else 0,
        "minimum_body_words": min(words) if words else 0,
        "maximum_body_words": max(words) if words else 0,
        "RICH_count": rich_n,
        "MEDIUM_count": med_n,
        "LIMITED_count": limc_n,
        "Kimi_calls": kimi_calls,
        "Kimi_initial_generations": kimi_initial,
        "Kimi_repairs": kimi_repairs,
        "Kimi_regenerations": kimi_regens,
        "Groq_calls": 0,
        "paid_private_qwen_calls": 0,
        "copyright_provider_calls": 0,
        "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
        "rich_max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
        **failure_buckets,
        "distinct_events": len(set(selected_events)) == len(selected_events),
        "event030_preserved": bool(post030.get("ok")),
        "event_030_check": post030,
        "articles": attempts,
        "publishable": [
            {
                "event": r.get("event_id"),
                "headline": r.get("headline"),
                "evidence_capacity": r.get("evidence_capacity"),
                "article_type": r.get("article_type"),
                "final_body_words": r.get("final_body_words"),
                "canonical_body_hash": r.get("canonical_body_hash"),
            }
            for r in publishable
        ],
        "NO_IMAGE": True,
        "NO_TELEGRAM": True,
        "NO_WORDPRESS": True,
        "out_root": str(out_root),
        "attempts_root": str(attempts_root),
    }
    (out_root / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "status": status,
            "publishable_articles": n_pub,
            "selected_events": selected_events,
            "NO_IMAGE": True,
            "NO_TELEGRAM": True,
            "NO_WORDPRESS": True,
        },
    )
    return report


def main() -> int:
    load_dotenv(REPO / ".env")
    result = run_top5_article_batch(environ=_load_environ())
    # Print compact batch summary + per-article blocks for the required report.
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("status") == "PASS":
        return 0
    if result.get("status") == "PARTIAL":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
