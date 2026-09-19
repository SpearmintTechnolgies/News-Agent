"""Controlled Writer V3 compile + bounded candidate recovery. No live providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.writer.canonical import CANONICAL_ARTICLE_FIELDS
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, build_evidence_ledgers
from newsagent_v2.article.writer.controlled.assembler import (
    assemble_canonical_article,
    canonical_field_report,
    normalize_seo_fields,
)
from newsagent_v2.article.writer.controlled.capacity import (
    CLASS_BORDERLINE,
    CLASS_INSUFFICIENT,
    CLASS_SUFFICIENT,
    EvidenceCapacity,
    analyze_evidence_capacity,
    article_input_for_ledgers,
    refine_capacity_with_plan,
)
from newsagent_v2.article.writer.controlled.config import ControlledWriterConfig, resolve_controlled_config
from newsagent_v2.article.writer.controlled.failures import (
    COPYRIGHT_SIMILARITY_FAILED,
    GROUNDING_FAILED,
    IMAGE_FAILED,
    INSUFFICIENT_EVIDENCE,
    QUOTE_GROUNDING_FAILED,
    SEO_ONLY_FAILED,
    WRITER_OUTPUT_INVALID,
    WRITER_PROVIDER_BUDGET_EXCEEDED,
    WRITER_PROVIDER_ERROR,
    classify_qa_failure,
    recovery_policy,
)
from newsagent_v2.article.writer.controlled.paragraph import (
    INVENTED_QUOTE,
    OUTCOME_FULLY_REJECTED,
    UNAUTHORIZED_QUOTE,
    ParagraphValidation,
    aggregate_quarantine_telemetry,
    validate_paragraph,
)
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, plan_article
from newsagent_v2.article.writer.controlled.renderer import (
    FakeProseRenderer,
    ProseRenderer,
    RendererResult,
)
from newsagent_v2.article.writer.controlled.writer_feasibility import (
    EDITORIAL_TARGET_MAX_WORDS,
    EDITORIAL_TARGET_MIN_WORDS,
    WRITER_INFEASIBLE,
    evaluate_writer_feasibility,
)

CAPACITY_ATTEMPT_ORDER = {
    CLASS_SUFFICIENT: 0,
    CLASS_BORDERLINE: 1,
    CLASS_INSUFFICIENT: 2,
}


def story_capacity_class(story: dict[str, Any], config: ControlledWriterConfig | None = None) -> str:
    if story.get("capacity_class") in {CLASS_SUFFICIENT, CLASS_BORDERLINE, CLASS_INSUFFICIENT}:
        return str(story["capacity_class"])
    cfg = config or resolve_controlled_config()
    raw = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    pack = article_input_for_ledgers(raw)
    ledgers = build_evidence_ledgers(pack)
    cap = analyze_evidence_capacity(
        ledgers,
        hard_minimum_words=cfg.hard_minimum_words,
        safety_margin_words=cfg.capacity_safety_margin_words,
    )
    return cap.capacity_class


@dataclass
class FrozenStoryBundle:
    event_id: str
    article: dict[str, Any]
    qa: dict[str, Any]
    ledgers: EvidenceLedgers
    plan: ArticlePlan
    capacity: EvidenceCapacity
    article_input: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "article": self.article,
            "qa": self.qa,
            "capacity": self.capacity.as_dict(),
            "plan": self.plan.as_dict(),
            "canonical_fields": list(CANONICAL_ARTICLE_FIELDS),
        }


@dataclass
class CandidateAttempt:
    event_id: str
    rank: int
    failure_class: str | None = None
    qa_passed: bool = False
    publishable: bool = False
    eligible_for_deeper_evidence: bool = False
    notes: str = ""
    qa: dict[str, Any] | None = None
    bundle: FrozenStoryBundle | None = None
    render_attempts: int = 0
    seo_normalize_attempts: int = 0


@dataclass
class CompileResult:
    ok: bool
    event_id: str
    failure_class: str | None = None
    capacity: EvidenceCapacity | None = None
    plan: ArticlePlan | None = None
    paragraph_validations: list[ParagraphValidation] = field(default_factory=list)
    article: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    bundle: FrozenStoryBundle | None = None
    eligible_for_deeper_evidence: bool = False
    render_attempts: int = 0
    seo_normalize_attempts: int = 0
    notes: str = ""
    quarantine: dict[str, int] = field(default_factory=dict)
    diagnostic: dict[str, Any] = field(default_factory=dict)
    writer_feasible: bool | None = None
    capacity_advisory: dict[str, Any] = field(default_factory=dict)


def _classify_paragraph_failures(validations: list[ParagraphValidation]) -> str | None:
    codes = [item["code"] for row in validations for item in row.issues]
    if not codes:
        return None
    if any(code in {INVENTED_QUOTE, UNAUTHORIZED_QUOTE} for code in codes) and not any(
        row.ok for row in validations
    ):
        return QUOTE_GROUNDING_FAILED
    if any(code in {INVENTED_QUOTE, UNAUTHORIZED_QUOTE} for code in codes) and not any(
        row.ok and row.text.strip() for row in validations
    ):
        return QUOTE_GROUNDING_FAILED
    return GROUNDING_FAILED


def _sanitize_writer_diagnostic(payload: Any, *, error: str | None = None) -> dict[str, Any]:
    """Persistable structural diagnostic. Never includes secrets."""
    out: dict[str, Any] = {"error": error}
    if not isinstance(payload, dict):
        out["native_type"] = type(payload).__name__
        return out
    paragraphs = payload.get("paragraphs") if isinstance(payload.get("paragraphs"), list) else []
    compact_paras: list[dict[str, Any]] = []
    for para in paragraphs[:20]:
        if not isinstance(para, dict):
            continue
        sentences = para.get("sentences") if isinstance(para.get("sentences"), list) else []
        compact_sents = []
        for sent in sentences[:12]:
            if not isinstance(sent, dict):
                continue
            compact_sents.append(
                {
                    "sentence_id": sent.get("sentence_id"),
                    "text": str(sent.get("text") or "")[:400],
                    "fact_ids_used": list(sent.get("fact_ids_used") or [])[:20],
                    "quote_ids_used": list(sent.get("quote_ids_used") or [])[:20],
                }
            )
        compact_paras.append(
            {
                "paragraph_id": para.get("paragraph_id"),
                "sentences": compact_sents,
                "text": str(para.get("text") or "")[:400] if not compact_sents else None,
            }
        )
    out["native"] = {
        "headline": str(payload.get("headline") or "")[:240],
        "dek": str(payload.get("dek") or "")[:400],
        "paragraph_count": len(paragraphs),
        "paragraphs": compact_paras,
    }
    return out


def _quarantine_unit_diagnostic(validations: list[ParagraphValidation]) -> dict[str, Any]:
    """Aggregate retained vs quarantined words by reason. Diagnostic only."""
    from newsagent_v2.article.qa.textutil import word_count
    from newsagent_v2.article.writer.controlled.realization import REALIZATION_INVALID

    buckets = {
        "accepted": 0,
        "quarantined_unknown_fact": 0,
        "quarantined_relationship": 0,
        "quarantined_semantic_mismatch": 0,
        "quarantined_attribution": 0,
        "quarantined_modal_or_polarity": 0,
        "quarantined_duplicate": 0,
        "quarantined_schema": 0,
        "quarantined_other": 0,
    }
    units_out: list[dict[str, Any]] = []
    for row in validations:
        for unit in row.units:
            words = word_count(unit.text)
            entry = {
                "paragraph_id": row.paragraph_id,
                "text": unit.text[:400],
                "status": unit.status,
                "issue_code": unit.issue_code,
                "words": words,
                "retained": unit.retained,
            }
            units_out.append(entry)
            if unit.retained:
                buckets["accepted"] += words
                continue
            code = str(unit.issue_code or unit.status or "").lower()
            msg = str(unit.issue_message or "").lower()
            if "unknown" in code or code == "unknown_fact_id":
                buckets["quarantined_unknown_fact"] += words
            elif "relationship" in code or "unauthorized_fact_relationship" in code:
                buckets["quarantined_relationship"] += words
            elif "polarity" in msg or "modal" in msg:
                buckets["quarantined_modal_or_polarity"] += words
            elif "attribution" in msg:
                buckets["quarantined_attribution"] += words
            elif "duplicate" in msg:
                buckets["quarantined_duplicate"] += words
            elif code in {REALIZATION_INVALID.lower(), "realization_invalid"} or "slot" in msg:
                buckets["quarantined_schema"] += words
            elif any(
                token in code
                for token in (
                    "unsupported",
                    "ambiguous",
                    "inseparable",
                    "unauthorized",
                )
            ):
                buckets["quarantined_semantic_mismatch"] += words
            else:
                buckets["quarantined_other"] += words
    return {"buckets": buckets, "units": units_out[:80]}


def compile_controlled_article(
    story: dict[str, Any],
    *,
    renderer: ProseRenderer | None = None,
    config: ControlledWriterConfig | None = None,
    image_failed: bool = False,
) -> CompileResult:
    cfg = config or resolve_controlled_config()
    renderer = renderer or FakeProseRenderer()
    event_id = str(story.get("event_id") or "")
    raw_input = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    if not event_id:
        event_id = str(raw_input.get("event_id") or "")
    pack = article_input_for_ledgers(raw_input)
    ledgers = build_evidence_ledgers(pack)
    capacity = analyze_evidence_capacity(
        ledgers,
        hard_minimum_words=cfg.hard_minimum_words,
        safety_margin_words=cfg.capacity_safety_margin_words,
    )
    capacity_advisory = {
        "capacity_class": capacity.capacity_class,
        "sufficient_for_article": capacity.sufficient_for_article,
        "estimated_safe_word_range": list(capacity.estimated_safe_word_range),
        "reason": capacity.reason,
    }
    # Production admission is authoritative when assess already decided.
    admitted = story.get("_writer_feasible")
    if admitted is True:
        feasibility = {"writer_feasible": True, "status": "WRITER_FEASIBLE", "reasons": [], "honored_admission": True}
    elif admitted is False:
        return CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=WRITER_INFEASIBLE,
            capacity=capacity,
            eligible_for_deeper_evidence=True,
            writer_feasible=False,
            capacity_advisory=capacity_advisory,
            notes="Writer infeasible: assess_admission_false",
        )
    else:
        feasibility = evaluate_writer_feasibility(
            article_input=raw_input,
            ledgers=ledgers,
            capacity=capacity,
        )
        if not feasibility.get("writer_feasible"):
            reasons = [str(item) for item in (feasibility.get("reasons") or [])]
            return CompileResult(
                ok=False,
                event_id=event_id,
                failure_class=WRITER_INFEASIBLE,
                capacity=capacity,
                eligible_for_deeper_evidence=True,
                writer_feasible=False,
                capacity_advisory=capacity_advisory,
                notes="Writer infeasible: " + (",".join(reasons) if reasons else "thin evidence"),
            )
    # Legacy capacity_class / sufficient_for_article / planned_safe floors are advisory only.
    # They must not override WRITER_FEASIBLE admission.

    plan = plan_article(
        ledgers,
        pack,
        hard_minimum_words=cfg.hard_minimum_words,
        target_min_words=EDITORIAL_TARGET_MIN_WORDS,
        target_max_words=EDITORIAL_TARGET_MAX_WORDS,
    )
    capacity = refine_capacity_with_plan(
        capacity,
        planned_safe_words=plan.planned_safe_words,
        minimum_surviving_words=plan.minimum_surviving_words,
        paragraph_loss_tolerance=plan.paragraph_loss_tolerance,
        hard_minimum_words=cfg.hard_minimum_words,
    )
    capacity_advisory = {
        "capacity_class": capacity.capacity_class,
        "sufficient_for_article": capacity.sufficient_for_article,
        "estimated_safe_word_range": list(capacity.estimated_safe_word_range),
        "planned_safe_words": plan.planned_safe_words,
        "minimum_surviving_words": plan.minimum_surviving_words,
        "reason": capacity.reason,
    }
    allowed_plan_ids = {cid for para in plan.paragraph_plans for cid in para.allowed_claim_ids}
    ledger_ids = {row.claim_id for row in ledgers.claims}
    if not allowed_plan_ids <= ledger_ids:
        return CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=WRITER_OUTPUT_INVALID,
            capacity=capacity,
            plan=plan,
            writer_feasible=True,
            capacity_advisory=capacity_advisory,
            notes="planner emitted claim ids outside EvidenceClaimLedger",
        )

    render_attempts = 0
    last_render: RendererResult | None = None
    validations: list[ParagraphValidation] = []
    while render_attempts < cfg.max_render_attempts:
        render_attempts += 1
        last_render = renderer.render(plan, ledgers)
        if last_render.budget_exceeded or (
            last_render.provider_error and "BUDGET" in str(last_render.error or "").upper()
        ):
            return CompileResult(
                ok=False,
                event_id=event_id,
                failure_class=WRITER_PROVIDER_BUDGET_EXCEEDED,
                capacity=capacity,
                plan=plan,
                render_attempts=render_attempts,
                writer_feasible=True,
                capacity_advisory=capacity_advisory,
                notes=last_render.error or "writer provider budget exceeded",
            )
        if last_render.provider_error:
            return CompileResult(
                ok=False,
                event_id=event_id,
                failure_class=WRITER_PROVIDER_ERROR,
                capacity=capacity,
                plan=plan,
                render_attempts=render_attempts,
                writer_feasible=True,
                capacity_advisory=capacity_advisory,
                notes=last_render.error or "writer provider error",
            )
        if last_render.invalid_output or not last_render.ok:
            return CompileResult(
                ok=False,
                event_id=event_id,
                failure_class=WRITER_OUTPUT_INVALID,
                capacity=capacity,
                plan=plan,
                render_attempts=render_attempts,
                writer_feasible=True,
                capacity_advisory=capacity_advisory,
                notes=last_render.error or "writer output invalid",
                diagnostic=_sanitize_writer_diagnostic(
                    last_render.native,
                    error=last_render.error or "writer output invalid",
                ),
            )
        by_id = {row.paragraph_id: row for row in last_render.paragraphs}
        validations = []
        for para in plan.paragraph_plans:
            rendered = by_id.get(para.paragraph_id)
            if rendered is None:
                validations.append(
                    ParagraphValidation(
                        paragraph_id=para.paragraph_id,
                        ok=False,
                        text="",
                        issues=[{"code": "missing_paragraph", "message": "renderer omitted paragraph"}],
                        outcome=OUTCOME_FULLY_REJECTED,
                    )
                )
                continue
            validations.append(
                validate_paragraph(
                    rendered,
                    para,
                    ledgers,
                    article_claim_ids=set(plan.selected_claim_ids),
                )
            )
        if any(row.assemblable for row in validations):
            break

    if last_render is None:
        return CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=WRITER_OUTPUT_INVALID,
            capacity=capacity,
            plan=plan,
            render_attempts=render_attempts,
            writer_feasible=True,
            capacity_advisory=capacity_advisory,
        )

    para_failure = _classify_paragraph_failures(validations)
    quarantine_stats = aggregate_quarantine_telemetry(validations)
    native_diag = _sanitize_writer_diagnostic(last_render.native) if last_render.native else {}
    unit_diag = _quarantine_unit_diagnostic(validations)
    if not any(row.assemblable for row in validations):
        return CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=para_failure or GROUNDING_FAILED,
            capacity=capacity,
            plan=plan,
            paragraph_validations=validations,
            render_attempts=render_attempts,
            notes="no validated paragraphs available for assembly",
            quarantine=quarantine_stats,
            diagnostic={**native_diag, "quarantine_units": unit_diag},
            writer_feasible=True,
            capacity_advisory=capacity_advisory,
        )

    article = assemble_canonical_article(
        plan=plan,
        ledgers=ledgers,
        article_input=pack,
        validated=validations,
        headline_text=last_render.headline_text,
        dek_text=last_render.dek_text,
        seo_title=last_render.seo_title,
        meta_description=last_render.meta_description,
        slug=last_render.slug,
        entities=last_render.entities,
        keywords=last_render.keywords,
    )
    if image_failed:
        return CompileResult(
            ok=False,
            event_id=event_id,
            failure_class=IMAGE_FAILED,
            capacity=capacity,
            plan=plan,
            paragraph_validations=validations,
            article=article,
            render_attempts=render_attempts,
            notes="image stage failed; article architecture is unchanged",
            quarantine=quarantine_stats,
            writer_feasible=True,
            capacity_advisory=capacity_advisory,
        )

    seo_attempts = 0
    qa = run_article_qa(article, pack, article_mode="normal")
    if not qa.get("qa_passed") and classify_qa_failure(qa) == SEO_ONLY_FAILED:
        policy = recovery_policy(SEO_ONLY_FAILED)
        if policy.get("seo_normalize") and seo_attempts < cfg.max_seo_normalize_attempts:
            seo_attempts += 1
            article = normalize_seo_fields(article)
            qa = run_article_qa(article, pack, article_mode="normal")

    if qa.get("qa_passed"):
        bundle = FrozenStoryBundle(
            event_id=event_id,
            article=article,
            qa=qa,
            ledgers=ledgers,
            plan=plan,
            capacity=capacity,
            article_input=pack,
        )
        return CompileResult(
            ok=True,
            event_id=event_id,
            capacity=capacity,
            plan=plan,
            paragraph_validations=validations,
            article=article,
            qa=qa,
            bundle=bundle,
            render_attempts=render_attempts,
            seo_normalize_attempts=seo_attempts,
            quarantine=quarantine_stats,
            writer_feasible=True,
            capacity_advisory=capacity_advisory,
        )

    failure = classify_qa_failure(qa)
    if para_failure == QUOTE_GROUNDING_FAILED and failure != COPYRIGHT_SIMILARITY_FAILED:
        failure = QUOTE_GROUNDING_FAILED
    policy = recovery_policy(failure)
    return CompileResult(
        ok=False,
        event_id=event_id,
        failure_class=failure,
        capacity=capacity,
        plan=plan,
        paragraph_validations=validations,
        article=article,
        qa=qa,
        eligible_for_deeper_evidence=bool(policy.get("eligible_for_deeper_evidence")),
        render_attempts=render_attempts,
        seo_normalize_attempts=seo_attempts,
        notes=str(policy.get("note") or ""),
        quarantine=quarantine_stats,
        diagnostic={**native_diag, "quarantine_units": unit_diag, "quarantine": quarantine_stats},
        writer_feasible=True,
        capacity_advisory=capacity_advisory,
    )


def collect_publishable_stories(
    stories: list[dict[str, Any]],
    *,
    compile_fn: Callable[[dict[str, Any]], CompileResult] | None = None,
    config: ControlledWriterConfig | None = None,
    renderer: ProseRenderer | None = None,
) -> dict[str, Any]:
    """Attempt ranked candidates until target publishable count or attempt budget."""
    cfg = config or resolve_controlled_config()
    compile_one = compile_fn or (
        lambda story: compile_controlled_article(story, renderer=renderer, config=cfg)
    )
    ranked = list(enumerate(stories, start=1))
    ranked.sort(
        key=lambda pair: (
            CAPACITY_ATTEMPT_ORDER.get(story_capacity_class(pair[1], cfg), 9),
            pair[0],
        )
    )
    attempts: list[CandidateAttempt] = []
    bundles: list[FrozenStoryBundle] = []
    scanned = 0
    for original_rank, story in ranked:
        if len(bundles) >= cfg.target_publishable_count:
            break
        if scanned >= cfg.max_candidate_attempts:
            break
        scanned += 1
        event_id = str(story.get("event_id") or "")
        compiled = compile_one(story)
        attempt = CandidateAttempt(
            event_id=compiled.event_id or event_id,
            rank=original_rank,
            failure_class=compiled.failure_class,
            qa_passed=bool(compiled.qa and compiled.qa.get("qa_passed")),
            publishable=compiled.ok,
            eligible_for_deeper_evidence=compiled.eligible_for_deeper_evidence,
            notes=compiled.notes,
            qa=compiled.qa,
            bundle=compiled.bundle,
            render_attempts=compiled.render_attempts,
            seo_normalize_attempts=compiled.seo_normalize_attempts,
        )
        attempts.append(attempt)
        if compiled.ok and compiled.bundle is not None:
            bundles.append(compiled.bundle)

    publishable = len(bundles)
    met_minimum = publishable >= cfg.min_desired_publishable_count
    return {
        "publishable_count": publishable,
        "target_publishable_count": cfg.target_publishable_count,
        "min_desired_publishable_count": cfg.min_desired_publishable_count,
        "max_candidate_attempts": cfg.max_candidate_attempts,
        "candidate_attempts": scanned,
        "met_minimum_desired": met_minimum,
        "qa_weakened": False,
        "stopped_reason": (
            "target_reached"
            if publishable >= cfg.target_publishable_count
            else "max_attempts"
            if scanned >= cfg.max_candidate_attempts or scanned >= len(stories)
            else "pool_exhausted"
        ),
        "failure_summary": [
            {
                "event_id": row.event_id,
                "rank": row.rank,
                "failure_class": row.failure_class,
                "qa_passed": row.qa_passed,
                "eligible_for_deeper_evidence": row.eligible_for_deeper_evidence,
            }
            for row in attempts
            if not row.publishable
        ],
        "attempts": attempts,
        "bundles": bundles,
        "canonical_ok": all(
            canonical_field_report(bundle.article)["ok"] for bundle in bundles
        ),
    }
