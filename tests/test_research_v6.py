from __future__ import annotations

from newsagent_v2.research import ResearchConfig, deep_research
from newsagent_v2.research.chrome import clean_paragraphs, is_chrome
from newsagent_v2.research.fetch import FetchResult
from newsagent_v2.research.pipeline import build_queries, named_entities
from newsagent_v2.research.primary import is_primary_url
from newsagent_v2.research.search import SearchHit

STORY_PARAS = [
    "The Securities and Exchange Commission approved the first triple-leveraged bitcoin fund on Thursday, "
    "according to a filing reviewed by reporters.",
    "Volatility Shares said the fund would begin trading next week and would target three times the daily "
    "move of bitcoin futures.",
    "Analysts at Bloomberg Intelligence said the approval marks a shift in how the SEC treats leveraged "
    "crypto products after years of rejections.",
    "The fund will charge an annual fee of 1.85 percent, the filing showed, higher than most existing "
    "bitcoin exchange-traded funds.",
]


def _html(paras: list[str], extra: str = "") -> str:
    body = "".join(f"<p>{p}</p>" for p in paras)
    return (
        "<html><head><title>SEC clears 3x bitcoin fund</title></head><body>"
        "<nav>Home Markets Bitcoin Ethereum DeFi Regulation</nav>"
        f"<article><h1>SEC clears 3x bitcoin fund</h1>{body}{extra}</article>"
        "<footer>All rights reserved</footer></body></html>"
    )


def test_chrome_lines_are_dropped():
    assert is_chrome("Add Decrypt as your preferred source on Google")
    assert is_chrome("This news article is produced in accordance with Cointelegraph's Editorial Policy.")
    assert is_chrome("Washington AI White House Donald Trump Regulation")
    assert is_chrome("Almost there. Sign in and your reply posts straight away.")
    assert is_chrome("By Jane Doe and Sam Lee, Reuters")
    assert is_chrome("Got Question about the News?")
    assert is_chrome(
        "Transparency note: This article was produced with the assistance of artificial intelligence and "
        "reviewed by our editorial team before publication."
    )
    assert not is_chrome("By the end of trading, bitcoin had fallen 4% to $82,000, exchange data showed.")
    assert not is_chrome(STORY_PARAS[0])
    assert not is_chrome(
        "The stablecoin bill sponsored by Senator Lummis would let investors subscribe to tokenized funds "
        "through regulated brokers, the draft said."
    )


def test_tail_marker_cuts_related_rail():
    paras, _ = clean_paragraphs(
        [STORY_PARAS[0], "## More on the subject", "Russia's Finance Ministry pays wages in digital rubles today."]
    )
    assert paras == [STORY_PARAS[0]]


def test_named_entities_skip_headline_words():
    texts = [
        "Analysts said Cathie Wood expects gains. Wood said the Dollar Could Surge headline was wrong, "
        "and the dollar could surge anyway.",
        "Separately, Cathie Wood repeated that Bitcoin could reach new highs.",
    ]
    names = named_entities("Cathie Wood predicts Bitcoin could surge", texts)
    assert "Cathie Wood" in names
    assert not any("Could" in n for n in names)


def test_queries_drop_possessive_fragment():
    queries = build_queries("What Could Decide Bitcoin’s Q4?", ["Bitcoin"], 3)
    assert queries[0] == "What Could Decide Bitcoin Q4?"


def test_primary_classification():
    assert is_primary_url("https://www.sec.gov/news/press-release/2026-1")
    assert is_primary_url("https://www.coinbase.com/blog/x", ["Coinbase"])
    assert not is_primary_url("https://news.bitcoin.com/x", ["Bitcoin"])
    assert not is_primary_url("https://www.coindesk.com/x", ["SEC"])


def test_deep_research_offline_end_to_end():
    pages = {
        "https://a.example/story": _html(STORY_PARAS, '<p><a href="https://www.sec.gov/rules/3x">SEC order</a> '
                                                      "was published alongside the decision on Thursday.</p>"),
        "https://b.example/copy": _html(STORY_PARAS),
        "https://c.example/other": _html(
            [
                "The SEC approval of the triple-leveraged bitcoin fund came after Volatility Shares refiled "
                "its application in August, people familiar with the matter said.",
                "Bitcoin futures volumes on the CME rose 12 percent on the news, exchange data showed on Friday.",
                "Critics said leveraged bitcoin funds expose retail investors to losses that compound quickly "
                "in volatile markets, according to consumer advocates.",
            ]
        ),
        "https://off.example/x": _html(
            [
                "The city council voted to approve a new park budget after a lengthy debate on Tuesday evening.",
                "Residents told the council that the playground had been closed for repairs since last spring.",
                "The mayor said construction would begin in March and should finish before the summer holidays.",
                "Two council members voted against the plan, citing rising costs for maintenance and staffing.",
            ]
        ),
        "https://www.sec.gov/rules/3x": _html(
            [
                "The Commission approved the proposed rule change allowing the triple-leveraged bitcoin fund "
                "from Volatility Shares to list, the order said.",
                "The order cited surveillance sharing agreements with CME as the basis for finding the proposal "
                "consistent with the Exchange Act.",
                "The Commission said the fund's daily reset feature was disclosed prominently in the registration "
                "statement filed by the sponsor in August.",
            ]
        ),
    }

    def fake_fetch(url: str) -> FetchResult:
        html = pages.get(url, "")
        return FetchResult(url, url, 200 if html else 404, html, "http")

    def fake_search(queries: list[str]) -> list[SearchHit]:
        return [
            SearchHit("https://b.example/copy", "SEC clears 3x bitcoin fund", "B", "", "bing_news"),
            SearchHit("https://c.example/other", "SEC clears triple leveraged bitcoin fund", "C", "", "google_news"),
            SearchHit("https://www.youtube.com/watch?v=1", "SEC bitcoin fund video", "YT", "", "bing_news"),
        ]

    story = {
        "event_id": "evt-test",
        "representative_title": "SEC clears 3x bitcoin fund from Volatility Shares",
        "entities": ["sec", "bitcoin"],
        "article_input": {
            "evidence": [
                {"url": "https://a.example/story", "source": "A", "title": "SEC clears 3x bitcoin fund"},
                {"url": "https://off.example/x", "source": "Off", "title": "Council park budget"},
            ]
        },
    }
    dossier = deep_research(
        story,
        config=ResearchConfig(use_browser=False),
        search_fn=fake_search,
        http_fetch_fn=fake_fetch,
    )
    kept = {s.host: s for s in dossier.sources}
    rejected = {s.host: s.rejected_reason for s in dossier.rejected}
    assert "a.example" in kept and "c.example" in kept
    assert rejected["b.example"].startswith("syndicated_copy_of")
    assert rejected["off.example"].startswith("off_topic")
    assert "youtube.com" not in kept and "youtube.com" not in rejected
    assert kept["sec.gov"].kind == "primary"
    assert all("Home Markets" not in p for s in dossier.sources for p in s.paragraphs)
