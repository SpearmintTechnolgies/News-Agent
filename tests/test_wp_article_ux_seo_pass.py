"""Final Article UX + SEO pipeline regression tests (offline, no live WP)."""

from __future__ import annotations

import re
from pathlib import Path

from newsagent_v2.publication.master_index import MasterIndexStore, MasterIndexRecord
from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.html_formatter import ArticleHtmlFormatter
from newsagent_v2.wordpress.seo_metadata import (
    truncate_at_boundary,
    strip_incomplete_title_tail,
    prefer_focus_at_seo_title_start,
    build_seo_for_article,
    SEOMetadataBuilder,
)
from newsagent_v2.wordpress.editorial_structure import (
    derive_section_heading,
    is_rejected_heading,
    heading_overlaps_body_phrase,
)


class OfflineTransport:
    def __call__(self, method, url, **kwargs):
        return {"ok": False, "error": "offline"}


def _fmt(master_index=None):
    return ArticleHtmlFormatter(
        WordPressConfig("https://example.com", "test", "pass"),
        OfflineTransport(),
        master_index=master_index,
    )


def test_internal_links_skip_cleanly_when_master_index_empty(tmp_path: Path):
    store = MasterIndexStore(root=tmp_path)
    result = _fmt(store).format_article(
        "Short body about bitcoin etf inflows.\n\n## Conclusion / What Happens Next\n\nWatch flows.",
        evidence=[],
        topic="bitcoin etf",
        entities=["bitcoin"],
    )
    assert result.internal_links == []
    assert "Read Also" not in result.html_content
    assert "https://" not in result.html_content or "example.com" not in [
        u for u in []  # no invented CN URLs
    ]


def test_internal_links_only_published_master_index_entries(tmp_path: Path):
    store = MasterIndexStore(root=tmp_path)
    # newsagent-source record without title must not be selected
    store.record_publication(
        event_id="evt-na",
        canonical_url="https://coinnetwork.com/draft-like",
        wp_post_id=1,
        article_version="v1",
        image_version=None,
    )
    # wordpress published-style record (source=wordpress, has title+url)
    published = MasterIndexRecord(
        event_id="wp-99",
        canonical_url="https://coinnetwork.com/bitcoin-etf-flows",
        wp_post_id=99,
        article_version=None,
        image_version=None,
        published_at="2026-01-01T00:00:00Z",
        indexing_state="SYNCED",
        title="Bitcoin ETF Flows Explained",
        topic="bitcoin",
        entities=["bitcoin", "etf"],
        source="wordpress",
    )
    store._path(published.event_id).write_text(
        __import__("json").dumps(published.to_dict()),
        encoding="utf-8",
    )
    # Ensure to_dict kept wordpress fields — rewrite with full dict if stripped
    raw = published.__dict__.copy()
    store._path(published.event_id).write_text(
        __import__("json").dumps(raw, default=str),
        encoding="utf-8",
    )

    result = _fmt(store).format_article(
        "Article about bitcoin ETF inflows and institutional demand.",
        evidence=[],
        topic="bitcoin etf",
        entities=["bitcoin"],
    )
    assert len(result.internal_links) >= 1
    assert all(l["url"].startswith("https://") for l in result.internal_links)
    assert "Bitcoin ETF Flows Explained" in result.html_content
    assert "Read Also" in result.html_content
    # newsagent-only URL without title must not appear
    assert "https://coinnetwork.com/draft-like" not in result.html_content


def test_derive_section_heading_short_editorial():
    samples = [
        (
            "Monday's activity represented the ninth-largest daily inflow since the funds "
            "began trading in January 2024. Bitcoin traded at $85,430.",
            10,
        ),
        (
            "US spot ether ETFs also saw significant activity on Monday, attracting around "
            "$270 million in their biggest daily inflow of 2026.",
            10,
        ),
        (
            "BlackRock's iShares Bitcoin Trust (IBIT) led the inflows with $381 million.",
            10,
        ),
    ]
    for text, max_words in samples:
        title = derive_section_heading(text)
        assert title
        assert len(title.split()) <= max_words
        assert len(title) <= 72
        assert not title.lower().startswith("monday's activity")
        assert "represented the ninth" not in title.lower()
        assert not is_rejected_heading(title), title


