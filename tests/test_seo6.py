"""V6 SEO: sitemap index, internal linking, Rank Math-style score, keyword fixes."""

from __future__ import annotations

from types import SimpleNamespace

from newsagent_v2.seo6 import (
    SiteIndex,
    SitePost,
    best_focus_keyword,
    fetch_site_index,
    finalize_seo,
    fit_meta,
    load_site_index,
    phrase_in,
    plan_and_apply,
    prepare_for_site,
    score_article,
    slug_with_keyword,
)
from newsagent_v2.wordpress.html_formatter import ArticleHtmlFormatter

BASE = "https://example.com"


class _Resp:
    def __init__(self, text="", data=None, status_code=200):
        self.text, self._data, self.status_code = text, data, status_code

    def json(self):
        return self._data


def _fake_get(posts):
    def get(url, params=None):
        if url.endswith("/sitemap_index.xml"):
            return _Resp(f"<sitemapindex><sitemap><loc>{BASE}/post-sitemap.xml</loc></sitemap>"
                         f"<sitemap><loc>{BASE}/page-sitemap.xml</loc></sitemap></sitemapindex>")
        if url.endswith("/post-sitemap.xml"):
            rows = "".join(f"<url><loc>{BASE}/{slug}/</loc><lastmod>2026-09-01T00:00:00+00:00</lastmod></url>"
                           for slug, _ in posts)
            return _Resp(f"<urlset>{rows}</urlset>")
        if url.endswith("/wp-json/wp/v2/posts"):
            return _Resp(data=[{"link": f"{BASE}/{slug}/", "title": {"rendered": title}} for slug, title in posts])
        raise AssertionError(f"unexpected url {url}")
    return get


SITE = [
    ("bitcoin-etfs-add-189m", "Bitcoin ETFs Add $189M as August Inflows Near $1 Billion"),
    ("bitcoin-etf-inflow-streak", "Bitcoin ETF Inflow Streak Reaches Five Days"),
    ("sanctions-crypto-funds-russia", "Sanctions Lists That Are Raising Crypto Funds for Russia"),
    ("ethereum-staking-guide", "A Guide to Ethereum Staking Rewards"),
    ("xrp-ruling", "Ripple XRP Court Ruling Explained"),
    *[(f"market-wrap-{i}", f"Crypto Market Wrap Day {i}: Solana, Dogecoin and Cardano Move") for i in range(25)],
]


def _index():
    return fetch_site_index(BASE, http_get=_fake_get(SITE))


def _article(**overrides):
    body = "\n\n".join([
        "The SEC cleared a 3x Bitcoin ETF on Friday, allowing Volatility Shares to list leveraged funds.",
        "## Why the 3x Bitcoin ETF matters",
        "Leveraged products have grown as spot Bitcoin ETF inflows rose through the summer, analysts said.",
        "The 3x Bitcoin ETF resets daily, so returns over longer periods can differ from three times the index.",
        "## What comes next for the 3x Bitcoin ETF",
        "Trading of the 3x Bitcoin ETF awaits the registration becoming effective.",
        "## Conclusion",
        "The 3x Bitcoin ETF is cleared but not yet trading.",
    ])
    record = {
        "headline": "SEC Clears 3x Bitcoin ETF From Volatility Shares",
        "seo_title": "3x Bitcoin ETF Cleared by SEC for Volatility Shares",
        "meta_description": "The SEC cleared a 3x Bitcoin ETF from Volatility Shares; trading of the leveraged "
                            "fund awaits the registration becoming effective later.",
        "focus_keyphrase": "3x Bitcoin ETF",
        "slug": "sec-clears-3x-bitcoin-etf",
        "tags": ["Bitcoin ETF", "SEC", "Volatility Shares"],
        "entities": [{"name": "Volatility Shares"}],
        "article_body": body,
        "sources": [{"url": "https://news.example/a"}, {"url": "https://news.example/b"}],
    }
    record.update(overrides)
    return record


def test_fetch_site_index_reads_post_sitemap_and_titles():
    index = _index()
    assert len(index.posts) == len(SITE)
    post = next(p for p in index.posts if p.slug == "bitcoin-etfs-add-189m")
    assert post.title.startswith("Bitcoin ETFs Add") and post.lastmod.startswith("2026-09-01")


def test_load_site_index_falls_back_to_stale_cache(tmp_path):
    cache = tmp_path / "index.json"
    first = load_site_index(BASE, cache_path=cache, http_get=_fake_get(SITE))

    def down(url, params=None):
        raise OSError("site down")

    again = load_site_index(BASE, cache_path=cache, ttl_seconds=0, http_get=down)
    assert [p.url for p in again.posts] == [p.url for p in first.posts]


def test_links_only_relevant_posts_with_unique_anchors():
    body, plan = plan_and_apply(_article(), _index())
    urls = {link["url"] for link in plan.inline} | {r["url"] for r in plan.related}
    assert urls and all("bitcoin-etf" in u for u in urls)
    assert not any("russia" in u or "xrp" in u or "staking" in u for u in urls)
    anchors = [link["anchor"].lower().rstrip("s") for link in plan.inline]
    assert len(anchors) == len(set(anchors))
    assert "3x bitcoin etf" not in anchors
    lede, conclusion = body.split("\n\n")[0], body.split("## Conclusion")[1]
    assert "](" not in lede and "](" not in conclusion
    for link in plan.inline:
        assert f"[{link['anchor']}]({link['url']})" in body


