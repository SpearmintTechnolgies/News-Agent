"""Rank Math-style SEO checks on the final article (exact focus keyword, links, lengths, readability).

Weights approximate Rank Math's test groups; the score is a guide for the editor, not Rank Math's own number.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from newsagent_v2.seo6.sitemap import SiteIndex

_NORM_RE = re.compile(r"[^a-z0-9$%]+")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_NUMBER_RE = re.compile(r"\d")
URL_MAX = 75
TITLE_MAX = 60
META_RANGE = (120, 160)
DENSITY_RANGE = (0.5, 2.5)
LONG_PARAGRAPH_WORDS = 120
_TRAILING_SMALL = {
    "a", "an", "the", "and", "or", "but", "of", "for", "to", "in", "on", "at", "by", "with", "as", "from",
    "into", "after", "before", "its", "their", "is", "are", "was", "were", "that", "which", "while",
}


def norm(text: str) -> str:
    return " " + _NORM_RE.sub(" ", (text or "").lower().replace("’", "'").replace("'", "")).strip() + " "


def phrase_in(keyword: str, text: str) -> bool:
    """Exact phrase, case-insensitive; a plural "s" on the last word still counts (as in Rank Math)."""
    return phrase_count(keyword, text) > 0


def phrase_count(keyword: str, text: str) -> int:
    key = norm(keyword).strip()
    if not key:
        return 0
    return len(re.findall(rf"(?<= ){re.escape(key)}s?(?= )", norm(text)))


def plain_body(markdown: str) -> str:
    return _MD_LINK_RE.sub(r"\1", re.sub(r"^#{1,6} .*$", "", markdown or "", flags=re.M))


def _sections_before_closing(markdown: str) -> str:
    return re.split(r"^## Conclusion", markdown or "", maxsplit=1, flags=re.M)[0]


@dataclass
class SEOCheck:
    id: str
    label: str
    points: int
    max_points: int
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.points >= self.max_points


@dataclass
class SEOScore:
    score: int
    checks: list[SEOCheck] = field(default_factory=list)

    def misses(self) -> list[SEOCheck]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "checks": [{**asdict(c), "passed": c.passed} for c in self.checks]}


def score_article(
    record: dict[str, Any],
    *,
    base_url: str,
    internal_links: int,
    external_links: int,
    has_featured_image: bool,
    index: SiteIndex | None = None,
) -> SEOScore:
    keyword = str(record.get("focus_keyphrase") or "")
    title = str(record.get("seo_title") or record.get("headline") or "")
    meta = str(record.get("meta_description") or "")
    slug = str(record.get("slug") or "")
    markdown = str(record.get("article_body") or "")
    text = plain_body(markdown)
    words = text.split()
    word_count = len(words)
    intro = " ".join(words[: max(word_count // 10, 40)])
    headings = re.findall(r"^#{2,3} (.+)$", markdown, flags=re.M)
    url = f"{base_url.rstrip('/')}/{slug}/"
    occurrences = phrase_count(keyword, text)
    density = 100.0 * occurrences / max(word_count, 1)
    paragraphs = [b for b in plain_body(_sections_before_closing(markdown)).split("\n\n") if b.strip()]
    longest = max((len(p.split()) for p in paragraphs), default=0)

    def check(cid: str, label: str, ok: bool | float, max_points: int, detail: str = "") -> SEOCheck:
        share = float(ok) if not isinstance(ok, bool) else (1.0 if ok else 0.0)
        return SEOCheck(cid, label, round(max_points * share), max_points, detail)

    length_share = 1.0 if word_count >= 2500 else 0.75 if word_count >= 1500 else 0.5 if word_count >= 1000 else 0.25 if word_count >= 600 else 0.0
    title_pos = norm(title).find(" " + norm(keyword).strip() + " ")
    taken_by = ""
    if index and keyword:
        for post in index.posts:
            if post.slug != slug and phrase_in(keyword, post.label):
                taken_by = post.url
                break

    checks = [
        # Basic SEO
        check("kw_title", "Focus keyword in SEO title", phrase_in(keyword, title), 10, title),
        check("kw_meta", "Focus keyword in meta description", phrase_in(keyword, meta), 8),
        check("kw_url", "Focus keyword in URL", phrase_in(keyword, slug.replace("-", " ")), 8, slug),
        check("kw_intro", "Focus keyword in the first 10% of the content", phrase_in(keyword, intro), 8),
        check("kw_content", "Focus keyword in the content", occurrences > 0, 4),
        check("kw_subheading", "Focus keyword in a subheading", any(phrase_in(keyword, h) for h in headings), 4),
        check("length", "Content length (Rank Math gives full marks at 2,500 words)", length_share, 8,
              f"{word_count} words"),
        # Additional
        check("density", "Keyword density 0.5–2.5%", DENSITY_RANGE[0] <= density <= DENSITY_RANGE[1], 6,
              f"{density:.2f}% ({occurrences} uses)"),
        check("image_alt", "Image with focus keyword as alt text", has_featured_image and bool(keyword), 4),
        check("url_length", f"URL under {URL_MAX} characters", len(url) <= URL_MAX, 4, f"{len(url)} characters"),
        check("external_links", "Links to external sources", external_links > 0, 4, f"{external_links} links"),
        check("internal_links", "Internal links to other posts", min(internal_links, 3) / 3, 6,
              f"{internal_links} links"),
        check("kw_unique", "Focus keyword not used by another post", not taken_by, 3, taken_by),
        check("kw_title_start", "Focus keyword near the start of the SEO title",
              0 <= title_pos <= max(len(norm(title)) // 2, 1), 3),
        # Title readability
        check("title_number", "Number in the SEO title", bool(_NUMBER_RE.search(title)), 4),
        check("title_length", f"SEO title at most {TITLE_MAX} characters", 0 < len(title) <= TITLE_MAX, 3,
              f"{len(title)} characters"),
        check("meta_length", "Meta description 120–160 characters", META_RANGE[0] <= len(meta) <= META_RANGE[1], 3,
              f"{len(meta)} characters"),
        # Content readability
        check("toc", "Table of contents", len(headings) >= 3, 3),
        check("short_paragraphs", f"Paragraphs under {LONG_PARAGRAPH_WORDS} words", longest < LONG_PARAGRAPH_WORDS, 4,
              f"longest {longest} words"),
        check("media", "Image or video inside the content", "![" in markdown or "<img" in markdown, 3),
    ]
    total = sum(c.max_points for c in checks)
    return SEOScore(score=round(100 * sum(c.points for c in checks) / total), checks=checks)


def _headline_phrases(headline: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z0-9$%][A-Za-z0-9$%'’.-]*", headline)
    small = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "at", "by", "with", "as", "after", "is"}
    out = []
    for size in (4, 3, 2):
        for i in range(len(tokens) - size + 1):
            gram = tokens[i:i + size]
            if gram[0].lower() in small or gram[-1].lower() in small:
                continue
            out.append(" ".join(gram))
    return out


def _coverage(keyword: str, record: dict[str, Any]) -> int:
    text = plain_body(str(record.get("article_body") or ""))
    words = text.split()
    intro = " ".join(words[: max(len(words) // 10, 40)])
    headings = re.findall(r"^#{2,3} (.+)$", str(record.get("article_body") or ""), flags=re.M)
    density = 100.0 * phrase_count(keyword, text) / max(len(words), 1)
    return (
        10 * phrase_in(keyword, str(record.get("seo_title") or ""))
        + 8 * phrase_in(keyword, str(record.get("meta_description") or ""))
        + 8 * phrase_in(keyword, intro)
        + 4 * any(phrase_in(keyword, h) for h in headings)
        + 6 * (DENSITY_RANGE[0] <= density <= DENSITY_RANGE[1])
        + min(phrase_count(keyword, text), 8)
    )


def slugify(text: str) -> str:
    return "-".join(norm(text).split())


def _placed(keyword: str, record: dict[str, Any]) -> bool:
    words = plain_body(str(record.get("article_body") or "")).split()
    intro = " ".join(words[: max(len(words) // 10, 40)])
    return all(
        phrase_in(keyword, part)
        for part in (str(record.get("seo_title") or ""), str(record.get("meta_description") or ""), intro)
    )


def best_focus_keyword(record: dict[str, Any]) -> str:
    """Writer's keyword when it sits in the SEO title, meta and intro (a specific phrase beats a
    denser generic one); otherwise the headline phrase with the best coverage."""
    writer = str(record.get("focus_keyphrase") or "").strip()
    if writer and _placed(writer, record):
        return writer
    best, best_score = writer, _coverage(writer, record) if writer else -1
    for phrase in _headline_phrases(str(record.get("headline") or "")):
        score = _coverage(phrase, record)
        if score > best_score:
            best, best_score = phrase, score
    return best


def fit_meta(meta: str, keyword: str = "", limit: int = META_RANGE[1]) -> str:
    """Trim an over-long meta description at a sentence, else a word, boundary; keep the keyword."""
    meta = " ".join(meta.split())
    if len(meta) <= limit:
        return meta
    head = meta[:limit]
    end = max(head.rfind(". "), head.rfind("; "))
    if end + 1 >= META_RANGE[0]:
        trimmed = head[: end + 1]
    else:
        words = head[: head.rfind(" ")].split()
        while len(words) > 1 and words[-1].lower().strip(",;:-") in _TRAILING_SMALL:
            words.pop()
        trimmed = " ".join(words).rstrip(",;:- ") + "."
    if keyword and phrase_in(keyword, meta) and not phrase_in(keyword, trimmed):
        return meta
    return trimmed


def finalize_seo(record: dict[str, Any]) -> dict[str, Any]:
    """Deterministic SEO fixes after writing: keyword choice, slug carrying it, meta length."""
    keyword = best_focus_keyword(record)
    out = dict(record, focus_keyphrase=keyword)
    out["slug"] = slug_with_keyword(str(record.get("slug") or slugify(str(record.get("headline") or ""))), keyword)
    out["meta_description"] = fit_meta(str(record.get("meta_description") or ""), keyword)
    keywords = [k for k in (record.get("keywords") or []) if norm(k).strip() != norm(keyword).strip()]
    out["keywords"] = [keyword, *keywords]
    return out


def slug_with_keyword(slug: str, keyword: str, max_words: int = 8) -> str:
    """Keep the writer's slug when it already carries the keyword; otherwise lead with the keyword."""
    if phrase_in(keyword, slug.replace("-", " ")):
        return slug
    key_words = slugify(keyword).split("-")
    rest = [w for w in slug.split("-") if w and w not in key_words]
    return "-".join((key_words + rest)[:max(max_words, len(key_words))])