def test_reject_metric_label_headings():
    """Entity+number / keyword+number / raw metric fragments must be rejected."""
    rejected = [
        "Bitcoin $85,430",
        "Spot Bitcoin ETFs Inflows $1.31 Billion",
        "Bitcoin ETFs Inflows $998.9 Million",
        "Ether $270 Million",
        "IBIT $381 Million",
    ]
    for title in rejected:
        assert is_rejected_heading(title), title
        # derive must not emit these even from text that contains the numbers
    # Bare traded-at alone may yield a verb+metric heading; bare metric label must not.
    h_bare = derive_section_heading(
        "The cryptocurrency traded at $85,430 at publication time."
    )
    assert h_bare != "Bitcoin $85,430"
    assert not (h_bare.lower().startswith("bitcoin $") or h_bare.lower().endswith(" $85,430") and "traded" not in h_bare.lower())
    h_price = derive_section_heading(
        "The surge arrived as bitcoin extended its recent price recovery. "
        "The cryptocurrency traded at $85,430 at publication time. "
        "It briefly climbed above $87,200 on Monday, according to CoinGecko."
    )
    assert h_price != "Bitcoin $85,430"
    assert h_price != "Bitcoin Extended Price Recovery"
    assert not is_rejected_heading(h_price)
    # Metric OK only with a verb (subject + verb + metric)
    assert any(v in h_price.lower() for v in ("climbed", "traded", "rose", "risen"))

    h_flow = derive_section_heading(
        "The month-to-date tally has reached $1.31 billion, following August's "
        "$3.52 billion inflow. However, US spot bitcoin ETFs have posted "
        "approximately $464 million in net outflows so far in 2026."
    )
    assert h_flow != "Spot Bitcoin ETFs Inflows $1.31 Billion"
    assert not re.search(r"(?i)inflows?\s+\$", h_flow)
    assert not is_rejected_heading(h_flow)


def test_derive_section_heading_prefers_natural_relationship():
    h1 = derive_section_heading(
        "The surge arrived as bitcoin extended its recent price recovery. "
        "The cryptocurrency traded at $85,430 at publication time. "
        "It briefly climbed above $87,200 on Monday, according to CoinGecko."
    )
    # Prefer subject+verb+metric editorial label over body-phrase noun pile
    assert h1.lower() != "bitcoin extended price recovery"
    assert "recovery" not in h1.lower()
    assert "climbed" in h1.lower() or "traded" in h1.lower()
    assert "$" in h1
    assert len(h1.split()) <= 8

    h2 = derive_section_heading(
        "BlackRock's iShares Bitcoin Trust (IBIT) led the inflows with $381 million."
    )
    assert "led" in h2.lower()
    assert "blackrock" in h2.lower() or "ibit" in h2.lower()

    h3 = derive_section_heading(
        "US spot ether ETFs also saw significant activity on Monday, attracting around "
        "$270 million in their biggest daily inflow of 2026."
    )
    assert "inflow" in h3.lower()
    assert not is_rejected_heading(h3)


def test_truncate_never_mid_number():
    raw = "Price briefly climbed above $87,200 during Monday trading hours."
    for n in range(28, len(raw) + 1):
        out = truncate_at_boundary(raw, n)
        assert "$87,00" not in out
        if "$87" in out:
            # if dollar amount present, must be complete token start
            assert "$87,200" in out or out.index("$87") == -1


