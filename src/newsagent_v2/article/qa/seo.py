from __future__ import annotations

import re
from collections import Counter
from typing import Any

from newsagent_v2.article.contract import ARTICLE_CATEGORIES
from newsagent_v2.article.qa.result import SEVERITY_WARNING, issue
from newsagent_v2.article.qa.textutil import word_count

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SEO_TITLE_MIN = 12
SEO_TITLE_MAX = 70
META_MIN = 40
META_MAX = 160


def check_seo(article: dict[str, Any]) -> tuple[list[dict[str, str]], int]:
    issues: list[dict[str, str]] = []
    seo_title = article.get("seo_title")
    meta = article.get("meta_description")
    slug = article.get("slug")
    category = article.get("category")
    keywords = article.get("keywords") if isinstance(article.get("keywords"), list) else []
    body = article.get("article_body") if isinstance(article.get("article_body"), str) else ""

    # SEO-only defects are WARNING — never alone kill an otherwise safe article.
    if not isinstance(seo_title, str) or not seo_title.strip():
        issues.append(
            issue(
                code="seo_title_missing",
                message="seo_title is missing",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )
    elif not (SEO_TITLE_MIN <= len(seo_title.strip()) <= SEO_TITLE_MAX):
        issues.append(
            issue(
                code="seo_title_length",
                message="seo_title length is outside 12-70 characters",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )

    if not isinstance(meta, str) or not meta.strip():
        issues.append(
            issue(
                code="meta_description_missing",
                message="meta_description is missing",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )
    elif not (META_MIN <= len(meta.strip()) <= META_MAX):
        issues.append(
            issue(
                code="meta_description_length",
                message="meta_description length is outside 40-160 characters",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )

    if not isinstance(slug, str) or not SLUG_RE.match(slug or ""):
        issues.append(
            issue(
                code="invalid_slug",
                message="slug must be lowercase kebab-case",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )

    if category not in ARTICLE_CATEGORIES:
        issues.append(
            issue(
                code="seo_invalid_category",
                message="category is not valid",
                severity=SEVERITY_WARNING,
                module="seo",
            )
        )

    body_words = body.lower().split()
    total = max(word_count(body), 1)
    for keyword in keywords:
        if not isinstance(keyword, str) or not keyword.strip():
            continue
        token = keyword.strip().lower()
        if " " in token:
            count = body.lower().count(token)
        else:
            count = Counter(body_words)[token]
        density = count / total
        if count >= 8 or density >= 0.05:
            issues.append(
                issue(
                    code="keyword_stuffing",
                    message=f"keyword {token!r} appears unnaturally often",
                    severity=SEVERITY_WARNING,
                    module="seo",
                )
            )
            break

    return issues, sum(1 for item in issues if item["module"] == "seo")
