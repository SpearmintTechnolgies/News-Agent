from __future__ import annotations

import re
from typing import Any

from newsagent_v2.article.input import evidence_text_blobs
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, SEVERITY_WARNING, issue
from newsagent_v2.article.qa.textutil import number_tokens, word_count

HEADLINE_MIN_WORDS = 3
HEADLINE_MAX_WORDS = 22
ALL_CAPS_RE = re.compile(r"[A-Z]")
DUP_PUNCT_RE = re.compile(r"([!?.,])\1")


def check_headline(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    headline = article.get("headline")
    if not isinstance(headline, str) or not headline.strip():
        issues.append(
            issue(
                code="headline_empty",
                message="headline is empty",
                severity=SEVERITY_CRITICAL,
                module="headline",
            )
        )
        return issues

    count = word_count(headline)
    if count < HEADLINE_MIN_WORDS or count > HEADLINE_MAX_WORDS:
        issues.append(
            issue(
                code="headline_length",
                message=f"headline word count {count} is outside {HEADLINE_MIN_WORDS}-{HEADLINE_MAX_WORDS}",
                severity=SEVERITY_WARNING,
                module="headline",
            )
        )

    letters = [char for char in headline if char.isalpha()]
    if letters and all(char.isupper() for char in letters) and len(letters) >= 8:
        issues.append(
            issue(
                code="headline_all_caps",
                message="headline is ALL-CAPS",
                severity=SEVERITY_WARNING,
                module="headline",
            )
        )

    if DUP_PUNCT_RE.search(headline):
        issues.append(
            issue(
                code="headline_duplicate_punctuation",
                message="headline has duplicate punctuation",
                severity=SEVERITY_WARNING,
                module="headline",
            )
        )

    evidence_numbers = set()
    for blob in evidence_text_blobs(article_input):
        evidence_numbers |= number_tokens(blob)
    for token in number_tokens(headline):
        if token not in evidence_numbers:
            issues.append(
                issue(
                    code="headline_unsupported_number",
                    message=f"headline number {token!r} is not present in evidence",
                    severity=SEVERITY_CRITICAL,
                    module="headline",
                )
            )
            break

    if headline.strip().endswith(("...", "…")):
        issues.append(
            issue(
                code="headline_ellipsis",
                message="headline ends with an ellipsis",
                severity=SEVERITY_WARNING,
                module="headline",
            )
        )
    return issues
