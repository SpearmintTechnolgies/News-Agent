"""Offline forensics for the persisted V3.2 fresh proof. Does not modify run artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa.grounding import is_connective_sentence
from newsagent_v2.article.qa.headline import check_headline
from newsagent_v2.article.qa.textutil import split_sentences
from newsagent_v2.article.writer.controlled.quarantine import classify_sentence, paragraph_plan_from_dict
from newsagent_v2.article.writer.controlled.renderer_contract import editorial_truncation_issues
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote
from newsagent_v2.article.writer.ledger_resolve import matching_ledger_claims

DEFAULT_V32_RUN = (
    Path(__file__).resolve().parents[5]
    / "benchmarks"
    / "writer_bakeoff"
    / "controlled_v32_fresh"
    / "live_runs"
    / "20260916T075412Z"
    / "groq_gpt_oss_20b_controlled_writer_v32"
)

# Closed labels for the 18 quarantined assertions in the persisted V3.2 run.
QUARANTINE_CLASSES = {
    "In response, industry leaders highlighted the need for clearer regulatory frameworks.": "D",
    "They emphasized that without decisive action, capital might seek more stable environments abroad.": "G",
    "The discussion underscored the urgency of establishing definitive rules to support growth and secure sustainable expansion in the digital asset space.": "D",
    "They noted that what the proposal leaves unresolved is the durability of agency rules, as regulations can shift over time.": "B",
    "The narrative stresses the need for consistent, long‑term policy to maintain investor confidence in domestic markets, and foster a stable environment that encourages long‑term growth and regulatory compliance for future investors globally.": "E",
    "This was an opportunity bigger than Ripple or any single company; we did this.": "B",
    "The focus shifted from practical regulatory solutions to partisan battles, undermining the effort to create a stable legal framework for digital assets and foster long‑term confidence among investors and institutions worldwide to drive growth ahead.": "E",
    "In the absence of a clear market‑structure law, the industry will focus close attention on alternative regulatory pathways.": "C",
    "The Senate’s failure on Tuesday to advance the Clarity Act underscores the challenges facing lawmakers as they balance consumer protection with innovation.": "D",
    "The outcome may redirect capital flows and influence future legislative efforts.": "G",
    "Stakeholders now seek international cooperation to bridge regulatory gaps, while analysts note global exchanges may step in to offer compliance frameworks that fill the domestic void for.": "E",
    "Congress and the Senate's decision came as a blow to the army of lobbyists and advocacy groups that had rallied behind the legislation.": "B",
    "The setback for the crypto industry's top policy goal may send the process back into November, delaying critical progress.": "C",
    "Without a united front, lawmakers face a fragmented agenda that could stall further advances.": "G",
    "The community remains watchful, hoping that the next session will address these divisions and restore momentum.": "G",
    "Meanwhile, industry stakeholders are exploring alternative legislative pathways, seeking bipartisan support to establish clear guidelines that safeguard investors while fostering innovation in the evolving digital‑asset landscape for and stability.": "E",
    "This outcome underscores the volatility inherent in high‑stakes legislation, where timing and political alignment can decisively influence the fate of regulatory initiatives.": "D",
    "Industry will now reassess strategies to navigate the legislative landscape.": "G",
}


def _norm(text: str) -> str:
    return " ".join(str(text or "").replace("\u2011", "-").replace("’", "'").split())


def _lookup_class(sentence: str) -> str:
    key = sentence.strip()
    if key in QUARANTINE_CLASSES:
        return QUARANTINE_CLASSES[key]
    compact = _norm(key)
    for stored, klass in QUARANTINE_CLASSES.items():
        if _norm(stored) == compact:
            return klass
    return "K"


def _ledgers(raw: dict[str, Any]) -> EvidenceLedgers:
    claims = tuple(
        LedgerClaim(row["claim_id"], row["text"], row.get("claim_type") or "fact", tuple(row.get("evidence_ids") or ()))
        for row in raw.get("claims") or []
    )
    quotes = tuple(
        LedgerQuote(
            row["quote_id"],
            row["text"],
            row.get("attribution") or row.get("speaker") or "",
            (row.get("evidence_ids") or [""])[0],
        )
        for row in raw.get("quotes") or []
    )
    return EvidenceLedgers(event_id=str(raw.get("event_id") or "event-007"), claims=claims, quotes=quotes)


def analyze_v32_run(run_dir: Path | None = None) -> dict[str, Any]:
    dest = Path(run_dir) if run_dir is not None else DEFAULT_V32_RUN
    native = json.loads((dest / "native.json").read_text(encoding="utf-8"))
    plan = json.loads((dest / "article_plan.json").read_text(encoding="utf-8"))
    ledgers_raw = json.loads((dest / "ledgers.json").read_text(encoding="utf-8"))
    article = json.loads((dest / "article.json").read_text(encoding="utf-8"))
    messages = json.loads((dest / "renderer_messages.json").read_text(encoding="utf-8"))
    payload = json.loads(messages["messages"][1]["content"])
    ledgers = _ledgers(ledgers_raw)
    plans = {
        row["paragraph_id"]: paragraph_plan_from_dict(row)
        for row in plan.get("paragraph_plans") or []
    }
    rows: list[dict[str, Any]] = []
    counts = {letter: 0 for letter in "ABCDEFGHIJK"}
    for para in native.get("paragraphs") or []:
        pid = str(para.get("paragraph_id") or "")
        para_plan = plans[pid]
        for sentence in split_sentences(str(para.get("text") or "")):
            if is_connective_sentence(sentence):
                continue
            unit = classify_sentence(sentence, para_plan, ledgers)
            if unit.retained and unit.status == "GROUNDED":
                klass = "A"
            else:
                klass = _lookup_class(sentence)
            nearest = matching_ledger_claims(sentence, ledgers)
            if not nearest:
                allowed = set(para_plan.allowed_claim_ids)
                nearest = [
                    row
                    for row in ledgers.claims
                    if row.claim_id in allowed
                    and any(tok in row.text.lower() for tok in sentence.lower().split() if len(tok) > 5)
                ][:2]
            counts[klass] = counts.get(klass, 0) + 1
            rows.append(
                {
                    "paragraph_id": pid,
                    "sentence": sentence,
                    "class": klass,
                    "quarantined": not unit.retained,
                    "status": unit.status,
                    "allowed_claim_ids": list(para_plan.allowed_claim_ids),
                    "mapped_claim_ids": list(unit.claim_ids),
                    "nearest_claim_ids": [row.claim_id for row in nearest],
                    "atomic_would_prevent": klass in {"C", "D", "E", "F", "G"},
                }
            )
    long_fields = []
    for para in payload.get("paragraph_plans") or []:
        for fact in para.get("allowed_semantic_facts") or []:
            for key in ("subject", "object", "qualifiers"):
                value = fact.get(key)
                blob = " ".join(value) if isinstance(value, list) else str(value or "")
                if len(blob.split()) >= 8:
                    long_fields.append({"claim_id": fact.get("claim_id"), "field": key, "text": blob})
    headline = str(native.get("headline") or "")
    dek = str(native.get("dek") or article.get("dek") or "")
    return {
        "total_assertions": len(rows),
        "class_counts": counts,
        "supported": counts["A"],
        "quarantined_rows": [row for row in rows if row["quarantined"]],
        "semantic_long_source_fields": long_fields,
        "semantic_fact_lexical_leakage": bool(long_fields),
        "payload_has_raw_claim_text_field": any(
            "text" in (fact or {})
            for para in payload.get("paragraph_plans") or []
            for fact in para.get("allowed_semantic_facts") or []
        ),
        "quotes_in_assembled_article": bool(article.get("quotes")),
        "headline_qa_issues": check_headline({"headline": headline}, {"evidence": []}),
        "headline_truncation_issues": editorial_truncation_issues(headline, field="headline"),
        "dek_truncation_issues": editorial_truncation_issues(dek, field="dek"),
        "historical_artifacts_modified": False,
    }
