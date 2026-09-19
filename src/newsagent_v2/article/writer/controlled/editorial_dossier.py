"""Compact EditorialDossier for paid writers. Facts are meaning, not source copy."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.controlled.proposition import proposition_frame_from_semantic_fact
from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers

EDITORIAL_SYSTEM_PROMPT = """You are a CoinNetwork staff writer.

NewsAgent already chose the facts. You choose the language.

Write polished professional financial/crypto journalism in natural grammatical English.
Freely vary syntax and sentence rhythm. Realize supplied facts; do not concatenate slots.
Preserve factual meaning, polarity, numbers, names, dates, and attribution.
Do not add factual propositions. Do not invent background, motives, consequences, or predictions.
Use only supplied quotes, reproduced exactly.
Target approximately 400–550 words when the supplied facts support it; never pad with new facts.

Return exactly one JSON object:
headline, dek, seo_title, meta_description, slug, entities[], keywords[],
paragraphs[{paragraph_id, sentences[{sentence_id, text, fact_ids_used, quote_ids_used}]}].
""".strip()


@dataclass(frozen=True)
class EditorialDossier:
    event_id: str
    angle: str
    target_min_words: int
    target_max_words: int
    facts: tuple[dict[str, Any], ...]
    statements: tuple[dict[str, Any], ...]
    quotes: tuple[dict[str, Any], ...]
    paragraph_order: tuple[dict[str, Any], ...]
    entities: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def build_editorial_dossier(
    plan: ArticlePlan,
    ledgers: EvidenceLedgers,
    *,
    target_min_words: int = 400,
    target_max_words: int = 550,
) -> EditorialDossier:
    claims = ledgers.claim_by_id()
    quotes = {row.quote_id: row for row in ledgers.quotes}
    fact_rows: list[dict[str, Any]] = []
    statements: list[dict[str, Any]] = []
    quote_rows: list[dict[str, Any]] = []
    paragraphs: list[dict[str, Any]] = []
    seen_facts: set[str] = set()
    seen_quotes: set[str] = set()
    names: list[str] = []

    for para in plan.paragraph_plans:
        para_facts: list[str] = []
        for cid in (*para.required_claim_ids, *para.optional_claim_ids):
            if cid not in claims or cid in seen_facts:
                continue
            seen_facts.add(cid)
            para_facts.append(cid)
            fact = semantic_fact_from_claim(claims[cid])
            frame = proposition_frame_from_semantic_fact(fact).as_dict()
            fact_rows.append(frame)
            if fact.attribution:
                statements.append(
                    {
                        "fact_id": cid,
                        "attribution": fact.attribution,
                        "polarity": fact.polarity,
                        "modal": fact.modal,
                    }
                )
            names.extend(fact.proper_names)
            names.extend(fact.legal_titles)
        para_quotes: list[str] = []
        for qid in para.allowed_quote_ids:
            if qid not in quotes or qid in seen_quotes:
                continue
            seen_quotes.add(qid)
            para_quotes.append(qid)
            quote_rows.append(
                {
                    "quote_id": qid,
                    "exact_text": quotes[qid].text,
                    "speaker": quotes[qid].speaker,
                }
            )
        paragraphs.append(
            {
                "paragraph_id": para.paragraph_id,
                "purpose": para.editorial_purpose,
                "relationship": para.relationship,
                "fact_ids": para_facts,
                "quote_ids": para_quotes,
            }
        )

    unique_names = tuple(dict.fromkeys(n for n in names if n))
    payload = {
        "event_id": plan.event_id,
        "angle": str(plan.headline_requirements.get("primary_action") or plan.category or "news"),
        "category": plan.category,
        "headline_roles": dict(plan.headline_requirements),
        "dek_roles": dict(plan.dek_requirements),
        "target_min_words": target_min_words,
        "target_max_words": target_max_words,
        "hard_minimum_words": 350,
        "facts": fact_rows,
        "attributed_statements": statements,
        "quotes": quote_rows,
        "paragraphs": paragraphs,
        "entities": list(unique_names),
        "pad_to_word_target": False,
        "invent_facts": False,
    }
    blob = json.dumps(payload)
    for claim in ledgers.claims:
        if len(claim.text.split()) >= 18 and claim.text in blob:
            raise RuntimeError("EditorialDossier leaked raw claim.text")
    return EditorialDossier(
        event_id=plan.event_id,
        angle=str(payload["angle"]),
        target_min_words=target_min_words,
        target_max_words=target_max_words,
        facts=tuple(fact_rows),
        statements=tuple(statements),
        quotes=tuple(quote_rows),
        paragraph_order=tuple(paragraphs),
        entities=unique_names,
        payload=payload,
    )


def dossier_messages(dossier: EditorialDossier) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": EDITORIAL_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(dossier.as_dict(), ensure_ascii=False, separators=(",", ":"))},
    ]
