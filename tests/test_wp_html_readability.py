"""Regression tests: WordPress HTML readability / sectioning pipeline.

Encodes structural requirements for CMS-ready article HTML:
- Logical H2 sections with descriptive headings from existing content
- H3 only for genuine subsections (esp. FAQ questions)
- Short paragraphs (no 300-500 word walls)
- Semantic spacing via <p>/<h2>/<h3> (no <br><br> spam)
- Conclusion / FAQs / Sources each get H2
- FAQ questions as H3 with answer paragraphs
- TOC anchors matching final headings
- Fact/link preservation (formatting only)
"""

from __future__ import annotations

import html as htmlmod
import re

from newsagent_v2.wordpress.config import WordPressConfig
from newsagent_v2.wordpress.html_formatter import ArticleHtmlFormatter


class MockTransport:
    def __call__(self, method, url, **kwargs):
        return {"ok": False, "error": "offline"}


def _formatter() -> ArticleHtmlFormatter:
    return ArticleHtmlFormatter(
        WordPressConfig("https://example.com", "test", "pass"),
        MockTransport(),
    )


# Wall-of-text body shaped like frozen evt-e159059f:
# bold closing markers, mashed FAQs, one giant lead block, no ## headings.
WALL_OF_TEXT_BODY = (
    "US spot bitcoin exchange-traded funds attracted $998.9 million in net inflows "
    "on Monday, marking their largest single-day haul since October and their biggest "
    "daily total of 2026. The funds recorded $998.9 million in daily net inflows, "
    "surpassing the previous 2026 high of $844 million on Jan. BlackRock's iShares "
    "Bitcoin Trust (IBIT) led the inflows with $381 million, according to data from "
    "Farside Investors. The ARK 21Shares Bitcoin ETF (ARKB) followed at $289 million, "
    "while Fidelity's Wise Origin Bitcoin Fund (FBTC) drew approximately $239 million. "
    "Monday's activity represented the ninth-largest daily inflow since the funds began "
    "trading in January 2024. The surge arrived as bitcoin extended its recent price "
    "recovery. The cryptocurrency traded at $85,430 at publication time, up 4.7% over "
    "24 hours and 12.3% over the past month. It briefly climbed above $87,200 on Monday, "
    "according to CoinGecko. Bitcoin has risen 44% this quarter to approximately $85,000, "
    "outperforming every other major asset including gold. Monday's inflow marked the "
    "first three-day streak of gains for two weeks. The activity follows a period of "
    "volatility that included a failed Senate cloture vote on the Clarity Act and a "
    "Federal Reserve interest-rate increase. That suggests strong institutional interest "
    "in the cryptocurrency despite tensions in the wider macroeconomy, particularly "
    "fiscal debt concerns across the advanced world. The month-to-date tally has reached "
    "$1.31 billion, following August's $3.52 billion inflow. However, US spot bitcoin "
    "ETFs have posted approximately $464 million in net outflows so far in 2026. US spot "
    "ether ETFs also saw significant activity on Monday, attracting around $270 million "
    "in their biggest daily inflow of 2026. US spot XRP ETFs recorded no net flows, "
    "leaving cumulative net inflows at about $1.71 billion. CryptoQuant analyst Julio "
    "Moreno noted Monday that bitcoin had moved above its 365-day moving average, which "
    "he described as the final signal needed to confirm a new bull market. "
    "**Conclusion / What Happens Next**\n\n"
    "The $998.9 million inflow extends a three-day positive streak for US spot bitcoin "
    "ETFs. Institutional flows will continue to be monitored against bitcoin's price "
    "action and broader macroeconomic conditions, including ongoing fiscal debt concerns "
    "across advanced economies. **FAQs**\n\n"
    "**What was the largest individual ETF inflow on Monday?**\n"
    "BlackRock's IBIT led with $381 million, followed by ARKB at $289 million and FBTC "
    "at approximately $239 million. **When did bitcoin last trade at record highs?**\n"
    "Bitcoin reached a record high of roughly $126,200 in October. "
    "**How much have US spot bitcoin ETFs lost or gained in 2026 overall?**\n"
    "Despite Monday's surge, the funds have posted about $464 million in net outflows "
    "so far in 2026. **What other cryptocurrency ETFs saw inflows?**\n"
    "US spot ether ETFs attracted around $270 million on Monday, their biggest daily "
    "inflow of 2026."
)

