"""SEO metadata builder and validation for WordPress articles.

Deterministic SEO fields without LLM, using existing NewsAgent data.
Supports:
- Core WordPress: title, excerpt (meta_description)
- Common SEO plugins (RankMath/Yoast compatible): seo_title, meta_description, focus_keyphrase, canonical
- Custom meta fields via WP REST 'meta' object

Validation reports PASS/WARN with concise reasons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any




def truncate_at_boundary(text: str, max_len: int, *, min_ratio: float = 0.55) -> str:
    """Truncate at a safe sentence/word boundary — never mid-token (e.g. never `$87,00`).

    Prefers the last sentence end within the window; otherwise the last whitespace.
    Strips trailing partial punctuation. Returns text unchanged when already short.
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw or max_len <= 0 or len(raw) <= max_len:
        return raw

    window = raw[:max_len]
    min_keep = max(20, int(max_len * min_ratio))

    best_sent = -1
    for match in re.finditer(r"[.!?]", window):
        idx = match.start()
        # Skip decimals like 998.9
        if idx > 0 and window[idx - 1].isdigit() and idx + 1 < len(window) and window[idx + 1].isdigit():
            continue
        nxt_ok = idx + 1 >= len(window) or window[idx + 1] in " '\"" or window[idx + 1].isupper()
        if nxt_ok and idx + 1 >= min_keep:
            best_sent = idx
    if best_sent >= min_keep - 1:
        return window[: best_sent + 1].strip()

    space = window.rfind(" ")
    if space >= min_keep:
        cut = window[:space].rstrip(" ,;:|-")
        # Refuse dangling incomplete money token like "$87," or "$"
        if cut.endswith("$") or re.search(r"\$\d[\d,]*$", cut) and not re.search(
            r"\$[\d,]+(?:\.\d+)?(?:\s*(?:billion|million|thousand))?$", cut, re.I
        ):
            prev = cut.rfind(" ")
            if prev >= min_keep:
                cut = cut[:prev].rstrip(" ,;:|-")
        return cut

    end = max_len
    while end > min_keep and (window[end - 1].isalnum() or window[end - 1] in ",.$%"):
        end -= 1
    trimmed = window[:end].rstrip(" ,;:|-")
    if trimmed:
        return trimmed
    return window[:max_len].rsplit(" ", 1)[0]



_TITLE_INCOMPLETE_TRAILING = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "as",
    "at", "by", "from", "into", "over", "after", "before", "largest", "biggest",
    "smallest", "highest", "lowest", "since", "amid", "nearly", "almost", "vs",
})


def strip_incomplete_title_tail(title: str) -> str:
    """Drop orphan trailing words left by length truncation (e.g. ... Inflow Largest)."""
    words = re.sub(r"\s+", " ", str(title or "")).strip().split()
    while words and words[-1].lower().strip(".,:;!?") in _TITLE_INCOMPLETE_TRAILING:
        words.pop()
    return " ".join(words).strip()


def prefer_focus_at_seo_title_start(title: str, focus_keyphrase: str) -> str:
    """If focus already appears in title after a short geo/org prefix, start with focus.

    Does not invent words — only reorders/strips an existing short prefix such as US/UK/EU.
    """
    title_s = re.sub(r"\s+", " ", str(title or "")).strip()
    focus = re.sub(r"\s+", " ", str(focus_keyphrase or "")).strip()
    if not title_s or not focus:
        return title_s
    tl = title_s.lower()
    fl = focus.lower()
    if fl not in tl:
        return title_s
    if tl.startswith(fl):
        return title_s
    # Strip a single leading 1-2 token qualifier when focus begins immediately after it.
    m = re.match(r"(?i)^(US|UK|EU|U\.S\.|U\.K\.)\s+(.+)$", title_s)
    if m and m.group(2).lower().startswith(fl):
        return m.group(2).strip()
    return title_s


