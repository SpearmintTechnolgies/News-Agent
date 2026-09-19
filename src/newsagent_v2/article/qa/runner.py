"""Run deterministic article QA. Does not call a model."""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.qa.claims import check_claims
from newsagent_v2.article.qa.editorial_cleanliness import check_editorial_cleanliness
from newsagent_v2.article.qa.grounding import check_grounding
from newsagent_v2.article.qa.headline import check_headline
from newsagent_v2.article.qa.isolation import check_cross_story_contamination
from newsagent_v2.article.qa.mechanics import check_mechanics
from newsagent_v2.article.qa.policy import (
    ArticleDepthPolicy,
    check_depth,
    resolve_depth_policy,
)
from newsagent_v2.article.qa.result import build_qa_result, empty_metrics
from newsagent_v2.article.qa.safety import check_safety
from newsagent_v2.article.qa.schema import check_schema
from newsagent_v2.article.qa.seo import check_seo
from newsagent_v2.article.qa.similarity import check_similarity
from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.qa.structure import check_structure
from newsagent_v2.article.render import materialize_article


def run_article_qa(
    article: Any,
    article_input: dict[str, Any],
    *,
    article_mode: str | None = None,
    depth_policy: ArticleDepthPolicy | None = None,
    other_article_inputs: list[dict[str, Any]] | None = None,
    skip_copyright_similarity: bool = False,
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    metrics = empty_metrics()

    if isinstance(article, dict):
        expand_provider_article(
            article,
            article_input,
            other_article_inputs=other_article_inputs,
        )
        materialize_article(article)

    schema_issues = check_schema(article, article_input)
    issues.extend(schema_issues)
    event_id = article.get("event_id") if isinstance(article, dict) else None
    if any(item["severity"] == "critical" and item["code"] in {
        "schema_not_object",
        "missing_field",
        "invalid_type",
    } for item in schema_issues):
        return build_qa_result(event_id=event_id, issues=issues, metrics=metrics)

    if not isinstance(article, dict):
        return build_qa_result(event_id=None, issues=issues, metrics=metrics)

    issues.extend(check_structure(article, article_input, other_article_inputs=other_article_inputs))

    mech_issues, mech_metrics = check_mechanics(article)
    issues.extend(mech_issues)
    metrics.update(mech_metrics)

    policy = resolve_depth_policy(
        article_mode=article_mode,
        depth_policy=depth_policy,
    )
    depth_issues, depth_metrics = check_depth(article, article_input, policy)
    issues.extend(depth_issues)
    metrics.update(depth_metrics)

    issues.extend(check_headline(article, article_input))

    claim_issues, claim_metrics = check_claims(article, article_input)
    issues.extend(claim_issues)
    metrics.update(claim_metrics)

    ground_issues, ground_metrics = check_grounding(
        article,
        article_input,
        other_article_inputs=other_article_inputs,
    )
    issues.extend(ground_issues)
    metrics.update(ground_metrics)

    # V4: copyright similarity is not a publishability gate.
    if not skip_copyright_similarity:
        sim_issues, sim_metrics = check_similarity(article, article_input)
        issues.extend(sim_issues)
        metrics.update(sim_metrics)
    else:
        metrics.setdefault("exact_overlap_count", 0)
        metrics.setdefault("exact_overlap_ngram_hits", 0)
        metrics.setdefault("max_similarity", 0.0)
        metrics["copyright_similarity_skipped"] = True

    issues.extend(check_cross_story_contamination(article, article_input, other_article_inputs))

    seo_issues, seo_count = check_seo(article)
    issues.extend(seo_issues)
    metrics["seo_issue_count"] = seo_count

    safety_issues, safety_count = check_safety(article, article_input)
    issues.extend(safety_issues)
    metrics["publishing_safety_issue_count"] = safety_count

    # Narrow post-generation contamination gate. No LLM. No auto-repair.
    clean_issues, clean_metrics = check_editorial_cleanliness(article)
    issues.extend(clean_issues)
    metrics.update(clean_metrics)

    return build_qa_result(event_id=event_id, issues=issues, metrics=metrics)
