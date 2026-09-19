from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from newsagent_v2.article.input import evidence_index
from newsagent_v2.article.qa.result import SEVERITY_CRITICAL, issue

WINDOWS_PATH_RE = re.compile(r"[A-Za-z]:\\|/Users/|/home/")
ENV_RE = re.compile(
    r"\b(?:GROQ_API_KEY|OPENAI_API_KEY|API_KEY|AUTHORIZATION|os\.environ)\b",
    re.IGNORECASE,
)
LOCALHOST_RE = re.compile(r"\b(?:localhost|127\.0\.0\.1)\b", re.IGNORECASE)
PROMPT_LEAK_RE = re.compile(
    r"(you are the editorial decision engine|system prompt|ignore previous instructions)",
    re.IGNORECASE,
)
HREF_RE = re.compile(r"https?://\S+|href\s*=\s*['\"]([^'\"]*)['\"]", re.IGNORECASE)


def check_safety(
    article: dict[str, Any],
    article_input: dict[str, Any],
) -> tuple[list[dict[str, str]], int]:
    issues: list[dict[str, str]] = []
    blobs = [
        article.get("headline"),
        article.get("dek"),
        article.get("article_body"),
        article.get("seo_title"),
        article.get("meta_description"),
        article.get("generation_notes"),
    ]
    text = "\n".join(part for part in blobs if isinstance(part, str))
    allowed = evidence_index(article_input)

    if WINDOWS_PATH_RE.search(text):
        issues.append(
            issue(
                code="windows_path_leak",
                message="article text contains an internal filesystem path",
                severity=SEVERITY_CRITICAL,
                module="safety",
            )
        )
    if ENV_RE.search(text):
        issues.append(
            issue(
                code="secret_or_env_leak",
                message="article text contains environment/secret identifiers",
                severity=SEVERITY_CRITICAL,
                module="safety",
            )
        )
    if LOCALHOST_RE.search(text):
        issues.append(
            issue(
                code="localhost_leak",
                message="article text contains a localhost URL",
                severity=SEVERITY_CRITICAL,
                module="safety",
            )
        )
    if PROMPT_LEAK_RE.search(text):
        issues.append(
            issue(
                code="prompt_leak",
                message="article text looks like prompt/system leakage",
                severity=SEVERITY_CRITICAL,
                module="safety",
            )
        )

    for match in HREF_RE.finditer(text):
        raw = match.group(1) if match.lastindex else match.group(0)
        if raw is not None and not raw.strip():
            issues.append(
                issue(
                    code="empty_href",
                    message="empty href-like URL",
                    severity=SEVERITY_CRITICAL,
                    module="safety",
                )
            )

    used = article.get("evidence_used") if isinstance(article.get("evidence_used"), list) else []
    for i, ref in enumerate(used):
        if not isinstance(ref, dict):
            continue
        url = ref.get("url")
        if not isinstance(url, str):
            continue
        parsed = urlparse(url)
        if parsed.hostname in {"localhost", "127.0.0.1"}:
            issues.append(
                issue(
                    code="localhost_evidence",
                    message=f"evidence_used[{i}] points at localhost",
                    severity=SEVERITY_CRITICAL,
                    module="safety",
                )
            )
        if url not in allowed:
            issues.append(
                issue(
                    code="unsupported_source_url",
                    message=f"evidence_used[{i}] is not in the evidence pack",
                    severity=SEVERITY_CRITICAL,
                    module="safety",
                )
            )

    return issues, len(issues)