@dataclass
class SEOMetadata:
    """SEO metadata for a WordPress post."""
    title: str  # WP title (headline)
    seo_title: str  # SEO-optimized title (may differ)
    meta_description: str  # Meta description
    focus_keyphrase: str  # Primary keyword/phrase
    slug: str  # URL slug
    canonical_url: str | None  # Canonical URL if different from post URL
    open_graph_title: str | None  # OG title
    open_graph_description: str | None  # OG description
    twitter_title: str | None  # Twitter card title
    twitter_description: str | None  # Twitter card description

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return {
            "title": self.title,
            "seo_title": self.seo_title,
            "meta_description": self.meta_description,
            "focus_keyphrase": self.focus_keyphrase,
            "slug": self.slug,
            "canonical_url": self.canonical_url,
            "open_graph_title": self.open_graph_title,
            "open_graph_description": self.open_graph_description,
            "twitter_title": self.twitter_title,
            "twitter_description": self.twitter_description,
        }


@dataclass
class SEOValidationResult:
    """Result of SEO validation."""
    status: str  # "PASS" or "WARN"
    seo_score: int  # 0-100
    issues: list[dict[str, Any]]  # [{code, message, severity}, ...]
    recommendations: list[str]  # Human-readable recommendations


def validate_wordpress_seo_meta(meta: dict[str, Any], seo: SEOMetadata) -> list[str]:
    """Validate NewsAgent SEO fields on the wp/v2/posts REST meta package.

    Rank Math keys are intentionally not required (and must not be sent on
    posts create/update — they triggered PHP fatals / HTTP 500 on the site).
    """
    expected = {
        "newsagent_seo_title": seo.seo_title,
        "newsagent_focus_keyphrase": seo.focus_keyphrase,
        "newsagent_canonical_url": seo.canonical_url or "",
    }
    issues = [key for key, value in expected.items() if meta.get(key) != value]
    # Guard: never allow Rank Math keys or JSON-null canonical on the wire.
    rank_math_keys = [k for k in meta if str(k).startswith("rank_math_")]
    if rank_math_keys:
        issues.extend(f"forbidden_rank_math:{k}" for k in sorted(rank_math_keys))
    if meta.get("newsagent_canonical_url") is None:
        issues.append("newsagent_canonical_url_null")
    return issues


