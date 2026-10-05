from __future__ import annotations

import json

import pytest

from newsagent_v2.facts.bank import Fact, FactBank, Quote
from newsagent_v2.write import Article, BudgetExceeded, KimiClient, StoryBudget, structural_issues, write_article
from newsagent_v2.write.writer import keyword_present

QUOTE = "This is an important milestone for the crypto market"


def _bank() -> FactBank:
    facts = [
        Fact(id=f"F{i}", text=f"The SEC approved fund number {i} for Volatility Shares on October 2, filings showed.",
             kind="fact", core=True, publishers=["CoinDesk"], numbers=[str(i)])
        for i in range(1, 41)
    ]
    return FactBank(event_id="evt-w", title="SEC approves 3x bitcoin fund", facts=facts,
                    quotes=[Quote(QUOTE, "Justin Young", "https://a.example", "CoinDesk")])


def _article_json(paragraph_words: int = 60, quote: str = QUOTE) -> dict:
    filler = " ".join(["detail"] * paragraph_words)
    sections = [
        {"heading": "" if i == 0 else f"3x bitcoin fund section {i}",
         "paragraphs": [{"text": f"The 3x bitcoin fund was approved. {filler}.", "facts": ["F1"], "quotes": []}
                        for _ in range(4)]}
        for i in range(4)
    ]
    sections[1]["paragraphs"][0] = {"text": f'Justin Young said "{quote}," in a statement.', "facts": ["F2"], "quotes": ["Q1"]}
    return {
        "headline": "SEC approves 3x bitcoin fund from Volatility Shares",
        "dek": "The SEC approved the fund.",
        "sections": sections,
        "conclusion": [{"text": "The 3x bitcoin fund awaits registration.", "facts": ["F3"]}],
        "faq": [{"question": f"What is 3x bitcoin fund question {i}?", "answer": "It is a leveraged fund.", "facts": ["F4"]}
                for i in range(4)],
        "seo": {"focus_keyword": "3x bitcoin fund", "meta_title": "SEC approves 3x bitcoin fund",
                "meta_description": "The SEC approved a 3x bitcoin fund from Volatility Shares, clearing an exchange "
                                    "listing hurdle; trading still awaits registration effectiveness.",
                "slug": "sec-approves-3x-bitcoin-fund", "tags": ["SEC", "Bitcoin", "ETF"], "category": "Markets"},
    }


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self.payload, self.status_code = payload, status

    def json(self) -> dict:
        return self.payload


def _post_returning(*bodies: dict):
    calls: list[dict] = []

    def post(url, headers, json, timeout):  # noqa: A002
        calls.append(json)
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        return FakeResponse({"choices": [{"message": {"content": __import__("json").dumps(body)}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    return post, calls


def test_good_draft_passes_in_one_call():
    post, calls = _post_returning(_article_json())
    budget = StoryBudget()
    result = write_article(_bank(), KimiClient("k", http_post=post), budget)
    assert result.ok, result.issues
    assert len(calls) == 1 and budget.calls == 1 and budget.total_tokens == 1500
    assert result.article.body_words >= 900


def test_short_draft_gets_one_revision():
    post, calls = _post_returning(_article_json(paragraph_words=20), _article_json())
    result = write_article(_bank(), KimiClient("k", http_post=post), StoryBudget())
    assert result.ok and result.revisions == 1 and len(calls) == 2
    revision_note = calls[1]["messages"][-1]["content"]
    assert "body is" in revision_note


def test_misquote_and_unknown_fact_are_flagged():
    data = _article_json(quote="This is a historic milestone for crypto")
    data["sections"][2]["paragraphs"][0]["facts"] = ["F999"]
    article = Article.from_json(data)
    bank = _bank()
    issues = structural_issues(article, {f.id: f for f in bank.facts}, {"Q1": bank.quotes[0]})
    assert any("not an exact listed quote" in i for i in issues)
    assert any("F999" in i for i in issues)


def test_budget_stops_calls():
    budget = StoryBudget(max_calls=1)
    budget.calls = 1
    post, _ = _post_returning(_article_json())
    with pytest.raises(BudgetExceeded):
        KimiClient("k", http_post=post).chat_json([], budget=budget, stage="draft")


def test_markdown_has_closing_sections_and_no_lede_heading():
    md = Article.from_json(_article_json()).to_markdown()
    assert md.startswith("The 3x bitcoin fund was approved.")
    assert "## Conclusion" in md and "## Frequently Asked Questions" in md


def test_keyword_present_is_exact_phrase():
    assert keyword_present("3x Bitcoin ETF", "the SEC cleared 3x bitcoin ETFs on Friday")
    assert keyword_present("Jay Clayton", "naming Jay Clayton's team")
    assert not keyword_present("Jay Clayton AI czar", "naming Jay Clayton as AI czar")
    assert not keyword_present("AI czar", "aiming for a czar")
