"""Internal links: pick related published posts and link natural phrases in the body."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.seo6.sitemap import SiteIndex, SitePost

MAX_INLINE_LINKS = 4
MAX_RELATED_LINKS = 3
MIN_SHARED_TERMS = 2
TOPIC_WEIGHT = 2.0  # headline, focus keyword, tags, entities (headings weigh 1)
MIN_RELEVANCE = 1.8  # term rarity is scaled to 0..1, so this is independent of site size
DUPLICATE_SHARE = 0.75

STOPWORDS = frozenset(
    "a an the and or but of for to in on at by with from as is are was were be been it its this that these those "
    "into over after before amid about than then new says said say will would could can may might has have had "
    "not no up down out how what why who when where which vs via per more most less just also all any their his "
    "her they we you your our us news today week year day report reports update updates latest here first".split()
)
# Common to most posts on a crypto site; they count, but little.
GENERIC_TERMS = frozenset("crypto cryptocurrency bitcoin btc market markets price prices coin coins token tokens".split())

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")
_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")


def _stem(word: str) -> str:
    word = word.lower().strip("'’-").replace("’", "'").removesuffix("'s")
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def terms(text: str) -> list[str]:
    return [
        s for s in (_stem(w) for w in _WORD_RE.findall(text or ""))
        if s and s not in STOPWORDS and len(s) > 1 and not s.isdigit()
    ]


@dataclass
class LinkPlan:
    inline: list[dict[str, str]] = field(default_factory=list)
    related: list[dict[str, str]] = field(default_factory=list)
    possible_duplicate: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def internal_count(self) -> int:
        return len(self.inline) + len(self.related)


def _idf(index: SiteIndex) -> dict[str, float]:
    df: dict[str, int] = {}
    for post in index.posts:
        for term in set(terms(post.label + " " + post.slug.replace("-", " "))):
            df[term] = df.get(term, 0) + 1
    total = max(len(index.posts), 1)
    scale = math.log(total + 1) or 1.0
    return {term: max(math.log((total + 1) / (count + 0.5)), 0.0) / scale for term, count in df.items()}


def _profile(article: dict[str, Any]) -> dict[str, float]:
    weights: dict[str, float] = {}

    def add(text: str, weight: float) -> None:
        for term in set(terms(text)):
            weights[term] = max(weights.get(term, 0.0), weight)

    body = re.split(r"^## Conclusion", article.get("article_body") or "", maxsplit=1, flags=re.M)[0]
    for heading in re.findall(r"^## (.+)$", body, flags=re.M):
        add(heading, 1.0)
    for entity in article.get("entities") or []:
        add(entity.get("name", "") if isinstance(entity, dict) else str(entity), 2.0)
    for tag in article.get("tags") or []:
        add(str(tag), 2.0)
    add(article.get("headline") or "", 3.0)
    add(article.get("focus_keyphrase") or "", 3.0)
    return weights


def _age_years(lastmod: str) -> float:
    try:
        when = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
    except ValueError:
        return 2.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - when).days / 365.0, 0.0)


def rank_related(article: dict[str, Any], index: SiteIndex, limit: int = 12) -> list[dict[str, Any]]:
    idf = _idf(index)
    profile = _profile(article)
    own_slug = str(article.get("slug") or "")
    headline_terms = set(terms(article.get("headline") or ""))
    ranked = []
    for post in index.posts:
        if post.slug == own_slug:
            continue
        post_terms = set(terms(post.label + " " + post.slug.replace("-", " ")))
        shared = post_terms & set(profile)
        topical = [t for t in shared if profile[t] >= TOPIC_WEIGHT]
        if len(topical) < MIN_SHARED_TERMS or not any(t not in GENERIC_TERMS for t in topical):
            continue
        score = sum(profile[t] * idf.get(t, 1.0) * (0.3 if t in GENERIC_TERMS else 1.0) for t in shared)
        score /= 1.0 + 0.25 * _age_years(post.lastmod)
        if score < MIN_RELEVANCE:
            continue
        overlap = len(post_terms & headline_terms) / max(len(headline_terms), 1)
        ranked.append({"post": post, "score": round(score, 2), "shared": sorted(shared), "headline_overlap": overlap})
    ranked.sort(key=lambda r: -r["score"])
    return ranked[:limit]


def _anchor_in(paragraph: str, post: SitePost, idf: dict[str, float]) -> tuple[int, int] | None:
    """Longest run of 2-6 words in the paragraph made of the post's title terms (stopwords allowed inside)."""
    target = set(terms(post.label + " " + post.slug.replace("-", " ")))
    words = list(_WORD_RE.finditer(paragraph))
    best: tuple[int, int, float] | None = None
    for i, first in enumerate(words):
        if _stem(first.group()) not in target:
            continue
        content, weight, specific = 0, 0.0, False
        for j in range(i, min(i + 6, len(words))):
            if j > i and paragraph[words[j - 1].end():words[j].start()].strip():
                break  # never span punctuation
            stem = _stem(words[j].group())
            if stem in target:
                content += 1
                specific = specific or stem not in GENERIC_TERMS
                weight += idf.get(stem, 1.0) * (0.3 if stem in GENERIC_TERMS else 1.0)
                if content >= 2 and specific and (best is None or weight > best[2]):
                    best = (first.start(), words[j].end(), weight)
            elif stem not in STOPWORDS:
                break
    if not best:
        return None
    start, end, _ = best
    before = paragraph[:start]
    if before.count('"') % 2 or before.count("“") > before.count("”") or _LINK_RE.search(paragraph[start:end]):
        return None
    return start, end


def plan_and_apply(article: dict[str, Any], index: SiteIndex) -> tuple[str, LinkPlan]:
    """Return the body with inline internal links and the plan (inline, related, duplicate warning)."""
    body = str(article.get("article_body") or "")
    plan = LinkPlan()
    if not index.posts:
        return body, plan
    ranked = rank_related(article, index)
    plan.candidates = [{"url": r["post"].url, "title": r["post"].label, "score": r["score"]} for r in ranked]
    if ranked and ranked[0]["headline_overlap"] >= DUPLICATE_SHARE:
        top = ranked[0]["post"]
        plan.possible_duplicate = {"url": top.url, "title": top.label}

    idf = _idf(index)
    blocks = body.split("\n\n")
    closing = next((i for i, b in enumerate(blocks) if b.startswith("## Conclusion")), len(blocks))
    linked_blocks: set[int] = set()
    used_anchors: set[str] = set()
    keyword = str(article.get("focus_keyphrase") or "").lower()
    for candidate in ranked:
        post = candidate["post"]
        if len(plan.inline) >= MAX_INLINE_LINKS:
            break
        for i in range(1, closing):
            block = blocks[i]
            if i in linked_blocks or block.startswith("#") or not block.strip():
                continue
            span = _anchor_in(block, post, idf)
            if span:
                start, end = span
                anchor = block[start:end]
                anchor_key = " ".join(terms(anchor))
                if anchor_key in used_anchors or (keyword and anchor.lower() == keyword):
                    continue
                used_anchors.add(anchor_key)
                blocks[i] = f"{block[:start]}[{anchor}]({post.url}){block[end:]}"
                linked_blocks.add(i)
                plan.inline.append({"url": post.url, "title": post.label, "anchor": anchor})
                break
    inline_urls = {link["url"] for link in plan.inline}
    for candidate in ranked:
        if len(plan.related) >= MAX_RELATED_LINKS:
            break
        if candidate["post"].url not in inline_urls:
            plan.related.append({"url": candidate["post"].url, "title": candidate["post"].label})
    return "\n\n".join(blocks), plan