class SEOMetadataBuilder:
    """Build SEO metadata from NewsAgent article data."""

    # SEO best practice limits
    TITLE_MIN = 30
    TITLE_MAX = 60
    META_DESC_MIN = 70
    META_DESC_MAX = 160
    SLUG_MAX = 50
    KEYPHRASE_MIN = 2

    def __init__(self, wp_base_url: str) -> None:
        self.wp_base_url = wp_base_url.rstrip("/")

    def _extract_keyphrase(
        self,
        headline: str,
        dek: str,
        *,
        keywords: list[str] | None = None,
        topic: str = "",
        entities: list[str] | None = None,
    ) -> str:
        """Extract focus keyphrase deterministically (no LLM).

        Prefers article.keywords, then multi-word headline/entity phrases.
        Rejects garbage single tokens (e.g. "spot" alone).
        """
        from .rankmath import select_focus_keyphrase

        return select_focus_keyphrase(
            keywords=keywords or [],
            headline=headline or "",
            topic=topic or "",
            entities=entities or [],
            dek=dek or "",
        )

    def _clean_slug(self, slug: str, headline: str) -> str:
        """Clean and validate slug."""
        if slug and len(slug) <= self.SLUG_MAX:
            return slug.lower().strip("-/")

        # Generate from headline
        if headline:
            # Lowercase, alphanumeric + hyphens only
            cleaned = re.sub(r'[^\w\s-]', '', headline.lower())
            cleaned = re.sub(r'[\s]+', '-', cleaned)
            cleaned = re.sub(r'-+', '-', cleaned)
            return cleaned[:self.SLUG_MAX].strip("-")

        return "post"

    def _derive_canonical(self, slug: str, canonical_hint: str | None) -> str | None:
        """Derive canonical URL, return None if same as post URL."""
        if canonical_hint and canonical_hint.startswith("http"):
            return canonical_hint
        # Canonical only needed if different from expected URL
        return None  # None means "use post URL"

    def build(
        self,
        headline: str,
        seo_title: str | None,
        dek: str | None,
        meta_description: str | None,
        slug: str | None,
        canonical: str | None = None,
        keywords: list[str] | None = None,
        topic: str = "",
        entities: list[str] | None = None,
    ) -> SEOMetadata:
        """Build SEO metadata from NewsAgent fields.

        Priority:
        1. Explicit SEO fields if present
        2. Fallback to headline/dek/content
        """
        # Title: prefer explicit seo_title, fallback to headline
        title = headline.strip() if headline else ""
        final_seo_title = (seo_title or "").strip() if seo_title else title

        # Meta description: explicit or dek
        final_meta_desc = (meta_description or "").strip() if meta_description else ""
        if not final_meta_desc and dek:
            final_meta_desc = dek.strip()

        # Keyphrase: keywords / headline / entities (never garbage single tokens)
        keyphrase = self._extract_keyphrase(
            headline or "",
            dek or "",
            keywords=keywords,
            topic=topic,
            entities=entities,
        )

        # Slug: clean existing or generate from headline
        final_slug = self._clean_slug(slug or "", headline or "")

        # Canonical: only if different
        canonical_url = self._derive_canonical(final_slug, canonical)

        # Enforce length at safe boundaries (never mid-token like "$87,00")
        if len(final_seo_title) > self.TITLE_MAX:
            final_seo_title = truncate_at_boundary(final_seo_title, self.TITLE_MAX)
        final_seo_title = strip_incomplete_title_tail(final_seo_title)
        final_seo_title = prefer_focus_at_seo_title_start(final_seo_title, keyphrase)
        # Re-enforce max length if prefix strip somehow lengthened (should not)
        if len(final_seo_title) > self.TITLE_MAX:
            final_seo_title = strip_incomplete_title_tail(
                truncate_at_boundary(final_seo_title, self.TITLE_MAX)
            )
        if final_meta_desc:
            # Prefer keeping focus keyphrase when already present; truncate safely
            final_meta_desc = truncate_at_boundary(final_meta_desc, self.META_DESC_MAX)

        # OG/Twitter: use SEO title/description if different
        og_title = final_seo_title if final_seo_title != title else None
        og_desc = (
            final_meta_desc
            if len(final_meta_desc) <= 300
            else truncate_at_boundary(final_meta_desc, 300)
        )

        return SEOMetadata(
            title=title,
            seo_title=final_seo_title,
            meta_description=final_meta_desc,
            focus_keyphrase=keyphrase,
            slug=final_slug,
            canonical_url=canonical_url,
            open_graph_title=og_title,
            open_graph_description=og_desc,
            twitter_title=og_title,
            twitter_description=og_desc,
        )


