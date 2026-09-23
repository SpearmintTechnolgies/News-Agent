"""WordPress SEO metadata tests - ZERO live API calls."""

from __future__ import annotations

import sys
sys.path.insert(0, 'src')

from newsagent_v2.wordpress.seo_metadata import (
    SEOMetadataBuilder,
    SEOValidator,
    SEOMetadata,
    SEOValidationResult,
    build_seo_for_article,
    format_seo_report,
)


def test_seo_extraction():
    """Test SEO metadata extraction from article."""
    print("Test 1: SEO extraction...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Bitcoin Price Surges Amid Market Optimism",
        seo_title=None,
        dek="BTC reaches new monthly high as institutional interest grows",
        meta_description=None,
        slug="bitcoin-price-surge",
        canonical=None,
    )

    assert seo.title == "Bitcoin Price Surges Amid Market Optimism"
    assert seo.seo_title == "Bitcoin Price Surges Amid Market Optimism"
    assert "btc reaches" in seo.meta_description.lower()
    assert seo.slug == "bitcoin-price-surge"
    assert seo.focus_keyphrase  # Should have extracted keyphrase
    print("  PASS")
    print(f"  Keyphrase: {seo.focus_keyphrase}")


def test_seo_title_preference():
    """Test explicit SEO title is preferred over headline."""
    print("Test 2: SEO title preference...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Long Headline Here",
        seo_title="Short SEO Title",
        dek="",
        meta_description=None,
        slug=None,
        canonical=None,
    )

    assert seo.title == "Long Headline Here"
    assert seo.seo_title == "Short SEO Title"
    print("  PASS")


def test_meta_description_preference():
    """Test explicit meta description is preferred over dek."""
    print("Test 3: Meta description preference...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Test",
        seo_title=None,
        dek="Dek content here",
        meta_description="Explicit meta description",
        slug=None,
        canonical=None,
    )

    assert seo.meta_description == "Explicit meta description"
    print("  PASS")


def test_slug_generation():
    """Test slug generation from headline."""
    print("Test 4: Slug generation...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Bitcoin Price Prediction: Market Analysis for 2024",
        seo_title=None,
        dek="",
        meta_description=None,
        slug=None,  # No slug provided
        canonical=None,
    )

    assert "bitcoin-price-prediction" in seo.slug
    assert "market-analysis" in seo.slug
    assert len(seo.slug) <= 50
    print("  PASS")
    print(f"  Generated slug: {seo.slug}")


def test_validation_pass():
    """Test SEO validation passing."""
    print("Test 5: SEO validation PASS...")
    validator = SEOValidator()

    seo = SEOMetadata(
        title="Bitcoin Price Surges Amid Growing Market Optimism and Institutional Interest",
        seo_title="Bitcoin Price Surges Amid Growing Market Optimism",
        meta_description="Bitcoin reaches new monthly highs as institutional investors increase allocation to cryptocurrency markets amid positive regulatory developments",
        focus_keyphrase="bitcoin price",
        slug="bitcoin-price-surge",
        canonical_url=None,
        open_graph_title=None,
        open_graph_description=None,
        twitter_title=None,
        twitter_description=None,
    )

    result = validator.validate(seo, content="This content discusses bitcoin price movements extensively.")

    assert result.status == "PASS", f"Expected PASS, got {result.status}"
    assert result.seo_score >= 90
    print("  PASS")
    print(f"  Score: {result.seo_score}")


def test_validation_warn():
    """Test SEO validation warnings."""
    print("Test 6: SEO validation WARN...")
    validator = SEOValidator()

    seo = SEOMetadata(
        title="Hi",  # Too short
        seo_title="Hi",
        meta_description="",  # Missing
        focus_keyphrase="missing",
        slug="very-long-slug-that-exceeds-fifty-characters-limit-problem",
        canonical_url=None,
        open_graph_title=None,
        open_graph_description=None,
        twitter_title=None,
        twitter_description=None,
    )

    result = validator.validate(seo, content="No keyphrase here")

    assert result.status == "WARN"
    assert result.seo_score < 80
    assert any(i["code"] == "TITLE_TOO_SHORT" for i in result.issues)
    assert any(i["code"] == "META_DESC_MISSING" for i in result.issues)
    print("  PASS")
    print(f"  Issues: {[i['code'] for i in result.issues]}")


def test_keyphrase_extraction():
    """Test keyphrase extraction from content."""
    print("Test 7: Keyphrase extraction...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Ethereum Staking Rewards Increase",
        seo_title=None,
        dek="ethereum validators see higher staking returns as network activity grows",
        meta_description=None,
        slug=None,
        canonical=None,
    )

    # Keyphrase should be extracted from dek
    # Stop words filtered, should have ethereum/staking/validators/rewards
    assert len(seo.focus_keyphrase) > 0
    assert len(seo.focus_keyphrase.split()) <= 3  # Max 3 words
    print("  PASS")
    print(f"  Extracted keyphrase: {seo.focus_keyphrase}")


def test_build_seo_for_article():
    """Test convenience function."""
    print("Test 8: build_seo_for_article...")

    article = {
        "headline": "Crypto Market Update",
        "seo_title": "Market Update",
        "dek": "Prices move higher today",
        "meta_description": "Daily market update",
        "slug": "market-update",
        "article_body": "Content about crypto market update here.",
    }

    seo, validation = build_seo_for_article(article, "https://example.com")

    assert seo.title == "Crypto Market Update"
    assert seo.seo_title == "Market Update"
    assert validation.status in ("PASS", "WARN")
    print("  PASS")