EVIDENCE = [
    {
        "url": "https://farside.co.uk/btc/",
        "source": "Farside Investors",
        "title": "Bitcoin ETF Flow Data",
    },
    {
        "url": "https://www.coingecko.com/en/coins/bitcoin",
        "source": "CoinGecko",
        "title": "Bitcoin Price",
    },
]

FACT_SNIPPETS = [
    "$998.9 million",
    "BlackRock's iShares Bitcoin Trust (IBIT)",
    "$381 million",
    "Farside Investors",
    "$85,430",
    "CoinGecko",
    "Julio Moreno",
    "$126,200",
    "$464 million",
    "$270 million",
]


def _paragraph_texts(html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<p>(.*?)</p>", html, flags=re.DOTALL | re.IGNORECASE)]


def _strip_tags(text: str) -> str:
    return htmlmod.unescape(re.sub(r"<[^>]+>", "", text))




def _h2_texts(html: str) -> list[str]:
    return [
        _strip_tags(m.group(1)).strip()
        for m in re.finditer(r"<h2\b[^>]*>(.*?)</h2>", html, flags=re.DOTALL | re.IGNORECASE)
    ]


def _h3_texts(html: str) -> list[str]:
    return [
        _strip_tags(m.group(1)).strip()
        for m in re.finditer(r"<h3\b[^>]*>(.*?)</h3>", html, flags=re.DOTALL | re.IGNORECASE)
    ]


def test_wall_of_text_gets_multiple_h2_sections():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    h2s = [h for h in _h2_texts(result.html_content) if h.lower() != "table of contents"]
    assert len(h2s) >= 4, f"expected multiple editorial H2s, got {h2s}"
    closing = {"conclusion / what happens next", "faqs", "sources"}
    body_h2s = [h for h in h2s if h.lower() not in closing]
    assert len(body_h2s) >= 2, f"expected titled body sections, got {h2s}"


def test_no_giant_paragraphs():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    for para in _paragraph_texts(result.html_content):
        words = _strip_tags(para).split()
        assert len(words) <= 160, f"paragraph too long ({len(words)} words): {para[:120]}"


def test_no_br_br_spam():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    html = result.html_content.lower()
    assert "<br><br>" not in html
    assert "<br/><br/>" not in html
    assert html.count("<br") <= 2


def test_conclusion_faqs_sources_are_h2():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    h2s_lower = [h.lower() for h in _h2_texts(result.html_content)]
    assert any("conclusion" in h for h in h2s_lower)
    assert any(h == "faqs" or h.startswith("faq") for h in h2s_lower)
    assert any(h == "sources" for h in h2s_lower)


def test_faq_questions_are_h3_with_answer_paragraphs():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    html = result.html_content
    h3s = _h3_texts(html)
    assert len(h3s) >= 4
    assert any("largest individual ETF inflow" in h for h in h3s)
    assert any("record highs" in h for h in h3s)
    assert re.search(
        r"<h3\b[^>]*>What was the largest individual ETF inflow on Monday\?</h3>\s*<p>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )


def test_toc_anchors_match_final_headings():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    assert "Table of Contents" in result.toc or "Table of Contents" in result.html_content
    # TOC / headings metadata is H2-only
    assert result.headings
    assert all(h["level"] == 2 for h in result.headings)
    for h in result.headings:
        assert h["anchor"]
        assert f'id="{h["anchor"]}"' in result.html_content
        assert f'href="#{h["anchor"]}"' in result.html_content
        assert h["text"] in _h2_texts(result.html_content)
    # FAQ H3s remain anchored in the body even though omitted from TOC
    for h3 in _h3_texts(result.html_content):
        assert f">{h3}</h3>" in result.html_content or h3 in result.html_content
        assert f'href="#' not in result.toc or h3 not in result.toc


def test_structure_order_intro_toc_sections_closing():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    html = result.html_content
    toc_pos = html.lower().find("table of contents")
    first_p = html.lower().find("<p>")
    assert first_p != -1 and toc_pos != -1
    assert first_p < toc_pos, "intro should precede TOC"
    concl = re.search(r"<h2\b[^>]*>[^<]*Conclusion", html, flags=re.I)
    faqs = re.search(r"<h2\b[^>]*>\s*FAQs\s*</h2>", html, flags=re.I)
    sources = re.search(r"<h2\b[^>]*>\s*Sources\s*</h2>", html, flags=re.I)
    assert concl and faqs and sources
    assert toc_pos < concl.start() < faqs.start() < sources.start()


def test_headings_do_not_invent_facts():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    body_lower = WALL_OF_TEXT_BODY.lower()
    stop = {
        "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "as",
        "at", "by", "from", "into", "over", "next", "what", "happens", "table",
        "contents", "sources", "faqs", "faq", "conclusion", "read", "also",
    }
    for h in result.headings:
        text = h["text"].strip()
        if (
            text.lower() in {"table of contents", "sources", "faqs", "conclusion / what happens next", "conclusion"}
            or "conclusion" in text.lower()
        ):
            continue
        for token in re.findall(r"[A-Za-z0-9$]+", text):
            if token.lower() in stop:
                continue
            assert token.lower() in body_lower, f"heading invents token {token!r} via {text!r}"


def test_facts_and_source_links_preserved():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    plain = _strip_tags(result.html_content)
    for snippet in FACT_SNIPPETS:
        assert snippet in plain, f"missing fact snippet: {snippet}"
    assert 'href="https://farside.co.uk/btc/"' in result.html_content
    assert 'href="https://www.coingecko.com/en/coins/bitcoin"' in result.html_content


def test_markdown_headings_still_work():
    """Existing ## / ### path must keep working."""
    article = """## Introduction

Short intro paragraph about the market move.

## Market Analysis

Details on flows and institutions.

### Subsection Detail

More detail here.

## Conclusion / What Happens Next

Flows will be monitored.

## FAQs

### What happened?

Funds saw large inflows.
"""
    result = _formatter().format_article(article, evidence=EVIDENCE, topic="")
    assert '<h2 id="introduction">Introduction</h2>' in result.html_content
    assert '<h3 id="subsection-detail">Subsection Detail</h3>' in result.html_content
    assert any(h["text"] == "Introduction" for h in result.headings)


def test_existing_short_paragraphs_not_over_merged():
    article = (
        "First idea stays alone.\n\n"
        "Second idea stays alone.\n\n"
        "## Conclusion / What Happens Next\n\n"
        "Watch the next session.\n\n"
        "## FAQs\n\n"
        "### Was this confirmed?\n\n"
        "Yes, according to the filing."
    )
    result = _formatter().format_article(article, evidence=[], topic="")
    paras = [_strip_tags(p).strip() for p in _paragraph_texts(result.html_content)]
    assert "First idea stays alone." in paras
    assert "Second idea stays alone." in paras


def test_reading_column_wrapper_and_scoped_css():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    html = result.html_content
    assert 'class="na-article-reading-column newsagent-article"' in html
    assert "<style>" in html
    assert ".na-article-reading-column" in html
    # max-width in the comfortable 720-820px band
    assert "max-width:780px" in html
    # Scoped only — no bare body/html global overrides
    assert "body{" not in html.replace(" ", "")
    assert "html{" not in html.replace(" ", "")


def test_h2_titles_are_short_editorial_not_sentence_copy():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    closing = {"conclusion / what happens next", "faqs", "sources", "table of contents"}
    body_h2s = [h for h in _h2_texts(result.html_content) if h.lower() not in closing]
    assert body_h2s, "expected editorial body H2s"
    for title in body_h2s:
        words = title.split()
        assert len(words) <= 10, f"H2 too long/sentence-like: {title!r}"
        assert len(title) <= 72, f"H2 char length too long: {title!r}"
        # Must not look like a copied lead sentence opener
        assert not title.lower().startswith("monday"), title
        assert "represented the" not in title.lower()
        assert "follows a period" not in title.lower()


def test_toc_lists_h2_single_faqs_sources_not_faq_h3s():
    result = _formatter().format_article(WALL_OF_TEXT_BODY, evidence=EVIDENCE, topic="")
    toc = result.toc
    assert "FAQs" in toc
    assert "Sources" in toc
    assert toc.count('href="#faqs"') == 1
    assert toc.count('href="#sources"') == 1
    # Per-FAQ questions must not appear as TOC entries
    for q in _h3_texts(result.html_content):
        assert q not in toc
    # Only H2 list items
    assert "toc-h3" not in toc
    assert "toc-h2" in toc