class SEOValidator:
    """Validate SEO metadata and report issues."""

    def __init__(self) -> None:
        self.builder = SEOMetadataBuilder("")  # URL not needed for validation

    def _check_title(self, title: str, seo_title: str) -> list[dict[str, Any]]:
        """Check title length and quality."""
        issues = []
        effective_title = seo_title if seo_title else title

        if not effective_title:
            issues.append({
                "code": "TITLE_MISSING",
                "message": "Title is empty",
                "severity": "critical",
            })
        elif len(effective_title) < self.builder.TITLE_MIN:
            issues.append({
                "code": "TITLE_TOO_SHORT",
                "message": f"Title is {len(effective_title)} chars (min {self.builder.TITLE_MIN})",
                "severity": "warning",
            })
        elif len(effective_title) > self.builder.TITLE_MAX:
            issues.append({
                "code": "TITLE_TOO_LONG",
                "message": f"Title is {len(effective_title)} chars (max {self.builder.TITLE_MAX})",
                "severity": "warning",
            })

        return issues

    def _check_meta_description(self, meta_desc: str) -> list[dict[str, Any]]:
        """Check meta description length."""
        issues = []

        if not meta_desc:
            issues.append({
                "code": "META_DESC_MISSING",
                "message": "Meta description is empty",
                "severity": "warning",
            })
        elif len(meta_desc) < self.builder.META_DESC_MIN:
            issues.append({
                "code": "META_DESC_TOO_SHORT",
                "message": f"Meta description is {len(meta_desc)} chars (min {self.builder.META_DESC_MIN})",
                "severity": "warning",
            })
        elif len(meta_desc) > self.builder.META_DESC_MAX:
            issues.append({
                "code": "META_DESC_TOO_LONG",
                "message": f"Meta description is {len(meta_desc)} chars (max {self.builder.META_DESC_MAX})",
                "severity": "warning",
            })

        return issues

    def _check_keyphrase(self, keyphrase: str, content: str) -> list[dict[str, Any]]:
        """Check keyphrase quality and presence in content."""
        issues = []

        if not keyphrase or len(keyphrase) < self.builder.KEYPHRASE_MIN:
            issues.append({
                "code": "KEYPHRASE_TOO_SHORT",
                "message": f"Keyphrase is too short: '{keyphrase}'",
                "severity": "info",
            })
        elif content and keyphrase.lower() not in content.lower():
            issues.append({
                "code": "KEYPHRASE_NOT_IN_CONTENT",
                "message": f"Keyphrase '{keyphrase}' not found in content",
                "severity": "warning",
            })

        return issues

    def _check_slug(self, slug: str) -> list[dict[str, Any]]:
        """Check slug quality."""
        issues = []

        if not slug:
            issues.append({
                "code": "SLUG_MISSING",
                "message": "Slug is empty",
                "severity": "warning",
            })
        elif len(slug) > self.builder.SLUG_MAX:
            issues.append({
                "code": "SLUG_TOO_LONG",
                "message": f"Slug is {len(slug)} chars (max {self.builder.SLUG_MAX})",
                "severity": "warning",
            })

        # Check for problematic characters
        if slug and re.search(r'[^a-z0-9\-]', slug):
            issues.append({
                "code": "SLUG_INVALID_CHARS",
                "message": "Slug contains invalid characters",
                "severity": "warning",
            })

        return issues

    def validate(
        self,
        seo: SEOMetadata,
        content: str = "",
    ) -> SEOValidationResult:
        """Validate SEO metadata and return result.

        Returns:
            SEOValidationResult with status "PASS" or "WARN"
        """
        issues = []

        # Run all checks
        issues.extend(self._check_title(seo.title, seo.seo_title))
        issues.extend(self._check_meta_description(seo.meta_description))
        issues.extend(self._check_keyphrase(seo.focus_keyphrase, content))
        issues.extend(self._check_slug(seo.slug))

        # Calculate score
        critical = sum(1 for i in issues if i["severity"] == "critical")
        warnings = sum(1 for i in issues if i["severity"] == "warning")
        infos = sum(1 for i in issues if i["severity"] == "info")

        # Score: 100 - penalties
        score = 100
        score -= critical * 20
        score -= warnings * 10
        score -= infos * 2
        seo_score = max(0, score)

        # Determine status
        status = "PASS" if critical == 0 and warnings <= 1 else "WARN"

        # Build recommendations
        recommendations = []
        for issue in issues:
            if issue["severity"] in ("critical", "warning"):
                recommendations.append(issue["message"])

        return SEOValidationResult(
            status=status,
            seo_score=seo_score,
            issues=issues,
            recommendations=recommendations,
        )


def format_seo_report(result: SEOValidationResult) -> str:
    """Format SEO validation result for display."""
    lines = [
        "SEO VALIDATION",
        f"Status: {result.status}",
        f"Score: {result.seo_score}/100",
    ]

    if result.issues:
        lines.append("Issues:")
        for issue in result.issues:
            severity = issue["severity"].upper()
            lines.append(f"  [{severity}] {issue['code']}: {issue['message']}")

    if result.recommendations:
        lines.append("Recommendations:")
        for rec in result.recommendations:
            lines.append(f"  - {rec}")

    return "\n".join(lines)


