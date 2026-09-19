from __future__ import annotations

import re
from collections import Counter
from typing import Any

from newsagent_v2.article.qa.result import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    issue,
)
from newsagent_v2.article.qa.textutil import (
    split_paragraphs,
    split_sentences,
    word_count,
)

DUP_PUNCT_RE = re.compile(r"([!?.,;:])\1{2,}")
SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([.,;:!?])")


def check_mechanics(article: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, int]]:
    issues: list[dict[str, str]] = []
    headline = article.get("headline") if isinstance(article.get("headline"), str) else ""
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""
    dek = article.get("dek") if isinstance(article.get("dek"), str) else ""

    if not headline.strip():
        issues.append(
            issue(
                code="empty_headline",
                message="headline is empty",
                severity=SEVERITY_CRITICAL,
                module="mechanics",
            )
        )
    if not body.strip():
        issues.append(
            issue(
                code="empty_body",
                message="article_body is empty",
                severity=SEVERITY_CRITICAL,
                module="mechanics",
            )
        )

    sentences = split_sentences(body)
    counts = Counter(sentence.lower() for sentence in sentences if len(sentence.split()) >= 6)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    for sentence, count in counts.items():
        if count > 1:
            issues.append(
                issue(
                    code="repeated_sentence",
                    message=f"repeated sentence occurs {count} times",
                    severity=SEVERITY_WARNING,
                    module="mechanics",
                )
            )

    paragraphs = split_paragraphs(body)
    para_counts = Counter(part.lower() for part in paragraphs if len(part.split()) >= 12)
    for paragraph, count in para_counts.items():
        if count > 1:
            issues.append(
                issue(
                    code="repeated_paragraph",
                    message="suspicious repeated paragraph",
                    severity=SEVERITY_WARNING,
                    module="mechanics",
                )
            )

    combined = f"{headline}\n{dek}\n{body}"
    if re.search(r"[ \t]{3,}", combined) or re.search(r"\n{3,}", combined):
        issues.append(
            issue(
                code="excessive_whitespace",
                message="excessive whitespace",
                severity=SEVERITY_WARNING,
                module="mechanics",
            )
        )
    if DUP_PUNCT_RE.search(combined):
        issues.append(
            issue(
                code="malformed_punctuation",
                message="malformed repeated punctuation",
                severity=SEVERITY_WARNING,
                module="mechanics",
            )
        )
    elif SPACE_BEFORE_PUNCT_RE.search(body):
        issues.append(
            issue(
                code="malformed_punctuation",
                message="space before punctuation",
                severity=SEVERITY_WARNING,
                module="mechanics",
            )
        )

    metrics = {
        "article_word_count": word_count(body),
        "headline_word_count": word_count(headline),
        "repeated_sentence_count": repeated,
        "grammar_mechanics_issue_count": sum(
            1 for item in issues if item["module"] == "mechanics"
        ),
    }
    return issues, metrics
