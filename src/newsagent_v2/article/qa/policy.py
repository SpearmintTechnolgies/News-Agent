"""Configurable article-depth policy. Not a universal length constant elsewhere."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any, Mapping

from newsagent_v2.article.input import evidence_text_blobs
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, SEVERITY_WARNING, issue
from newsagent_v2.article.qa.textutil import word_count

ARTICLE_MODE_NORMAL = "normal"
ARTICLE_MODE_FULL = "full"
ARTICLE_MODE_BRIEF = "brief"
KNOWN_ARTICLE_MODES = frozenset(
    {ARTICLE_MODE_NORMAL, ARTICLE_MODE_FULL, ARTICLE_MODE_BRIEF}
)
FULL_ARTICLE_MODES = frozenset({ARTICLE_MODE_NORMAL, ARTICLE_MODE_FULL})

# Production QA floor. Do not change lightly.
PRODUCTION_HARD_MINIMUM_WORDS = 600

# TEMPORARY / DEMO ONLY — CEO delivery mode.
# Accepts a shorter article; does NOT weaken grounding, copyright, or factual gates.
# Prefer NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS; ARTICLE_MIN_WORDS is also accepted.
DEMO_ARTICLE_MIN_WORDS_ENV = "NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS"
ARTICLE_MIN_WORDS_ENV = "ARTICLE_MIN_WORDS"
DEMO_ARTICLE_MIN_WORDS_ACTIVE_MARKER = "TEMPORARY_DEMO_ARTICLE_MIN_WORDS"


@dataclass(frozen=True)
class ArticleDepthPolicy:
    mode: str
    hard_minimum_words: int
    target_min_words: int
    target_max_words: int
    min_evidence_words_for_target_depth: int
    temporary_demo_minimum: bool = False


NORMAL_ARTICLE_POLICY = ArticleDepthPolicy(
    mode=ARTICLE_MODE_NORMAL,
    hard_minimum_words=PRODUCTION_HARD_MINIMUM_WORDS,
    target_min_words=700,
    target_max_words=1000,
    min_evidence_words_for_target_depth=80,
    temporary_demo_minimum=False,
)

BRIEF_ARTICLE_POLICY = ArticleDepthPolicy(
    mode=ARTICLE_MODE_BRIEF,
    hard_minimum_words=60,
    target_min_words=80,
    target_max_words=220,
    min_evidence_words_for_target_depth=20,
    temporary_demo_minimum=False,
)


def _environ_map(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return environ if environ is not None else os.environ


def resolve_article_hard_minimum_words(
    environ: Mapping[str, str] | None = None,
) -> tuple[int, bool]:
    """Return (hard_minimum_words, temporary_demo_active).

    TEMPORARY / DEMO: when ARTICLE_MIN_WORDS or NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS
    is set to a positive int, that value overrides the production 600-word normal floor
    for length QA only. All other QA gates remain unchanged.
    """
    env = _environ_map(environ)
    raw = str(env.get(DEMO_ARTICLE_MIN_WORDS_ENV) or env.get(ARTICLE_MIN_WORDS_ENV) or "").strip()
    if not raw:
        return PRODUCTION_HARD_MINIMUM_WORDS, False
    try:
        value = int(raw)
    except ValueError:
        return PRODUCTION_HARD_MINIMUM_WORDS, False
    if value < 1:
        return PRODUCTION_HARD_MINIMUM_WORDS, False
    return value, value != PRODUCTION_HARD_MINIMUM_WORDS


def active_normal_depth_policy(
    environ: Mapping[str, str] | None = None,
) -> ArticleDepthPolicy:
    """Production normal policy, or TEMPORARY/DEMO length floor when configured."""
    floor, demo = resolve_article_hard_minimum_words(environ)
    if not demo and floor == PRODUCTION_HARD_MINIMUM_WORDS:
        return NORMAL_ARTICLE_POLICY
    return replace(
        NORMAL_ARTICLE_POLICY,
        hard_minimum_words=floor,
        temporary_demo_minimum=True,
    )


def resolve_depth_policy(
    *,
    article_mode: str | None = None,
    depth_policy: ArticleDepthPolicy | None = None,
    environ: Mapping[str, str] | None = None,
) -> ArticleDepthPolicy:
    if depth_policy is not None:
        if depth_policy.mode not in KNOWN_ARTICLE_MODES:
            raise ValueError(f"unknown article depth mode: {depth_policy.mode!r}")
        return depth_policy
    mode = article_mode or ARTICLE_MODE_NORMAL
    if mode == ARTICLE_MODE_BRIEF:
        return BRIEF_ARTICLE_POLICY
    if mode in FULL_ARTICLE_MODES:
        return active_normal_depth_policy(environ)
    raise ValueError(
        f"unknown article_mode {mode!r}; pass {ARTICLE_MODE_BRIEF!r} explicitly "
        "to use brief policy. Short articles are not converted to brief."
    )


def evidence_word_count(article_input: dict[str, Any]) -> int:
    return word_count(" ".join(evidence_text_blobs(article_input)))


def check_depth(
    article: dict[str, Any],
    article_input: dict[str, Any],
    policy: ArticleDepthPolicy,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    issues: list[dict[str, str]] = []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    article_words = word_count(body)
    pack_words = evidence_word_count(article_input)

    if article_words < policy.hard_minimum_words:
        issues.append(
            issue(
                code="below_article_minimum_length",
                message=(
                    f"{policy.mode} article has {article_words} words; "
                    f"hard minimum is {policy.hard_minimum_words}"
                ),
                severity=SEVERITY_CRITICAL,
                module="depth",
            )
        )

    if (
        policy.mode == ARTICLE_MODE_NORMAL
        and pack_words < policy.min_evidence_words_for_target_depth
    ):
        issues.append(
            issue(
                code="insufficient_evidence_for_target_depth",
                message=(
                    f"evidence pack has {pack_words} words of title/summary text; "
                    "that is not enough to support a responsible full article. "
                    "Do not invent filler."
                ),
                severity=SEVERITY_CRITICAL,
                module="depth",
            )
        )

    if article_words >= policy.hard_minimum_words and not (
        policy.target_min_words <= article_words <= policy.target_max_words
    ):
        issues.append(
            issue(
                code="outside_target_word_range",
                message=(
                    f"article word count {article_words} is outside the "
                    f"{policy.target_min_words}-{policy.target_max_words} target"
                ),
                severity=SEVERITY_WARNING,
                module="depth",
            )
        )

    metrics = {
        "article_mode": policy.mode,
        "depth_hard_minimum_words": policy.hard_minimum_words,
        "evidence_word_count": pack_words,
        DEMO_ARTICLE_MIN_WORDS_ACTIVE_MARKER: bool(policy.temporary_demo_minimum),
    }
    return issues, metrics