def test_possible_duplicate_flagged():
    index = SiteIndex(base_url=BASE, posts=[
        SitePost(url=f"{BASE}/sec-clears-3x-etf/", slug="sec-clears-3x-etf",
                 title="SEC Clears 3x Bitcoin ETF From Volatility Shares", lastmod="2026-10-01"),
        *_index().posts,
    ])
    _, plan = plan_and_apply(_article(slug="new-slug"), index)
    assert plan.possible_duplicate and plan.possible_duplicate["url"].endswith("/sec-clears-3x-etf/")


def test_formatter_renders_inline_links_and_read_also():
    body, plan = plan_and_apply(_article(), _index())
    formatter = ArticleHtmlFormatter(SimpleNamespace(base_url=BASE), transport=None)
    html = formatter.format_article(body, evidence=None, preserve_structure=True,
                                          read_also_links=[{"url": f"{BASE}/x/", "title": "Related & More"}]).html_content
    for link in plan.inline:
        assert f'<a href="{link["url"]}">{link["anchor"]}</a>' in html
    assert "Read Also" in html and "Related &amp; More" in html and "](" not in html


def test_score_rewards_keyword_placement_and_links():
    article = _article()
    low = score_article(article, base_url=BASE, internal_links=0, external_links=0, has_featured_image=False)
    high = score_article(article, base_url=BASE, internal_links=3, external_links=2, has_featured_image=True)
    assert high.score > low.score
    passed = {c.id for c in high.checks if c.passed}
    assert {"kw_title", "kw_meta", "kw_url", "kw_intro", "kw_subheading"} <= passed
    missing = score_article(_article(focus_keyphrase="Volatility Shares lawsuit"), base_url=BASE,
                            internal_links=3, external_links=2, has_featured_image=True)
    assert missing.score < high.score
    assert "kw_title" in {c.id for c in missing.misses()}


def test_keyword_uniqueness_against_site_titles():
    index = _index()
    taken = score_article(_article(focus_keyphrase="Bitcoin ETF"), base_url=BASE, internal_links=3,
                          external_links=2, has_featured_image=True, index=index)
    unique = score_article(_article(), base_url=BASE, internal_links=3, external_links=2,
                           has_featured_image=True, index=index)
    assert "kw_unique" in {c.id for c in taken.misses()}
    assert "kw_unique" not in {c.id for c in unique.misses()}


def test_phrase_matching_is_exact():
    assert phrase_in("3x Bitcoin ETF", "two 3x bitcoin ETFs")
    assert not phrase_in("3x Bitcoin ETF", "a bitcoin 3x ETF")


def test_writer_keyword_kept_when_placed_else_best_phrase():
    assert best_focus_keyword(_article()) == "3x Bitcoin ETF"
    chosen = best_focus_keyword(_article(focus_keyphrase="leveraged crypto products"))
    assert chosen != "leveraged crypto products" and phrase_in(chosen, _article()["headline"])


def test_slug_and_meta_fixes():
    assert slug_with_keyword("sec-clears-fund", "3x Bitcoin ETF") == "3x-bitcoin-etf-sec-clears-fund"
    assert slug_with_keyword("sec-clears-3x-bitcoin-etf", "3x Bitcoin ETF") == "sec-clears-3x-bitcoin-etf"
    long_meta = ("The SEC cleared a 3x Bitcoin ETF from Volatility Shares on Friday. Trading of the leveraged fund "
                 "awaits the registration statement becoming effective, the filing shows today.")
    trimmed = fit_meta(long_meta, "3x Bitcoin ETF")
    assert len(trimmed) <= 160 and phrase_in("3x Bitcoin ETF", trimmed)
    clause = ("The SEC approved a Cboe BZX rule change on October 2 clearing six Volatility Shares 3x leveraged "
              "funds, including the first 3x Bitcoin ETF and 3x Ether ETF in the US.")
    assert fit_meta(clause, "3x Bitcoin ETF").endswith("3x Ether ETF.")
    record = finalize_seo(_article(meta_description=long_meta, slug="sec-clears-fund"))
    assert record["slug"].startswith("3x-bitcoin-etf") and len(record["meta_description"]) <= 160
    assert record["keywords"][0] == "3x Bitcoin ETF"


def test_prepare_for_site_attaches_links_and_report():
    out = prepare_for_site(_article(), BASE, has_featured_image=True, index=_index())
    assert out["seo"]["internal_links"] >= 1 and out["seo"]["focus_keyword"] == "3x Bitcoin ETF"
    assert 0 < out["seo"]["score"] <= 100
    assert "](" in out["article_body"]


def test_prepare_for_site_without_index_still_scores():
    out = prepare_for_site(_article(), BASE, has_featured_image=False)
    assert out["seo"]["internal_links"] == 0 and out["related_links"] == []
    assert "](" not in out["article_body"]