def build_seo_for_article(
    article: dict[str, Any],
    wp_base_url: str,
    repair: bool = False,
) -> tuple[SEOMetadata, SEOValidationResult]:
    """Convenience: Build SEO metadata and validate for an article."""
    builder = SEOMetadataBuilder(wp_base_url)
    validator = SEOValidator()

    keywords_raw = article.get("keywords") or []
    if isinstance(keywords_raw, str):
        keywords_list = [keywords_raw]
    elif isinstance(keywords_raw, (list, tuple)):
        keywords_list = [str(k) for k in keywords_raw if k]
    else:
        keywords_list = []
    entities_raw = article.get("entities") or []
    if isinstance(entities_raw, (list, tuple)):
        entities_list = [str(e) for e in entities_raw if e]
    else:
        entities_list = []

    seo = builder.build(
        headline=article.get("headline", ""),
        seo_title=article.get("seo_title"),
        dek=article.get("dek"),
        meta_description=article.get("meta_description"),
        slug=article.get("slug"),
        canonical=article.get("canonical_url"),
        keywords=keywords_list,
        topic=str(article.get("topic") or article.get("category") or ""),
        entities=entities_list,
    )

    content = article.get("article_body", "")
    if article.get("focus_keyphrase"):
        # Writer-chosen SEO fields were already checked against the article; keep them as given.
        seo = replace(
            seo,
            focus_keyphrase=str(article["focus_keyphrase"]),
            seo_title=str(article.get("seo_title") or seo.seo_title),
            meta_description=str(article.get("meta_description") or seo.meta_description),
        )
        return seo, validator.validate(seo, content)
    validation = validator.validate(seo, content)

    if repair and validation.status != "PASS":
        # Deterministic metadata-only repair. It derives text from the frozen
        # headline/dek/body and never changes the article or its claims.
        from .rankmath import is_quality_focus_keyphrase, select_focus_keyphrase

        headline = str(article.get("headline") or "").strip()
        dek = str(article.get("dek") or "").strip()
        body = re.sub(r"\s+", " ", str(content or "")).strip()
        title = seo.seo_title or headline
        if len(title) < SEOMetadataBuilder.TITLE_MIN:
            combined = f"{title} | {headline}".strip(" |") if headline else title
            title = truncate_at_boundary(combined, SEOMetadataBuilder.TITLE_MAX)
        elif len(title) > SEOMetadataBuilder.TITLE_MAX:
            title = truncate_at_boundary(title, SEOMetadataBuilder.TITLE_MAX)
        title = strip_incomplete_title_tail(title)
        title = prefer_focus_at_seo_title_start(title, seo.focus_keyphrase or "")
        if len(title) > SEOMetadataBuilder.TITLE_MAX:
            title = strip_incomplete_title_tail(
                truncate_at_boundary(title, SEOMetadataBuilder.TITLE_MAX)
            )
        description = seo.meta_description or dek or body
        if len(description) < SEOMetadataBuilder.META_DESC_MIN and body:
            description = f"{description} {body}".strip()
        description = truncate_at_boundary(description, SEOMetadataBuilder.META_DESC_MAX)
        keyphrase = seo.focus_keyphrase
        # Never collapse to a garbage single token (historic "spot" bug).
        if not is_quality_focus_keyphrase(keyphrase):
            keyphrase = select_focus_keyphrase(
                keywords=keywords_list,
                headline=headline,
                topic=str(article.get("topic") or article.get("category") or ""),
                entities=entities_list,
                dek=dek,
                existing=keyphrase,
            )
        elif keyphrase.lower() not in str(content).lower():
            # Prefer a quality multi-word phrase present in content; do not pick lone weak tokens.
            rebuilt = select_focus_keyphrase(
                keywords=keywords_list,
                headline=headline,
                topic=str(article.get("topic") or article.get("category") or ""),
                entities=entities_list,
                dek=dek,
            )
            if is_quality_focus_keyphrase(rebuilt):
                keyphrase = rebuilt
        seo = replace(seo, seo_title=title, meta_description=description, focus_keyphrase=keyphrase)
        validation = validator.validate(seo, content)

    return seo, validation
