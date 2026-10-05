"""V6 long-form writer: one drafting call, one revision call if structure checks fail."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.facts.bank import Fact, FactBank, Quote
from newsagent_v2.seo6.score import phrase_count, phrase_in
from newsagent_v2.write.article import Article
from newsagent_v2.write.kimi import KimiClient, KimiError, StoryBudget
from newsagent_v2.write.prompt import (
    BODY_ASK_WORDS,
    BODY_MIN_WORDS,
    CATEGORIES,
    build_messages,
    revision_messages,
    select_facts,
    usable_quotes,
)

logger = logging.getLogger(__name__)

MAX_SECTION_WORDS = 380
MAX_SECTION_PARAGRAPHS = 6
MIN_KEYWORD_USES = 4
_QUOTED_RE = re.compile(r"[“\"]([^”\"]{3,600})[”\"]")
_NORM_RE = re.compile(r"[^a-z0-9$%]+")


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower().replace("’", "'")).strip()


def keyword_present(keyword: str, text: str) -> bool:
    """The exact keyword phrase, as Rank Math matches it (case-insensitive, plural last word allowed)."""
    return phrase_in(keyword, text)


@dataclass
class WriteResult:
    ok: bool
    article: Article | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    revisions: int = 0
    error: str = ""
    facts_offered: list[str] = field(default_factory=list)
    quote_ids: dict[str, dict[str, str]] = field(default_factory=dict)
    messages: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "article": self.article.to_dict() if self.article else None,
            "issues": list(self.issues),
            "revisions": self.revisions,
            "error": self.error,
            "facts_offered": list(self.facts_offered),
            "quote_ids": dict(self.quote_ids),
        }


def structural_issues(
    article: Article,
    facts: dict[str, Fact],
    quotes: dict[str, Quote],
) -> list[str]:
    issues: list[str] = []
    if not article.headline:
        issues.append("headline is missing")
    elif len(article.headline) > 90:
        issues.append(f"headline is {len(article.headline)} characters; maximum is 90")
    if article.body_words < BODY_MIN_WORDS:
        issues.append(
            f"body is {article.body_words} words; it must be at least {BODY_ASK_WORDS}. Develop the story's own "
            "facts in more depth and use relevant background facts; never add unrelated market news"
        )
    for index, section in enumerate(article.sections, start=1):
        section_words = sum(len(p.text.split()) for p in section.paragraphs)
        if section_words > MAX_SECTION_WORDS or len(section.paragraphs) > MAX_SECTION_PARAGRAPHS:
            issues.append(
                f"section {index} has {len(section.paragraphs)} paragraphs and {section_words} words; keep each "
                f"section to at most {MAX_SECTION_PARAGRAPHS} paragraphs and {MAX_SECTION_WORDS} words, and cut "
                "material that is not about this story"
            )
    if not 4 <= len(article.sections) <= 6:
        issues.append(f"article has {len(article.sections)} sections; use four to six")
    for index, section in enumerate(article.sections[1:], start=2):
        if not section.heading:
            issues.append(f"section {index} has no heading")
    if not article.conclusion:
        issues.append("conclusion is missing")
    if not 4 <= len(article.faq) <= 5:
        issues.append(f"FAQ has {len(article.faq)} questions; use four or five")

    unknown_facts = sorted(article.cited_facts() - set(facts))
    if unknown_facts:
        issues.append(f"cited fact IDs that do not exist: {', '.join(unknown_facts[:10])}")
    for location, para in article.all_paragraphs():
        if not para.facts:
            issues.append(f"{location} cites no facts; cite the facts it uses")
        for qid in para.quotes:
            if qid not in quotes:
                issues.append(f"{location} cites quote {qid}, which does not exist")
        quoted = [m.group(1) for m in _QUOTED_RE.finditer(para.text) if len(m.group(1).split()) >= 3]
        allowed = [_norm(quotes[q].text) for q in para.quotes if q in quotes]
        for span in quoted:
            if not any(_norm(span) in source for source in allowed):
                issues.append(
                    f'{location} puts quotation marks around "{span[:80]}", which is not an exact listed quote '
                    "cited in that paragraph; quote exactly or paraphrase without quotation marks"
                )

    seo = article.seo
    if not seo.focus_keyword:
        issues.append("seo.focus_keyword is missing")
    else:
        first = article.sections[0].paragraphs[0].text if article.sections and article.sections[0].paragraphs else ""
        for label, text in (
            ("the headline", article.headline),
            ("the meta_title", seo.meta_title),
            ("the first paragraph", first),
            ("the meta_description", seo.meta_description),
        ):
            if not keyword_present(seo.focus_keyword, text):
                issues.append(f'the exact focus keyword phrase "{seo.focus_keyword}" is not in {label}')
        if not any(keyword_present(seo.focus_keyword, s.heading) for s in article.sections):
            issues.append(f'the exact focus keyword phrase "{seo.focus_keyword}" is not in any section heading')
        body_text = " ".join(p.text for s in article.sections for p in s.paragraphs)
        uses = phrase_count(seo.focus_keyword, body_text)
        if uses < MIN_KEYWORD_USES:
            issues.append(
                f'the exact focus keyword phrase "{seo.focus_keyword}" appears {uses} times in the body; use it '
                f"{MIN_KEYWORD_USES} to 8 times where it reads naturally, or choose a shorter phrase the story repeats"
            )
    if not seo.meta_title or len(seo.meta_title) > 60:
        issues.append(f"meta_title must be 1 to 60 characters (is {len(seo.meta_title)})")
    if not 120 <= len(seo.meta_description) <= 160:
        issues.append(f"meta_description must be 140 to 155 characters (is {len(seo.meta_description)})")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+){1,9}", seo.slug or ""):
        issues.append("slug must be lowercase words joined by hyphens")
    if seo.category not in CATEGORIES:
        issues.append(f"category must be one of {', '.join(CATEGORIES)}")
    if not 3 <= len(seo.tags) <= 6:
        issues.append(f"use three to six tags (has {len(seo.tags)})")
    return issues


def write_article(
    bank: FactBank,
    client: KimiClient,
    budget: StoryBudget,
    *,
    today: str | None = None,
    max_revisions: int = 1,
) -> WriteResult:
    day = today or datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    facts = select_facts(bank)
    quote_list = usable_quotes(bank)
    fact_map = {f.id: f for f in facts}
    quote_map = dict(quote_list)
    result = WriteResult(
        ok=False,
        facts_offered=[f.id for f in facts],
        quote_ids={qid: {"speaker": q.speaker, "text": q.text, "source_url": q.source_url} for qid, q in quote_list},
    )
    messages = build_messages(bank, today=day, facts=facts, quotes=quote_list)
    result.messages = messages
    try:
        raw = client.chat_json(messages, budget=budget, stage="draft")
        article = Article.from_json(raw)
        issues = structural_issues(article, fact_map, quote_map)
        while issues and result.revisions < max_revisions:
            logger.info("[WRITER] event=%s revising %d issues", bank.event_id, len(issues))
            raw = client.chat_json(revision_messages(messages, raw, issues), budget=budget, stage="revise_structure")
            result.revisions += 1
            article = Article.from_json(raw)
            issues = structural_issues(article, fact_map, quote_map)
    except KimiError as exc:
        result.error = str(exc)
        return result
    result.raw = raw
    result.article = article
    result.issues = issues
    result.ok = not issues
    return result
