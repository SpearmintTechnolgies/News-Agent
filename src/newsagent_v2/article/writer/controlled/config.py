"""Controlled Writer V3 configuration. Does not weaken QA."""

from __future__ import annotations

from dataclasses import dataclass

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.batch.contract import MAKE_STORY_COUNT, TOP5_COUNT
from newsagent_v2.batch.viability import DEFAULT_CANDIDATE_SCAN_LIMIT, resolve_candidate_scan_limit

MIN_DESIRED_PUBLISHABLE_COUNT = 3
MAX_RENDER_ATTEMPTS = 1
MAX_SEO_NORMALIZE_ATTEMPTS = 1
MAX_DEEPER_EVIDENCE_STAGES = 0
LEDGER_EVIDENCE_WORD_BUDGET = 4000
# Extra planned words above the QA hard floor so one dropped paragraph need not sink the article.
CAPACITY_SAFETY_MARGIN_WORDS = 80


@dataclass(frozen=True)
class ControlledWriterConfig:
    """Publishable-count goals never override QA or grounding."""

    make_story_count: int = MAKE_STORY_COUNT
    target_publishable_count: int = MAKE_STORY_COUNT
    min_desired_publishable_count: int = MIN_DESIRED_PUBLISHABLE_COUNT
    max_candidate_attempts: int = DEFAULT_CANDIDATE_SCAN_LIMIT
    max_render_attempts: int = MAX_RENDER_ATTEMPTS
    max_seo_normalize_attempts: int = MAX_SEO_NORMALIZE_ATTEMPTS
    enable_deeper_evidence: bool = False
    hard_minimum_words: int = NORMAL_ARTICLE_POLICY.hard_minimum_words
    target_min_words: int = NORMAL_ARTICLE_POLICY.target_min_words
    target_max_words: int = NORMAL_ARTICLE_POLICY.target_max_words
    capacity_safety_margin_words: int = CAPACITY_SAFETY_MARGIN_WORDS

    def __post_init__(self) -> None:
        if self.max_render_attempts < 1:
            raise ValueError("max_render_attempts must be >= 1")
        if self.max_seo_normalize_attempts < 0:
            raise ValueError("max_seo_normalize_attempts must be >= 0")
        if self.max_candidate_attempts < 1:
            raise ValueError("max_candidate_attempts must be >= 1")
        if self.target_publishable_count < 1:
            raise ValueError("target_publishable_count must be >= 1")
        if self.min_desired_publishable_count < 1:
            raise ValueError("min_desired_publishable_count must be >= 1")


def resolve_controlled_config(
    *,
    target_publishable_count: int | None = None,
    max_candidate_attempts: int | None = None,
    environ: dict[str, str] | None = None,
    top5: bool = False,
) -> ControlledWriterConfig:
    target = TOP5_COUNT if top5 else (target_publishable_count or MAKE_STORY_COUNT)
    attempts = max_candidate_attempts
    if attempts is None:
        attempts = resolve_candidate_scan_limit(environ)
    return ControlledWriterConfig(
        make_story_count=MAKE_STORY_COUNT,
        target_publishable_count=int(target),
        min_desired_publishable_count=MIN_DESIRED_PUBLISHABLE_COUNT,
        max_candidate_attempts=int(attempts),
    )
