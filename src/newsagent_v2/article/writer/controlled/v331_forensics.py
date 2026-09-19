"""Offline V3.3.1 replay of the persisted V3.3 article. Does not rewrite artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa.textutil import words
from newsagent_v2.article.writer.controlled.proposition import proposition_frame_from_claim
from newsagent_v2.article.writer.controlled.realization import REALIZATION_INVALID, classify_article_sentences
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim

V33_LIVE = Path(
    "benchmarks/writer_bakeoff/controlled_v33_fresh/live_runs/"
    "20260916T093822Z/groq_gpt_oss_20b_controlled_writer_v33"
)


def frame_would_prevent_defect(sentence: str, frame: dict[str, Any]) -> bool:
    """True when the proposed frame supplies subject+predicate the fragment lacked."""
    text = " ".join(words(sentence))
    has_verbish = bool(frame.get("predicate"))
    has_subject = bool(frame.get("subject"))
    fragment = len(text.split()) < 8 or sentence.strip()[:1].islower() or not sentence.strip().endswith((".", "!", "?"))
    return bool(fragment and has_verbish and has_subject)


def replay_v33_prose(run_dir: Path | None = None, ledgers: EvidenceLedgers | None = None) -> dict[str, Any]:
    dest = Path(run_dir or V33_LIVE)
    article = json.loads((dest / "article.json").read_text(encoding="utf-8"))
    report = classify_article_sentences(
        headline=str(article.get("headline") or ""),
        dek=str(article.get("dek") or ""),
        body=str(article.get("article_body") or ""),
    )
    claims: dict[str, LedgerClaim] = {}
    if ledgers is not None:
        claims = ledgers.claim_by_id()
    else:
        raw = json.loads((dest / "ledgers.json").read_text(encoding="utf-8"))
        for row in raw.get("claims") or []:
            claims[row["claim_id"]] = LedgerClaim(
                row["claim_id"],
                row["text"],
                row.get("claim_type") or "fact",
                tuple(row.get("evidence_ids") or ()),
            )
    invalid = [item for item in report["units"] if item["label"] == REALIZATION_INVALID]
    for item in invalid:
        item["frame_would_prevent"] = False
        if claims:
            frame = proposition_frame_from_claim(next(iter(claims.values()))).as_dict()
            item["frame_would_prevent"] = frame_would_prevent_defect(item["text"], frame)
    return report
