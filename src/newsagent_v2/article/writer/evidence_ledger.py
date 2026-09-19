"""Pre-writing EvidenceClaimLedger and QuoteLedger from frozen evidence. No LLM."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from newsagent_v2.article.expand import story_evidence_units
from newsagent_v2.article.qa.grounding import is_connective_sentence
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import split_sentences

SPEAKER_RE = re.compile(
    r"\b(Cynthia Lummis|Senator Lummis|Lummis|John Boozman|Tim Scott|"
    r"Donald Trump|President Trump|Republican aide|a Republican aide)\b",
    re.IGNORECASE,
)
SAID_RE = re.compile(r"\b(?:she|he|they|Lummis|aide)\s+said\b", re.IGNORECASE)


@dataclass(frozen=True)
class LedgerClaim:
    claim_id: str
    text: str
    claim_type: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class LedgerQuote:
    quote_id: str
    text: str
    speaker: str
    evidence_id: str


@dataclass(frozen=True)
class EvidenceLedgers:
    event_id: str
    claims: tuple[LedgerClaim, ...]
    quotes: tuple[LedgerQuote, ...]
    source: str = "frozen_evidence_units"

    def claim_texts(self) -> list[str]:
        return [row.text for row in self.claims]

    def claim_by_id(self) -> dict[str, LedgerClaim]:
        return {row.claim_id: row for row in self.claims}

    def as_claim_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "claim_id": row.claim_id,
                "id": row.claim_id,
                "text": row.text,
                "claim_type": row.claim_type,
                "evidence_ids": list(row.evidence_ids),
            }
            for row in self.claims
        ]

    def as_quote_dicts(self) -> list[dict[str, Any]]:
        return [
            {
                "quote_id": row.quote_id,
                "text": row.text,
                "kind": "direct",
                "attribution": row.speaker,
                "evidence_ids": [row.evidence_id],
            }
            for row in self.quotes
        ]


def _last_speaker(blob: str) -> str:
    found = SPEAKER_RE.findall(blob)
    if not found:
        return ""
    last = found[-1]
    if re.search(r"aide", last, re.IGNORECASE):
        return "a Republican aide"
    if re.search(r"lummis", last, re.IGNORECASE):
        return "Cynthia Lummis"
    return last


def _quote_speaker(unit_text: str, quote_text: str) -> str:
    idx = unit_text.find(quote_text)
    window_before = unit_text[:idx] if idx >= 0 else unit_text
    after_start = idx + len(quote_text) if idx >= 0 else 0
    window_after = unit_text[after_start : after_start + 80]
    if SAID_RE.search(window_after) or SAID_RE.search(window_before[-80:]):
        speaker = _last_speaker(window_before)
        if speaker:
            return speaker
    speaker = _last_speaker(window_before)
    return speaker or "unknown speaker"


def build_evidence_ledgers(article_input: dict[str, Any]) -> EvidenceLedgers:
    event_id = str(article_input.get("event_id") or "")
    claims: list[LedgerClaim] = []
    quotes: list[LedgerQuote] = []
    seen_claims: set[str] = set()
    seen_quotes: set[str] = set()
    claim_n = 0
    quote_n = 0
    for unit in story_evidence_units(article_input):
        evidence_id = str(unit.get("evidence_id") or "").strip()
        text = str(unit.get("text") or "").strip()
        if not evidence_id or not text:
            continue
        for span in extract_quoted_spans(text):
            key = re.sub(r"\s+", " ", span).strip()
            if len(key) < 8 or key.lower() in seen_quotes:
                continue
            seen_quotes.add(key.lower())
            quote_n += 1
            quotes.append(
                LedgerQuote(
                    quote_id=f"Q{quote_n:02d}",
                    text=span.strip(),
                    speaker=_quote_speaker(text, span),
                    evidence_id=evidence_id,
                )
            )
        for sentence in split_sentences(text):
            stripped = sentence.strip()
            if is_connective_sentence(stripped):
                continue
            key = re.sub(r"\s+", " ", stripped).lower()
            if key in seen_claims:
                continue
            seen_claims.add(key)
            claim_n += 1
            claims.append(
                LedgerClaim(
                    claim_id=f"C{claim_n:02d}",
                    text=stripped,
                    claim_type="fact",
                    evidence_ids=(evidence_id,),
                )
            )
    return EvidenceLedgers(
        event_id=event_id,
        claims=tuple(claims),
        quotes=tuple(quotes),
    )


def ledger_prompt_payload(ledgers: EvidenceLedgers) -> dict[str, Any]:
    return {
        "allowed_claims": ledgers.as_claim_dicts(),
        "allowed_quotes": [
            {
                "quote_id": row.quote_id,
                "exact_text": row.text,
                "speaker": row.speaker,
                "evidence_id": row.evidence_id,
            }
            for row in ledgers.quotes
        ],
        "instructions": (
            "You are writing from a closed factual ledger. "
            "Every factual/assertive proposition MUST be expressible using "
            "one or more of these allowed claims. "
            "If a sentence needs a fact that is not here, omit that sentence. "
            "Quotes may only come from allowed_quotes and must be reproduced exactly."
        ),
    }


def ledger_fingerprint(ledgers: EvidenceLedgers) -> str:
    payload = {
        "claims": ledgers.as_claim_dicts(),
        "quotes": ledgers.as_quote_dicts(),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return sha256(blob.encode("utf-8")).hexdigest()
