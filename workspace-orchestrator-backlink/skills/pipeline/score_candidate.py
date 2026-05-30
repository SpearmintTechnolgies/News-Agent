#!/usr/bin/env python3
"""score_candidate.py — qualify opportunities with blacklist check and scoring."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))

import backlink_db  # noqa: E402

QUALIFY_THRESHOLD = 50.0

RELEVANCE_TERMS = (
    "crypto",
    "cryptograph",
    "blockchain",
    "bitcoin",
    "defi",
    "encryption",
    "security",
    "web3",
    "token",
    "wallet",
    "cipher",
)

SPAM_TERMS = (
    "buy backlinks",
    "cheap seo",
    "guest post service",
    "link farm",
    "paid links",
)

URL_PATH_HINTS = (
    "write-for-us",
    "guest-post",
    "guest_post",
    "contribute",
    "submit-a-guest",
    "become-a-contributor",
)

GUEST_POST_PHRASES = (
    "write for us",
    "guest post",
    "submit a post",
    "become a contributor",
)


@dataclass
class ScoreResult:
    qualified: bool
    relevance_score: float
    spam_risk_score: float
    final_score: float
    reason: str | None = None
    signals: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_signals(
    opportunity: backlink_db.OpportunityRow,
    *,
    db_path: str,
) -> dict[str, Any]:
    if opportunity.context_json:
        try:
            context = json.loads(opportunity.context_json)
            signals = context.get("discovery_signals")
            if isinstance(signals, dict):
                return signals
        except json.JSONDecodeError:
            pass

    parsed = backlink_db.get_parsed_page(opportunity.url, db_path=db_path)
    if parsed and isinstance(parsed.get("signals"), dict):
        return parsed["signals"]
    return {}


def _haystack(opportunity: backlink_db.OpportunityRow) -> str:
    parts = [
        opportunity.title or "",
        opportunity.snippet or "",
        opportunity.url or "",
        opportunity.domain or "",
    ]
    return " ".join(parts).lower()


def score_candidate(
    opportunity: backlink_db.OpportunityRow,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    qualify_threshold: float = QUALIFY_THRESHOLD,
) -> ScoreResult:
    """Score an opportunity. Returns qualified=False for blacklist or low score."""
    if backlink_db.is_blacklisted(opportunity.domain, opportunity.url, db_path=db_path):
        return ScoreResult(
            qualified=False,
            relevance_score=0.0,
            spam_risk_score=100.0,
            final_score=0.0,
            reason="blacklisted",
        )

    signals = _load_signals(opportunity, db_path=db_path)
    haystack = _haystack(opportunity)

    relevance = 20.0
    for term in RELEVANCE_TERMS:
        if term in haystack:
            relevance += 8.0
    for phrase in GUEST_POST_PHRASES:
        if phrase in haystack:
            relevance += 20.0
            break
    for hint in URL_PATH_HINTS:
        if hint in haystack.replace("_", "-"):
            relevance += 15.0
            break
    if signals.get("guest_post_language"):
        relevance += 15.0
    if signals.get("has_form"):
        relevance += 10.0
    if signals.get("has_textarea_form"):
        relevance += 5.0
    relevance = min(relevance, 100.0)

    spam = 0.0
    for term in SPAM_TERMS:
        if term in haystack:
            spam += 35.0

    try:
        from update_learning_weights import load_weights  # noqa: E402

        weights = load_weights()
        domain_key = (opportunity.domain or "").lower()
        if domain_key:
            relevance += float(weights.get("domain_boost", {}).get(domain_key, 0))
            spam += float(weights.get("domain_penalty", {}).get(domain_key, 0))
    except ImportError:
        pass

    spam = min(spam, 100.0)
    relevance = min(relevance, 100.0)

    final_score = max(0.0, min(100.0, relevance - spam * 0.5))
    qualified = final_score >= qualify_threshold
    reason = None if qualified else "below_threshold"

    return ScoreResult(
        qualified=qualified,
        relevance_score=round(relevance, 2),
        spam_risk_score=round(spam, 2),
        final_score=round(final_score, 2),
        reason=reason,
        signals=signals or None,
    )


def qualify_workflow_opportunity(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    qualify_threshold: float = QUALIFY_THRESHOLD,
) -> tuple[ScoreResult, backlink_db.OpportunityRow | None]:
    """Load workflow opportunity and score it."""
    wf = backlink_db.get_workflow(workflow_id, db_path=db_path)
    if not wf or not wf.opportunity_id:
        raise ValueError(f"Workflow has no opportunity: {workflow_id}")

    opp = backlink_db.get_opportunity(wf.opportunity_id, db_path=db_path)
    if not opp:
        raise ValueError(f"Opportunity not found for workflow: {workflow_id}")

    score = score_candidate(opp, db_path=db_path, qualify_threshold=qualify_threshold)
    return score, opp


def main() -> int:
    import argparse

    from worker_cli import run_score  # noqa: E402

    parser = argparse.ArgumentParser(description="Run scoring for one workflow")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    return run_score(args.workflow_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
