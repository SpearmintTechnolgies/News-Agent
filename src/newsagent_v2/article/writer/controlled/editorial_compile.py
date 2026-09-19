"""Supplemental plan from unused authorized facts. Max one extra generation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import active_normal_depth_policy
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.controlled.failures import classify_qa_failure
from newsagent_v2.article.writer.controlled.paragraph import (
    ParagraphValidation,
    aggregate_quarantine_telemetry,
    validate_paragraph,
)
from newsagent_v2.article.writer.controlled.pipeline import (
    CompileResult,
    compile_controlled_article,
    _quarantine_unit_diagnostic,
    _sanitize_writer_diagnostic,
)
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, plan_article
from newsagent_v2.article.writer.controlled.renderer import ProseRenderer
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers

MAX_SUPPLEMENTAL_GENERATIONS = 1


def unused_authorized_fact_ids(plan: ArticlePlan, validations: list[ParagraphValidation]) -> tuple[str, ...]:
    used: set[str] = set()
    for row in validations:
        used.update(row.mapped_claim_ids)
        for unit in row.units:
            if unit.retained:
                used.update(unit.claim_ids)
    return tuple(cid for cid in plan.selected_claim_ids if cid not in used)


def _below_minimum(qa: dict[str, Any] | None) -> bool:
    if not isinstance(qa, dict):
        return False
    codes = [str(item.get("code") or "") for item in (qa.get("critical_failures") or []) if isinstance(item, dict)]
    return "below_article_minimum_length" in codes


def compile_editorial_article(
    story: dict[str, Any],
    *,
    renderer: ProseRenderer,
    image_failed: bool = False,
) -> CompileResult:
    first = compile_controlled_article(story, renderer=renderer, image_failed=image_failed)
    if first.ok or image_failed:
        first.notes = (first.notes + " supplemental=0").strip()
        return first
    words = word_count(str((first.article or {}).get("article_body") or ""))
    # Align supplemental trigger with active QA floor (incl. TEMPORARY/DEMO ARTICLE_MIN_WORDS).
    length_floor = active_normal_depth_policy().hard_minimum_words
    if not _below_minimum(first.qa) and not (first.article and words < length_floor):
        return first
    if first.plan is None:
        return first
    unused = unused_authorized_fact_ids(first.plan, first.paragraph_validations)
    if not unused:
        first.notes = (first.notes + " supplemental=skipped_no_unused_facts").strip()
        return first
    raw = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    pack = article_input_for_ledgers(raw)
    full = build_evidence_ledgers(pack)
    subset = replace(full, claims=tuple(row for row in full.claims if row.claim_id in set(unused)))
    if not subset.claims:
        return first
    supp_plan = plan_article(subset, pack)
    if not supp_plan.paragraph_plans:
        return first
    rendered = renderer.render(supp_plan, full)
    if rendered.provider_error or rendered.invalid_output or not rendered.ok:
        first.notes = (first.notes + " supplemental=provider_or_invalid").strip()
        first.diagnostic = {
            **(first.diagnostic or {}),
            "supplemental_error": rendered.error,
            "supplemental_native": _sanitize_writer_diagnostic(
                rendered.native, error=rendered.error or "supplemental_invalid"
            ).get("native"),
        }
        return first
    extra_validations: list[ParagraphValidation] = []
    by_id = {row.paragraph_id: row for row in rendered.paragraphs}
    for para in supp_plan.paragraph_plans:
        item = by_id.get(para.paragraph_id)
        if item is None:
            continue
        extra_validations.append(
            validate_paragraph(
                item,
                para,
                full,
                article_claim_ids=set(first.plan.selected_claim_ids) if first.plan else set(supp_plan.selected_claim_ids),
            )
        )
    merged = list(first.paragraph_validations) + extra_validations
    if not any(row.assemblable for row in extra_validations):
        first.notes = (first.notes + " supplemental=no_safe_material").strip()
        return first
    article = assemble_canonical_article(
        plan=first.plan,
        ledgers=full,
        article_input=pack,
        validated=merged,
        headline_text=str((first.article or {}).get("headline") or rendered.headline_text),
        dek_text=str((first.article or {}).get("dek") or rendered.dek_text),
        seo_title=rendered.seo_title,
        meta_description=rendered.meta_description,
        slug=rendered.slug,
        entities=rendered.entities,
        keywords=rendered.keywords,
    )
    qa = run_article_qa(article, pack, article_mode="normal")
    quarantine_stats = aggregate_quarantine_telemetry(merged)
    unit_diag = _quarantine_unit_diagnostic(merged)
    native_diag = _sanitize_writer_diagnostic(rendered.native) if rendered.native else {}
    if qa.get("qa_passed"):
        return CompileResult(
            ok=True,
            event_id=first.event_id,
            capacity=first.capacity,
            plan=first.plan,
            paragraph_validations=merged,
            article=article,
            qa=qa,
            render_attempts=first.render_attempts + 1,
            notes="supplemental=1",
            quarantine=quarantine_stats,
            diagnostic={**native_diag, "quarantine_units": unit_diag, "quarantine": quarantine_stats},
            writer_feasible=first.writer_feasible,
            capacity_advisory=first.capacity_advisory,
        )
    failure = classify_qa_failure(qa)
    return CompileResult(
        ok=False,
        event_id=first.event_id,
        failure_class=failure,
        capacity=first.capacity,
        plan=first.plan,
        paragraph_validations=merged,
        article=article,
        qa=qa,
        render_attempts=first.render_attempts + 1,
        notes="supplemental=1 still_failed",
        quarantine=quarantine_stats,
        diagnostic={**native_diag, "quarantine_units": unit_diag, "quarantine": quarantine_stats},
        writer_feasible=first.writer_feasible,
        capacity_advisory=first.capacity_advisory,
    )
