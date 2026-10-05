from __future__ import annotations

from types import SimpleNamespace

import newsagent_v2.story6 as story6
from newsagent_v2.facts.gate import GateResult
from newsagent_v2.research.dossier import ResearchDossier, SourceDoc
from newsagent_v2.story6 import OUTCOME_BLOCKED, OUTCOME_READY, OUTCOME_SKIPPED, run_story
from newsagent_v2.write import KimiClient
from test_qa_v6 import _bank, _clean_article, _post_returning, _with_paragraph


def _event() -> SimpleNamespace:
    report = SimpleNamespace(source="CoinDesk", url="https://a.example/x", headline="Regulators clear fund",
                             published_at="2026-10-04", description="")
    return SimpleNamespace(event_id="evt-q", canonical_title="Regulators clear leveraged bitcoin fund",
                           entities={"Bitcoin"}, reports=[report])


def _dossier() -> ResearchDossier:
    source = SourceDoc(url="https://finance.biggo.com/a", publisher="finance.biggo.com", title="Fund cleared",
                       paragraphs=["Regulators cleared the leveraged bitcoin fund listing rule. " * 30])
    return ResearchDossier(event_id="evt-q", title="Regulators clear leveraged bitcoin fund",
                           entities=["Volatility Shares"], sources=[source])


def _passing_gate(monkeypatch) -> None:
    monkeypatch.setattr(story6, "build_fact_bank", lambda dossier: _bank())
    monkeypatch.setattr(story6, "qualify", lambda dossier, bank, urls: GateResult(passed=True))


def test_thin_story_is_skipped_with_reason_and_no_writer_call():
    calls = []
    result = run_story(_event(), {}, client=KimiClient("k", http_post=lambda *a, **k: calls.append(1)),
                       research_fn=lambda story: ResearchDossier(event_id="evt-q", title="t"))
    assert result.outcome == OUTCOME_SKIPPED and result.reason.startswith("Skipped:")
    assert not calls


def test_ready_story_produces_publishable_record(monkeypatch):
    _passing_gate(monkeypatch)
    post, calls = _post_returning(_clean_article())
    result = run_story(_event(), {}, client=KimiClient("k", http_post=post), research_fn=lambda story: _dossier())
    assert result.outcome == OUTCOME_READY and len(calls) == 1
    record = result.article
    assert record["preserve_structure"] and record["pipeline"] == "v6"
    assert record["article_body"].count("## ") >= 5 and "## Frequently Asked Questions" in record["article_body"]
    assert record["focus_keyphrase"] == "leveraged bitcoin fund"
    assert record["categories"] == ["Policy & Regulations"]
    assert record["sources"] == [{"url": "https://finance.biggo.com/a", "source": "Biggo", "title": "Fund cleared"}]
    assert record["entities"] == [{"name": "Volatility Shares"}]


def test_invention_that_survives_revisions_is_blocked(monkeypatch):
    _passing_gate(monkeypatch)
    invented = _with_paragraph(_clean_article(), "Analyst Walter Pennington said inflows reached $45 million.")
    post, calls = _post_returning(invented)
    result = run_story(_event(), {}, client=KimiClient("k", http_post=post), research_fn=lambda story: _dossier())
    assert result.outcome == OUTCOME_BLOCKED and len(calls) == 3
    assert "Walter Pennington" in result.reason or "45" in result.reason
