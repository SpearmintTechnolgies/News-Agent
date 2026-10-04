from __future__ import annotations

import copy
import json
import random

from newsagent_v2.facts.bank import Fact, FactBank, Quote
from newsagent_v2.qa6 import BLOCK, FIX, STATUS_BLOCKED, STATUS_REVIEW, run_qa, write_and_check
from newsagent_v2.research.dossier import display_publisher
from newsagent_v2.write import Article, KimiClient, StoryBudget

QUOTE = "This is an important milestone for the crypto market"
COPY_FACT = (
    "the products will anchor their exposure to futures prices on the regulated exchange rather than holding "
    "the underlying coins directly"
)
VOCAB = (
    "regulators cleared leveraged bitcoin fund listing exchange rule filing approval trading shares investors "
    "daily returns futures contracts sponsor trust registration statement order review notice comment period "
    "market products exposure volatility losses holding period disclosure brokers suitability oversight custody "
    "price index benchmark launch timeline effective securities commission staff decision application series"
).split()


def _bank() -> FactBank:
    facts = [
        Fact(id=f"F{i}", text=f"Regulators cleared the leveraged bitcoin fund listing rule in filing round {i}.",
             kind="fact", core=True, publishers=["CoinDesk"], numbers=[str(i)])
        for i in range(1, 31)
    ]
    facts.append(Fact(id="F31", text=f"Analysts said {COPY_FACT}.", kind="fact", core=True, publishers=["Reuters"]))
    return FactBank(event_id="evt-q", title="Regulators clear leveraged bitcoin fund", facts=facts,
                    quotes=[Quote(QUOTE, "Justin Young", "https://a.example", "CoinDesk")])


def _prose(seed: int, words: int = 50) -> str:
    rng = random.Random(seed)
    sentences = []
    while sum(len(s.split()) for s in sentences) < words:
        chunk = rng.sample(VOCAB, 12)
        sentences.append(" ".join(chunk).capitalize() + ".")
    return " ".join(sentences)


def _clean_article() -> dict:
    sections = []
    for s in range(4):
        paragraphs = [{"text": _prose(s * 10 + p), "facts": [f"F{s * 5 + p + 1}"], "quotes": []} for p in range(5)]
        sections.append({"heading": "" if s == 0 else f"Leveraged bitcoin fund detail {s}", "paragraphs": paragraphs})
    sections[0]["paragraphs"][0]["text"] = "Regulators cleared a leveraged bitcoin fund listing. " + _prose(100, 40)
    sections[1]["paragraphs"][1] = {"text": f'Justin Young said "{QUOTE}," in a statement.', "facts": ["F6"], "quotes": ["Q1"]}
    return {
        "headline": "Regulators clear leveraged bitcoin fund listing",
        "dek": "Regulators cleared the listing rule.",
        "sections": sections,
        "conclusion": [{"text": _prose(200, 90), "facts": ["F21"]}],
        "faq": [{"question": f"What does the leveraged bitcoin fund order cover, part {chr(97 + i)}?",
                 "answer": _prose(300 + i, 45), "facts": [f"F{22 + i}"]} for i in range(4)],
        "seo": {"focus_keyword": "leveraged bitcoin fund", "meta_title": "Regulators clear leveraged bitcoin fund",
                "meta_description": "Regulators cleared a leveraged bitcoin fund listing rule, an exchange step "
                                    "that leaves trading to wait on registration and the sponsor's next filings.",
                "slug": "regulators-clear-leveraged-bitcoin-fund", "tags": ["Bitcoin", "ETF", "Regulation"],
                "category": "Regulation"},
    }


def _with_paragraph(data: dict, text: str, facts: list[str] | None = None) -> dict:
    data = copy.deepcopy(data)
    data["sections"][2]["paragraphs"][0] = {"text": text, "facts": facts or ["F11"], "quotes": []}
    return data


def _codes(data: dict) -> dict[str, str]:
    report = run_qa(Article.from_json(data), _bank(), None)
    return {i.code: i.severity for i in report.issues}