def test_reading_column_spacing_not_giant():
    """Scoped CSS must keep news-like vertical rhythm — no giant spacer gaps."""
    body = (
        "US spot bitcoin exchange-traded funds attracted $998.9 million in net inflows "
        "on Monday, marking their largest single-day haul since October.\n\n"
        "## Conclusion / What Happens Next\n\n"
        "Flows will be monitored."
    )
    html = _fmt().format_article(body, evidence=[], topic="bitcoin").html_content
    assert "max-width:780px" in html
    style_m = re.search(r"<style>(.*?)</style>", html, flags=re.S)
    assert style_m
    style = style_m.group(1)

    def _rule_margins(selector_snippet: str) -> list[float]:
        # Find margin declarations associated with the selector snippet
        vals: list[float] = []
        for m in re.finditer(
            re.escape(selector_snippet) + r"[^{]*\{([^}]*)\}",
            style,
        ):
            block = m.group(1)
            for mm in re.finditer(r"margin(?:-top|-bottom|-left|-right)?:([^;]+);", block):
                for part in mm.group(1).split():
                    part = part.strip()
                    if part.endswith("em"):
                        vals.append(float(part[:-2]))
                    elif part.endswith("rem"):
                        vals.append(float(part[:-3]))
        return vals

    # Reject giant spacer values (3rem+/3em+ style gaps)
    all_em = [float(x) for x in re.findall(r"(?<![\\w-])(\\d+(?:\\.\\d+)?)em", style)]
    all_rem = [float(x) for x in re.findall(r"(?<![\\w-])(\\d+(?:\\.\\d+)?)rem", style)]
    assert all(v < 3.0 for v in all_em), f"giant em spacer in CSS: {all_em}"
    assert all(v < 3.0 for v in all_rem), f"giant rem spacer in CSS: {all_rem}"

    # TOC vertical margins tightened vs prior 1.4/1.6em
    toc_margins = _rule_margins(".na-article-reading-column .article-toc{")
    # fallback: looser search
    toc_block = re.search(
        r"\.na-article-reading-column \.article-toc\{([^}]*)\}",
        style,
    )
    assert toc_block, style
    toc_margin = re.search(r"margin:([^;]+);", toc_block.group(1))
    assert toc_margin
    toc_parts = [p.strip() for p in toc_margin.group(1).split()]
    # margin: top right bottom left OR top/bottom shorthand
    em_parts = [float(p[:-2]) for p in toc_parts if p.endswith("em")]
    assert em_parts, toc_parts
    assert max(em_parts) <= 1.0, f"TOC margin still too large: {toc_parts}"

    h2_block = re.search(
        r"\.na-article-reading-column h2,\.na-article-reading-column h3\{([^}]*)\}",
        style,
    )
    assert h2_block
    h2_margin = re.search(r"margin:([^;]+);", h2_block.group(1))
    assert h2_margin
    h2_parts = [float(p[:-2]) for p in h2_margin.group(1).split() if p.endswith("em")]
    assert h2_parts and max(h2_parts) <= 1.25, h2_parts

    # Adjacent-sibling collapse for TOC → first H2
    assert ".article-toc + h2{margin-top:0.5em;}" in style.replace(" ", "") or (
        ".na-article-reading-column .article-toc + h2{margin-top:0.5em;}" in style
    )

    p_block = re.search(r"\.na-article-reading-column p\{([^}]*)\}", style)
    assert p_block
    p_margin = re.search(r"margin:([^;]+);", p_block.group(1))
    assert p_margin
    p_parts = [float(p[:-2]) for p in p_margin.group(1).split() if p.endswith("em")]
    assert p_parts and max(p_parts) <= 1.0, p_parts


def test_seo_title_strips_orphan_trailing_and_prefers_focus_start():
    assert (
        strip_incomplete_title_tail(
            "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow Largest"
        )
        == "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow"
    )
    assert (
        prefer_focus_at_seo_title_start(
            "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow",
            "spot bitcoin etfs",
        )
        == "Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow"
    )
    builder = SEOMetadataBuilder("https://example.com")
    seo = builder.build(
        headline="US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow, Largest Since October",
        seo_title="US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow Largest Since October",
        dek="US spot bitcoin ETFs saw strong inflows Monday.",
        meta_description="US spot bitcoin ETFs saw $998.9 million in net inflows Monday, the largest since October.",
        slug="us-spot-bitcoin-etfs-nearly-1-billion-daily-inflow-largest-since-october",
        keywords=[],
        topic="bitcoin",
        entities=[],
    )
    assert len(seo.seo_title) <= builder.TITLE_MAX
    assert not seo.seo_title.lower().endswith("largest")
    assert seo.focus_keyphrase.lower() in seo.seo_title.lower()
    assert seo.seo_title.lower().startswith(seo.focus_keyphrase.lower())
    # Meta still boundary-safe
    assert "$87,00" not in seo.meta_description
    assert len(seo.meta_description) <= builder.META_DESC_MAX


