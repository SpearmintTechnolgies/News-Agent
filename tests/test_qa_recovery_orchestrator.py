from __future__ import annotations

from copy import deepcopy

from newsagent_v2.article.writer.v4.recovery import (
    USER_FAILURE_MESSAGE,
    recover_article,
)


def _qa(code: str | None, passed: bool = False, *, sentence: str = "") -> dict:
    return {
        "qa_passed": passed,
        "publishable": passed,
        "critical_failures": []
        if passed
        else [{"code": code, "message": f"bad {code}", "sentence": sentence}],
        "metrics": {"uncovered_assertive_sentences": [sentence] if sentence else []},
    }


def _runner(article, article_input):
    if article.get("fixed") or "!!" not in article.get("article_body", ""):
        return _qa(None, True)
    return _qa(article.get("failure"), False, sentence="unsupported sentence")


def test_mechanics_failure_repairs_locally_and_passes():
    article = {"failure": "malformed_punctuation", "article_body": "One!!  fact."}
    outcome = recover_article(article, {"evidence": []}, _qa("malformed_punctuation"), qa_fn=_runner)
    assert outcome.succeeded
    assert outcome.cycles == 1
    assert outcome.article["article_body"] == "One! fact."


def test_cleanliness_failure_sanitizes_and_passes():
    article = {"failure": "EDITORIAL_CLEANLINESS_FAILED", "headline": "Headline", "article_body": "Source: wire\n\nValid copy."}
    def runner(current, data):
        return _qa(None, "Source:" not in current["article_body"])
    outcome = recover_article(article, {"evidence": []}, _qa("EDITORIAL_CLEANLINESS_FAILED"), qa_fn=runner)
    assert outcome.succeeded
    assert "Source:" not in outcome.article["article_body"]


def test_grounding_researches_only_gaps_and_repairs_section():
    article = {"failure": "body_assertion_not_in_claims", "article_body": "unsupported sentence. Valid sentence."}
    data = {"evidence": [{"url": "https://old.example", "source": "old"}]}
    calls = []
    def research(current, gaps):
        calls.append(gaps)
        return {"pack": {"evidence": [{"url": "https://new.example", "source": "new"}]}}
    def runner(current, current_data):
        return _qa(None, "unsupported sentence." not in current["article_body"])
    outcome = recover_article(article, data, _qa("body_assertion_not_in_claims", sentence="unsupported sentence."), qa_fn=runner, research_fn=research)
    assert outcome.succeeded
    assert calls == [["unsupported sentence."]]
    assert data["evidence"][-1]["url"] == "https://new.example"
    assert outcome.article["article_body"] == "Valid sentence."


def test_quote_failure_verifies_and_preserves_quote_when_valid():
    article = {"failure": "quote_missing_evidence", "article_body": 'A source said "quoted words".'}
    def runner(current, data):
        return _qa(None, True)
    outcome = recover_article(article, {}, _qa("quote_missing_evidence"), qa_fn=runner, verify_quotes=lambda *_: True)
    assert outcome.succeeded
    assert "quoted words" in outcome.article["article_body"]


def test_successful_work_is_preserved():
    article = {"failure": "body_assertion_not_in_claims", "article_body": "unsupported sentence. valid sentence.", "image": "image-v1", "seo_title": "Keep me"}
    original = deepcopy(article)
    def runner(current, _):
        return _qa(None, "unsupported sentence." not in current["article_body"])
    outcome = recover_article(article, {"evidence": []}, _qa("body_assertion_not_in_claims", sentence="unsupported sentence."), qa_fn=runner)
    assert outcome.succeeded
    assert outcome.article["image"] == original["image"]
    assert outcome.article["seo_title"] == original["seo_title"]
    assert outcome.article["article_body"] == "valid sentence."


def test_recovery_stops_after_two_cycles():
    outcome = recover_article({"article_body": "bad."}, {}, _qa("malformed_punctuation"), qa_fn=lambda *_: _qa("malformed_punctuation"))
    assert not outcome.succeeded
    assert outcome.cycles == 2
    assert len(outcome.history) == 2


def test_kimi_guard_usage_is_recorded_without_extra_calls():
    calls = {"research": 0, "usage": 0}
    def research(*_):
        calls["research"] += 1
        return {"pack": {"evidence": []}}
    def usage():
        calls["usage"] += 1
        return {"kimi_calls": 1, "total_tokens": 20}
    recover_article({"article_body": "bad."}, {}, _qa("body_assertion_not_in_claims"), qa_fn=lambda *_: _qa("body_assertion_not_in_claims"), research_fn=research, usage_fn=usage)
    assert calls == {"research": 2, "usage": 2}


def test_internal_codes_are_not_user_failure_message():
    assert "GROUNDING_FAILED" not in USER_FAILURE_MESSAGE
    assert "malformed_punctuation" not in USER_FAILURE_MESSAGE


def test_exhausted_recovery_has_clean_terminal_message_and_internal_history():
    outcome = recover_article({"article_body": "bad."}, {}, _qa("body_assertion_not_in_claims"), qa_fn=lambda *_: _qa("body_assertion_not_in_claims"))
    assert not outcome.succeeded
    assert outcome.history[-1]["failure_type"] == "GROUNDING_FAILED"
    assert USER_FAILURE_MESSAGE == "Article could not be completed reliably after automatic verification. Please retry."


def test_successful_recovery_is_publishable_review_input():
    outcome = recover_article({"article_body": "bad."}, {}, _qa("malformed_punctuation"), qa_fn=lambda *_: _qa(None, True))
    assert outcome.succeeded
    assert outcome.qa["publishable"]
    assert outcome.qa["qa_passed"]
