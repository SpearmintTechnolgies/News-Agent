"""Stories that cannot support an article are filtered before a card is sent."""

from types import SimpleNamespace

import newsagent_v2.story6 as story6
from newsagent_v2.research.dossier import ResearchDossier
from newsagent_v2.story6 import OUTCOME_SKIPPED, run_story, screen_story
from newsagent_v2.write import KimiClient
from start_v5_bot import select_postable
from test_story6 import _dossier, _event


def _named(event_id: str):
    event = _event()
    event.event_id = event_id
    return event


def test_select_postable_keeps_only_stories_that_pass_and_stops_at_the_limit():
    events = [_named(f"evt-{n}") for n in range(6)]

    def screen(event):
        return event.event_id in {"evt-1", "evt-3", "evt-5"}, "thin"

    seen = []
    postable, dropped, checked = select_postable(
        events, screen, limit=10, keep=2, progress=lambda i, total, event: seen.append(event.event_id),
    )
    assert [e.event_id for e in postable] == ["evt-1", "evt-3"]
    assert dropped == 2
    assert checked == 4
    assert seen == ["evt-0", "evt-1", "evt-2", "evt-3"]


def test_failed_screen_is_reused_when_the_same_story_is_run(monkeypatch):
    story6._SCREEN_CACHE.clear()
    calls = []

    def research(story):
        calls.append(story["event_id"])
        return ResearchDossier(event_id=story["event_id"], title=story["representative_title"])

    monkeypatch.setattr(story6, "deep_research", research)
    passed, reason = screen_story(_event())
    result = run_story(_event(), {}, client=KimiClient("k", http_post=lambda *a, **k: None))
    assert passed is False
    assert reason.startswith("Skipped:")
    assert result.outcome == OUTCOME_SKIPPED
    assert result.reason == reason
    assert calls == ["evt-q"]


def test_passing_screen_is_reused_by_the_writer(monkeypatch):
    story6._SCREEN_CACHE.clear()
    calls = []
    monkeypatch.setattr(story6, "deep_research", lambda story: calls.append(1) or _dossier())
    monkeypatch.setattr(story6, "build_fact_bank", lambda dossier: __import__("test_qa_v6", fromlist=["_bank"])._bank())
    from newsagent_v2.facts.gate import GateResult
    monkeypatch.setattr(story6, "qualify", lambda dossier, bank, urls: GateResult(passed=True))
    monkeypatch.setattr(story6, "write_and_check", lambda *a, **k: SimpleNamespace(
        status="review", article=None, report=None, budget={}, history=[], revisions=0, error="",
    ))
    assert screen_story(_event()) == (True, "Evidence sufficient for a long-form article.")
    run_story(_event(), {}, client=KimiClient("k", http_post=lambda *a, **k: None))
    assert calls == [1]
