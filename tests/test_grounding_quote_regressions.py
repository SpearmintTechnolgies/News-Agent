"""Focused offline regressions for assertion and quote grounding."""

from copy import deepcopy

from newsagent_v2.article.qa.runner import run_article_qa
from newsagent_v2.article.writer.controlled.paragraph import validate_paragraph
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from tests.fixtures.article_qa import clean_article, article_input
from tests.test_controlled_writer_v3 import _rich_story


def _codes(result: dict) -> set[str]:
    return {
        str(item.get("code") or "")
        for item in (result.get("critical_failures") or [])
        if isinstance(item, dict)
    }


def test_unsupported_assertion_is_quarantined_before_qa() -> None:
    story = _rich_story()
    ledgers = build_evidence_ledgers(story["article_input"])
    plan = plan_article(ledgers, story["article_input"])
    rendered = type("Rendered", (), {
        "text": "The bill will transform every American crypto market after years of neglect."
    })()
    result = validate_paragraph(rendered, plan.paragraph_plans[0], ledgers)
    assert not result.ok
    assert "The bill will transform" not in result.text


def test_unattributed_direct_quote_cannot_pass_qa() -> None:
    article = clean_article()
    article["article_body"] = 'A spokesperson said, "An unsupported statement."'
    article["quotes"] = [{
        "text": "An unsupported statement.",
        "kind": "direct",
        "evidence_refs": [{"url": "https://example.com/news/northwind-records-exposed", "source": "TestWire"}],
    }]
    result = run_article_qa(article, article_input())
    assert not result["qa_passed"]
    assert "direct_quote_no_attribution" in _codes(result)


def test_unsupported_paraphrase_cannot_pass_qa() -> None:
    article = clean_article()
    article["quotes"] = [{
        "text": "The regulator secretly approved every account.",
        "kind": "paraphrase",
        "attribution": "TestWire summary",
        "evidence_refs": [{"url": "https://example.com/news/northwind-records-exposed", "source": "TestWire"}],
    }]
    result = run_article_qa(article, article_input())
    assert not result["qa_passed"]
    assert "quote_unsupported_evidence" in _codes(result)


def test_evidence_backed_assertion_and_paraphrase_pass() -> None:
    article = clean_article()
    result = run_article_qa(article, article_input())
    assert result["qa_passed"]
    assert "quote_unsupported_evidence" not in _codes(result)