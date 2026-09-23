"""WordPress HTML formatter tests - ZERO live API calls."""

from __future__ import annotations

import sys
sys.path.insert(0, 'src')

from newsagent_v2.wordpress.html_formatter import ArticleHtmlFormatter, FormattedArticle
from newsagent_v2.wordpress.config import WordPressConfig


class MockTransport:
    """Mock transport for internal link discovery."""
    def __init__(self):
        self.posts = [
            {"id": 1, "title": {"rendered": "Bitcoin Price Update"}, "link": "https://example.com/bitcoin-price", "status": "publish"},
            {"id": 2, "title": {"rendered": "Crypto Market Analysis"}, "link": "https://example.com/crypto-market", "status": "publish"},
        ]

    def __call__(self, method, url, **kwargs):
        if "posts?search=" in url:
            # Formatter requests published posts only
            assert "status=publish" in url
            return {"ok": True, "payload": self.posts}
        return {"ok": False, "error": "unknown"}


def test_toc_generation():
    """Test TOC is generated from headings."""
    print("Test 1: TOC generation...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = """## Introduction
This is the intro.

## Market Analysis
Details here.

### Subsection A
More details.

## Conclusion
Wrap up."""

    result = formatter.format_article(article, evidence=[], topic="")

    assert "Table of Contents" in result.toc
    assert 'href="#introduction"' in result.toc
    assert 'href="#market-analysis"' in result.toc
    # H3 subsections stay in body anchors but are NOT dumped into main TOC
    assert 'href="#subsection-a"' not in result.toc
    assert 'id="subsection-a"' in result.html_content
    assert len(result.headings) == 3  # H2s only in TOC metadata
    assert all(h["level"] == 2 for h in result.headings)
    print("  PASS")


def test_heading_transformation():
    """Test markdown headings become HTML with anchors."""
    print("Test 2: Heading transformation...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "## Market Update\nContent here.\n\n### Bitcoin\nDetails."

    result = formatter.format_article(article, evidence=[], topic="")

    assert '<h2 id="market-update">Market Update</h2>' in result.html_content
    assert '<h3 id="bitcoin">Bitcoin</h3>' in result.html_content
    print("  PASS")


def test_source_links_from_evidence():
    """Test source links use only evidence URLs."""
    print("Test 3: Source links from evidence...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "This is the article body."
    evidence = [
        {"url": "https://reuters.com/article/123", "source": "Reuters", "title": "Reuters Article"},
        {"url": "", "source": "No URL", "title": "Missing URL"},  # Should be skipped
        {"url": "https://coindesk.com/news", "source": "CoinDesk", "title": ""},
    ]

    result = formatter.format_article(article, evidence=evidence, topic="")

    assert "Sources" in result.html_content
    assert 'href="https://reuters.com/article/123"' in result.html_content
    assert "Reuters Article" in result.html_content
    assert 'href="https://coindesk.com/news"' in result.html_content
    # Missing URL should not appear
    assert "No URL" not in result.html_content
    assert len(result.source_links) == 2
    print("  PASS")


def test_internal_links_wp_rest():
    """Test internal links discovered via WP REST."""
    print("Test 4: Internal links via WP REST...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "Article content."
    result = formatter.format_article(article, evidence=[], topic="bitcoin")

    assert "Read Also" in result.html_content
    assert 'href="https://example.com/bitcoin-price"' in result.html_content
    assert len(result.internal_links) > 0
    print("  PASS")


def test_paragraph_formatting():
    """Test paragraphs are wrapped in <p> tags."""
    print("Test 5: Paragraph formatting...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "First paragraph.\n\nSecond paragraph after blank line."

    result = formatter.format_article(article, evidence=[], topic="")

    assert "<p>First paragraph.</p>" in result.html_content
    assert "<p>Second paragraph" in result.html_content
    print("  PASS")


def test_blockquote_handling():
    """Test blockquotes from markdown-style quotes."""
    print("Test 6: Blockquote handling...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "> This is a quote."

    result = formatter.format_article(article, evidence=[], topic="")

    assert "<blockquote>This is a quote.</blockquote>" in result.html_content
    print("  PASS")


def test_list_handling():
    """Test lists are formatted."""
    print("Test 7: List handling...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "- Item one\n- Item two\n- Item three"

    result = formatter.format_article(article, evidence=[], topic="")

    assert "<ul>" in result.html_content
    assert "<li>Item one</li>" in result.html_content
    assert "</ul>" in result.html_content
    print("  PASS")


def test_html_escaping():
    """Test HTML entities are escaped."""
    print("Test 8: HTML escaping...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "Price < $100 & rising"

    result = formatter.format_article(article, evidence=[], topic="")

    assert "<script>" not in result.html_content
    assert "&lt;" in result.html_content or "< $100" not in result.html_content
    print("  PASS")


def test_no_invention():
    """Test no URLs are invented."""
    print("Test 9: No URL invention...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = "No evidence provided."
    # Empty evidence - no sources section should appear
    result = formatter.format_article(article, evidence=[], topic="")

    # Sources section should not appear when no evidence
    assert "<section class=\"article-sources\">" not in result.html_content
    assert result.source_links == []
    print("  PASS")


def test_full_article_assembly():
    """Test complete article assembly with all sections."""
    print("Test 10: Full article assembly...")
    config = WordPressConfig("https://example.com", "test", "pass")
    transport = MockTransport()
    formatter = ArticleHtmlFormatter(config, transport)

    article = """## Introduction
The crypto market moves fast.

## Analysis
Data shows trends.

### Key Points
- Point one
- Point two

> Expert opinion here.

## Sources Required"""

    evidence = [
        {"url": "https://example.com/source1", "source": "Source 1", "title": "Title 1"},
    ]

    result = formatter.format_article(article, evidence=evidence, topic="crypto")

    # TOC present
    assert "Table of Contents" in result.html_content
    # Body headings
    assert "<h2 id=\"introduction\">Introduction</h2>" in result.html_content
    assert "<h2 id=\"analysis\">Analysis</h2>" in result.html_content
    assert "<h3 id=\"key-points\">Key Points</h3>" in result.html_content
    # Blockquote
    assert "<blockquote>Expert opinion here.</blockquote>" in result.html_content
    # Sources
    assert "<section class=\"article-sources\">" in result.html_content
    # Read Also
    assert "<section class=\"article-read-also\">" in result.html_content

    print("  PASS")


if __name__ == "__main__":
    test_toc_generation()
    test_heading_transformation()
    test_source_links_from_evidence()
    test_internal_links_wp_rest()
    test_paragraph_formatting()
    test_blockquote_handling()
    test_list_handling()
    test_html_escaping()
    test_no_invention()
    test_full_article_assembly()

    print("\n=== ALL HTML FORMATTER TESTS PASSED ===")