def test_clean_article_has_nothing_to_block_or_fix():
    report = run_qa(Article.from_json(_clean_article()), _bank(), None)
    assert not report.by(BLOCK) and not report.by(FIX), report.summary() + str([i.to_dict() for i in report.issues])
    assert report.metrics["body_words"] >= 900


def test_invented_name_and_figure_block():
    codes = _codes(_with_paragraph(_clean_article(), "Analyst Walter Pennington said inflows reached $45 million."))
    assert codes.get("invented_name") == BLOCK
    assert codes.get("invented_figure") == BLOCK


def test_invented_quote_blocks():
    codes = _codes(_with_paragraph(_clean_article(), 'Justin Young said "this changes everything for traders" today.'))
    assert codes.get("invented_quote") == BLOCK


def test_copied_fact_sentence_needs_fix():
    codes = _codes(_with_paragraph(_clean_article(), f"Analysts noted {COPY_FACT}.", ["F31"]))
    assert codes.get("copied_phrasing") == FIX


def test_web_address_and_hype_need_fix_but_brand_domains_are_fine():
    codes = _codes(_with_paragraph(_clean_article(), "The listing is a game changer, according to finance.biggo.com."))
    assert codes.get("web_address") == FIX and codes.get("hype_language") == FIX
    assert "web_address" not in _codes(_with_paragraph(_clean_article(), "Bitcoin.com News reported the listing rule."))


def test_outlet_names_are_not_invented_names():
    codes = _codes(_with_paragraph(_clean_article(), "Reuters and CoinDesk reported the listing rule on the exchange."))
    assert "invented_name" not in codes


def test_headline_title_case_is_not_junk():
    data = _clean_article()
    data["headline"] = "Regulators Clear Leveraged Bitcoin Fund Listing For Trading"
    assert "site_junk" not in _codes(data)


def test_display_publisher():
    assert display_publisher("finance.biggo.com") == "Biggo"
    assert display_publisher("CryptoTicker.io") == "CryptoTicker"
    assert display_publisher("bbc.co.uk") == "Bbc"
    assert display_publisher("The Block") == "The Block"


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload, self.status_code = payload, 200

    def json(self) -> dict:
        return self.payload


def _post_returning(*bodies: dict):
    calls: list[dict] = []

    def post(url, headers, json, timeout):  # noqa: A002
        calls.append(json)
        body = bodies[min(len(calls) - 1, len(bodies) - 1)]
        return FakeResponse({"choices": [{"message": {"content": globals()["json"].dumps(body)}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})

    return post, calls


def test_loop_revises_copied_draft_then_goes_to_review():
    copied = _with_paragraph(_clean_article(), f"Analysts noted {COPY_FACT}.", ["F31"])
    post, calls = _post_returning(copied, _clean_article())
    outcome = write_and_check(_bank(), None, KimiClient("k", http_post=post), StoryBudget())
    assert outcome.status == STATUS_REVIEW and outcome.revisions == 1 and len(calls) == 2
    assert "copied from a source" in calls[1]["messages"][-1]["content"]


def test_loop_keeps_better_version_when_revision_is_worse():
    copied = _with_paragraph(_clean_article(), f"Analysts noted {COPY_FACT}.", ["F31"])
    worse = _with_paragraph(_clean_article(), "Analyst Walter Pennington said inflows reached $45 million.")
    post, calls = _post_returning(copied, worse)
    outcome = write_and_check(_bank(), None, KimiClient("k", http_post=post), StoryBudget())
    assert len(calls) == 3 and outcome.revisions == 2
    assert [h.get("kept") for h in outcome.history[1:]] == [False, False]
    assert outcome.status == STATUS_REVIEW
    assert any(i.code == "copied_phrasing" for i in outcome.report.issues)


def test_loop_blocks_when_invention_survives_budget():
    invented = _with_paragraph(_clean_article(), "Analyst Walter Pennington said inflows reached $45 million.")
    post, calls = _post_returning(invented)
    outcome = write_and_check(_bank(), None, KimiClient("k", http_post=post), StoryBudget())
    assert len(calls) == 3 and outcome.status == STATUS_BLOCKED
    assert json.loads(json.dumps(outcome.to_dict()))["status"] == STATUS_BLOCKED