def test_seo_report_formatting():
    """Test SEO report formatting."""
    print("Test 9: SEO report formatting...")

    result = SEOValidationResult(
        status="WARN",
        seo_score=75,
        issues=[
            {"code": "TITLE_TOO_SHORT", "message": "Title is 10 chars", "severity": "warning"},
            {"code": "META_DESC_MISSING", "message": "Meta description empty", "severity": "warning"},
        ],
        recommendations=["Title is 10 chars", "Meta description empty"],
    )

    report = format_seo_report(result)

    assert "SEO VALIDATION" in report
    assert "Status: WARN" in report
    assert "Score: 75/100" in report
    assert "TITLE_TOO_SHORT" in report
    print("  PASS")


def test_canonical_handling():
    """Test canonical URL generation."""
    print("Test 10: Canonical handling...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Test",
        seo_title=None,
        dek="",
        meta_description=None,
        slug="test",
        canonical="https://canonical.example.com/original",
    )

    assert seo.canonical_url == "https://canonical.example.com/original"
    print("  PASS")


def test_og_twitter_fields():
    """Test OG/Twitter field derivation."""
    print("Test 11: OG/Twitter fields...")
    builder = SEOMetadataBuilder("https://example.com")

    seo = builder.build(
        headline="Original Headline Here",
        seo_title="Different SEO Title",
        dek="Description here",
        meta_description=None,
        slug="test",
        canonical=None,
    )

    # OG title should use SEO title when different
    assert seo.open_graph_title == "Different SEO Title"
    # OG description should match meta
    assert seo.open_graph_description == seo.meta_description
    # Twitter should mirror OG
    assert seo.twitter_title == seo.open_graph_title
    print("  PASS")




def test_meta_description_truncates_at_word_boundary():
    """Never produce mid-token cuts like `$87,00`."""
    from newsagent_v2.wordpress.seo_metadata import truncate_at_boundary, build_seo_for_article

    raw = (
        "Bitcoin briefly climbed above $87,200 on Monday according to CoinGecko "
        "while US spot bitcoin ETFs attracted nearly one billion dollars in net "
        "inflows led by BlackRock IBIT and other major issuers amid broader "
        "market recovery signals across digital asset markets worldwide today."
    )
    # Force a cut that would naive-slice inside $87,200
    cut_at = raw.index("$87,200") + len("$87,")  # mid-number
    naive = raw[:cut_at]
    assert "$87,00" in naive or naive.endswith("$87,")
    safe = truncate_at_boundary(raw, cut_at)
    assert "$87,00" not in safe
    assert not safe.rstrip().endswith("$87,")
    assert "$87,200" in safe or "$87" not in safe  # either keep full token or drop it

    article = {
        "headline": "Bitcoin Climbs as Spot ETFs Attract Nearly $1 Billion",
        "dek": raw,
        "meta_description": raw,
        "slug": "bitcoin-etf-inflows-monday",
        "article_body": raw + " bitcoin etf inflows continued for a third session.",
        "keywords": ["bitcoin etf inflows", "spot bitcoin etfs"],
    }
    seo, validation = build_seo_for_article(article, "https://example.com", repair=True)
    assert len(seo.meta_description) <= 160
    assert "$87,00" not in seo.meta_description
    assert not seo.meta_description.endswith(("Bla", "Black", "BlackR", "BlackRo"))
    # Focus keyphrase stays meaningful
    assert "bitcoin" in seo.focus_keyphrase.lower()
    assert len(seo.seo_title) <= 60
    assert seo.slug
    assert " " not in seo.slug


def test_seo_title_and_slug_helpers():
    from newsagent_v2.wordpress.seo_metadata import SEOMetadataBuilder

    builder = SEOMetadataBuilder("https://example.com")
    long_title = (
        "US Spot Bitcoin ETFs Attract Nearly One Billion Dollars In A Single "
        "Session As Institutional Demand Returns Strongly Across Issuers"
    )
    seo = builder.build(
        headline=long_title,
        seo_title=long_title,
        dek="US spot bitcoin ETFs attracted $998.9 million on Monday.",
        meta_description=(
            "US spot bitcoin ETFs attracted $998.9 million on Monday as bitcoin "
            "briefly climbed above $87,200 according to CoinGecko data and "
            "institutional flows accelerated across major issuers including BlackRock."
        ),
        slug=None,
        keywords=["bitcoin etf inflows"],
        topic="bitcoin",
        entities=["BlackRock"],
    )
    assert len(seo.seo_title) <= builder.TITLE_MAX
    assert len(seo.meta_description) <= builder.META_DESC_MAX
    assert "$87,00" not in seo.meta_description
    assert seo.focus_keyphrase
    assert len(seo.slug) <= builder.SLUG_MAX
    assert seo.slug.replace("-", "").isalnum() or all(c.isalnum() or c == "-" for c in seo.slug)


if __name__ == "__main__":
    test_seo_extraction()
    test_seo_title_preference()
    test_meta_description_preference()
    test_slug_generation()
    test_validation_pass()
    test_validation_warn()
    test_keyphrase_extraction()
    test_build_seo_for_article()
    test_seo_report_formatting()
    test_canonical_handling()
    test_og_twitter_fields()

    print("\n=== ALL SEO TESTS PASSED ===")
