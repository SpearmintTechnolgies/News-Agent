"""Draft, check, revise: the V6 write loop within one story's Kimi budget."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from newsagent_v2.facts.bank import FactBank
from newsagent_v2.qa6.checks import BLOCK, FIX, WARN, Issue, QAReport, release_invented_quotes, run_qa
from newsagent_v2.research.dossier import ResearchDossier
from newsagent_v2.write.article import Article
from newsagent_v2.write.kimi import KimiClient, KimiError, StoryBudget
from newsagent_v2.write.prompt import build_messages, revision_messages, select_facts, usable_quotes

logger = logging.getLogger(__name__)

STATUS_REVIEW = "review"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"


@dataclass
class DraftOutcome:
    status: str
    article: Article | None = None
    report: QAReport | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    revisions: int = 0
    error: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "article": self.article.to_dict() if self.article else None,
            "qa": self.report.to_dict() if self.report else None,
            "revisions": self.revisions,
            "error": self.error,
            "history": self.history,
            "budget": self.budget,
        }


def _paraphrase_invented_quotes(article, bank, dossier, report, history):
    """A non-verbatim quote is prose, not a reason to discard the article or buy another model call."""
    if not any(issue.code == "invented_quote" for issue in report.by(BLOCK)):
        return article, report
    released = release_invented_quotes(article, bank.quotes)
    if not released:
        return article, report
    report = run_qa(article, bank, dossier)
    report.issues.append(Issue(
        "quote_paraphrased", WARN, "quotes",
        f"{released} quotation mark{'s' if released != 1 else ''} removed because the words were not a verbatim source quote",
    ))
    history.append({"stage": "paraphrase_quotes", "released": released, "summary": report.summary()})
    return article, report


def _score(report: QAReport) -> tuple[int, int]:
    """Blocking problems first, then problems to fix; lower is better."""
    return len(report.by(BLOCK)), len(report.by(FIX))


def _parse(raw: dict[str, Any]) -> Article | None:
    try:
        return Article.from_json(raw)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[WRITE-LOOP] unparseable article: %s", exc)
        return None


def write_and_check(
    bank: FactBank,
    dossier: ResearchDossier | None,
    client: KimiClient,
    budget: StoryBudget,
    *,
    today: str | None = None,
    feedback: str = "",
) -> DraftOutcome:
    """Draft once, then spend the remaining budget on QA-driven revisions.

    A revision is kept only if it does not add blocking problems or problems to fix.
    A worse revision is discarded and not retried; the draft already in hand continues.
    The final status is ``blocked`` when a blocking problem remains, otherwise ``review``
    with the remaining fix/warn items shown to the editor. ``feedback`` is the editor's
    note on a previous version (REVISE).
    """
    day = today or datetime.now(timezone.utc).strftime("%A, %B %d, %Y")
    messages = build_messages(
        bank, today=day, facts=select_facts(bank), quotes=usable_quotes(bank), feedback=feedback
    )
    outcome = DraftOutcome(status=STATUS_FAILED)
    try:
        raw = client.chat_json(messages, budget=budget, stage="draft")
    except KimiError as exc:
        outcome.error = str(exc)
        outcome.budget = budget.to_dict()
        return outcome

    article = _parse(raw)
    if article is None:
        outcome.error = "draft was not a valid article"
        outcome.budget = budget.to_dict()
        return outcome
    report = run_qa(article, bank, dossier)
    outcome.history.append({"stage": "draft", "score": _score(report), "summary": report.summary()})
    article, report = _paraphrase_invented_quotes(article, bank, dossier, report, outcome.history)

    while report.revision_requests() and budget.calls < budget.max_calls and budget.total_tokens < budget.max_tokens:
        stage = f"revise_{outcome.revisions + 1}"
        try:
            candidate_raw = client.chat_json(
                revision_messages(messages, raw, report.revision_requests()), budget=budget, stage=stage
            )
        except KimiError as exc:
            outcome.error = str(exc)
            break
        outcome.revisions += 1
        candidate = _parse(candidate_raw)
        if candidate is None:
            outcome.history.append({"stage": stage, "kept": False, "summary": "unparseable"})
            continue
        candidate_report = run_qa(candidate, bank, dossier)
        kept = _score(candidate_report) <= _score(report)
        outcome.history.append(
            {"stage": stage, "kept": kept, "score": _score(candidate_report), "summary": candidate_report.summary()}
        )
        logger.info("[WRITE-LOOP] event=%s %s %s kept=%s", bank.event_id, stage, candidate_report.summary(), kept)
        if not kept:
            break
        raw, article, report = candidate_raw, candidate, candidate_report
        article, report = _paraphrase_invented_quotes(article, bank, dossier, report, outcome.history)

    outcome.raw = raw
    outcome.article = article
    outcome.report = report
    outcome.status = STATUS_BLOCKED if report.blocked else STATUS_REVIEW
    outcome.budget = budget.to_dict()
    return outcome