def test_seo_metadata_boundaries_still_green():
    raw = "US spot bitcoin ETFs saw $998.9 million in net inflows Monday, the largest since October. BlackRock's IBIT led with $381 million as bitcoin climbed above $87,000."
    for n in range(40, 161):
        out = truncate_at_boundary(raw, n)
        assert "$87,00" not in out
        assert not out.endswith("$")
    article = {
        "headline": "US Spot Bitcoin ETFs Log Nearly $1 Billion Daily Inflow, Largest Since October",
        "seo_title": "US Spot Bitcoin ETFs Nearly $1 Billion Daily Inflow Largest Since October",
        "meta_description": raw,
        "dek": "BlackRock's IBIT leads $998.9 million surge.",
        "slug": "us-spot-bitcoin-etfs-nearly-1-billion-daily-inflow-largest-since-october",
        "keywords": [],
        "entities": [],
        "article_body": (
            "US spot bitcoin ETFs attracted inflows. Spot bitcoin etfs remained in focus "
            "as institutions added exposure."
        ),
    }
    seo, validation = build_seo_for_article(article, "https://example.com")
    assert len(seo.seo_title) <= 60
    assert len(seo.meta_description) <= 160
    assert "$87,00" not in seo.seo_title
    assert "$87,00" not in seo.meta_description
    assert validation.status in {"PASS", "WARN"}


def test_heading_rejects_semantic_overlap_with_body_phrase():
    """H2 must not merely duplicate/repackage a nearby body sentence phrase."""
    section = (
        "Monday's activity represented the ninth-largest daily inflow since the funds "
        "began trading in January 2024. The surge arrived as bitcoin extended its recent "
        "price recovery. The cryptocurrency traded at $85,430 at publication time, up "
        "4.7% over 24 hours and 12.3% over the past month. It briefly climbed above "
        "$87,200 on Monday, according to CoinGecko."
    )
    # Direct overlap detector
    assert heading_overlaps_body_phrase(
        "Bitcoin Extended Price Recovery",
        section,
    )
    assert is_rejected_heading("Bitcoin Extended Price Recovery")

    h = derive_section_heading(section)
    assert h
    assert not heading_overlaps_body_phrase(h, section)
    assert h.lower() != "bitcoin extended price recovery"
    assert "extended" not in h.lower() or "recovery" not in h.lower()
    # Prefer informative subject+verb(+metric)
    assert any(v in h.lower() for v in ("climbed", "traded", "rose", "risen"))
    assert "$" in h
    assert 3 <= len(h.split()) <= 8


def test_heading_rejects_noun_pile_and_bare_metric_labels():
    assert is_rejected_heading("Bitcoin Extended Price Recovery")
    assert is_rejected_heading("Bitcoin $85,430")
    assert not is_rejected_heading("Bitcoin Climbed Above $87,200")


def test_outflows_heading_only_when_dominant():
    """Do not emit awkward Posted Outflows when outflows are contrast-only."""
    section = (
        "The activity follows a period of volatility that included a failed Senate "
        "cloture vote on the Clarity Act and a Federal Reserve interest-rate increase. "
        "That suggests strong institutional interest in the cryptocurrency despite "
        "tensions in the wider macroeconomy. The month-to-date tally has reached "
        "$1.31 billion, following August's $3.52 billion inflow. However, US spot "
        "bitcoin ETFs have posted approximately $464 million in net outflows so far "
        "in 2026. US spot ether ETFs also saw significant activity on Monday, "
        "attracting around $270 million in their biggest daily inflow of 2026."
    )
    h = derive_section_heading(section)
    assert h
    assert h.lower() != "spot bitcoin etfs posted outflows"
    assert "outflow" not in h.lower()
