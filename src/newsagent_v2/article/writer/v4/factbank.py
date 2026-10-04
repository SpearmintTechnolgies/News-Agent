"""V4 Event FactBank: extract, dedupe, and conflict-attribute propositions.

RAW source prose stays in research stores. FactBank holds normalized semantics only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.enrich import ATTRIBUTION_RE
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, word_count, words
from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim
from newsagent_v2.article.writer.evidence_ledger import (
    EvidenceLedgers,
    LedgerClaim,
    LedgerQuote,
    build_evidence_ledgers,
)
from newsagent_v2.article.writer.v4.packet import (
    AuthorizedFact,
    AuthorizedQuote,
    WriterEvidencePacket,
)

_DEDUP_OVERLAP = 0.82
_CONFLICT_SUBJECT_OVERLAP = 0.55


@dataclass(frozen=True)
class FactProposition:
    proposition_id: str
    text: str
    subject: str = ""
    predicate: str = ""
    object: str = ""
    entities: tuple[str, ...] = ()
    numbers: tuple[str, ...] = ()
    units: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    asset: str = ""
    attribution: str = ""
    modality: str = ""
    polarity: str = "affirmed"
    source_ids: tuple[str, ...] = ()
    primary_source_support: bool = False
    confidence: float = 0.7
    conflict_group: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposition_id": self.proposition_id,
            "text": self.text,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "entities": list(self.entities),
            "numbers": list(self.numbers),
            "units": list(self.units),
            "dates": list(self.dates),
            "asset": self.asset,
            "attribution": self.attribution,
            "modality": self.modality,
            "polarity": self.polarity,
            "source_ids": list(self.source_ids),
            "primary_source_support": self.primary_source_support,
            "confidence": self.confidence,
            "conflict_group": self.conflict_group,
        }


@dataclass
class FactBank:
    event_id: str
    propositions: tuple[FactProposition, ...] = ()
    quotes: tuple[AuthorizedQuote, ...] = ()
    conflicts: tuple[tuple[str, str], ...] = ()
    dedup_merged_count: int = 0
    raw_claim_count: int = 0

    @property
    def unique_proposition_count(self) -> int:
        return len(self.propositions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "unique_proposition_count": self.unique_proposition_count,
            "raw_claim_count": self.raw_claim_count,
            "dedup_merged_count": self.dedup_merged_count,
            "conflict_pairs": [list(pair) for pair in self.conflicts],
            "propositions": [row.as_dict() for row in self.propositions],
            "quotes": [row.as_dict() for row in self.quotes],
        }

    def to_ledgers(self) -> EvidenceLedgers:
        claims = tuple(
            LedgerClaim(
                claim_id=row.proposition_id,
                text=row.text,
                claim_type="fact",
                evidence_ids=row.source_ids or ("factbank",),
            )
            for row in self.propositions
        )
        quotes = tuple(
            LedgerQuote(
                quote_id=row.id,
                text=row.exact_quote,
                speaker=row.speaker,
                evidence_id=row.provenance or "factbank",
            )
            for row in self.quotes
        )
        return EvidenceLedgers(
            event_id=self.event_id,
            claims=claims,
            quotes=quotes,
            source="v4_factbank",
        )


def _normalize_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _token_set(text: str) -> set[str]:
    return {tok for tok in words(text) if len(tok) > 2}


def _overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _numbers(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.group(0) for m in NUMBER_TOKEN_RE.finditer(text or "")))


def _is_primary_source_id(source_id: str, pack: dict[str, Any]) -> bool:
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    for row in evidence:
        if not isinstance(row, dict):
            continue
        eid = str(row.get("evidence_id") or row.get("id") or "").strip()
        if eid and eid == source_id:
            role = str(row.get("source_role") or "").lower()
            stype = str(row.get("source_type") or "").lower()
            return role == "primary_evidence" or stype in {
                "regulator",
                "company",
                "official",
                "government",
                "official_regulator",
                "crypto_company",
                "exchange",
            }
    return False


def _claim_to_proposition(
    claim: LedgerClaim,
    *,
    pack: dict[str, Any],
    prop_id: str,
) -> FactProposition:
    semantic = semantic_fact_from_claim(claim)
    numbers = tuple(semantic.numbers) or _numbers(claim.text)
    entities = tuple(
        dict.fromkeys(list(semantic.proper_names) + list(semantic.legal_titles))
    )
    attribution = semantic.attribution or (
        ATTRIBUTION_RE.search(claim.text).group(0)
        if ATTRIBUTION_RE.search(claim.text)
        else ""
    )
    primary = any(_is_primary_source_id(sid, pack) for sid in claim.evidence_ids)
    asset = ""
    for token in ("Bitcoin", "BTC", "Ether", "Ethereum", "ETH", "USDT", "USDC"):
        if re.search(rf"\b{re.escape(token)}\b", claim.text, re.I):
            asset = token
            break
    return FactProposition(
        proposition_id=prop_id,
        text=claim.text.strip(),
        subject=semantic.subject,
        predicate=semantic.predicate or semantic.relation,
        object=semantic.complement or semantic.object,
        entities=entities,
        numbers=numbers,
        dates=tuple([semantic.time] if semantic.time else ()),
        asset=asset,
        attribution=attribution,
        modality=semantic.modal or "",
        polarity=semantic.polarity or "affirmed",
        source_ids=tuple(claim.evidence_ids),
        primary_source_support=primary,
        confidence=0.85 if primary else 0.7,
    )


def _should_merge(left: FactProposition, right: FactProposition) -> bool:
    if left.numbers and right.numbers and set(left.numbers) != set(right.numbers):
        # Same topic different numbers → conflict, not merge.
        return False
    return _overlap(_token_set(left.text), _token_set(right.text)) >= _DEDUP_OVERLAP


def _is_conflict(left: FactProposition, right: FactProposition) -> bool:
    if not left.numbers or not right.numbers:
        return False
    if set(left.numbers) == set(right.numbers):
        return False
    subj_overlap = _overlap(_token_set(left.subject or left.text), _token_set(right.subject or right.text))
    pred_overlap = _overlap(
        _token_set(left.predicate or ""),
        _token_set(right.predicate or ""),
    )
    return subj_overlap >= _CONFLICT_SUBJECT_OVERLAP and (
        pred_overlap >= 0.4 or not left.predicate or not right.predicate
    )


def build_fact_bank(
    *,
    event_id: str,
    pack: dict[str, Any],
    ledgers: EvidenceLedgers | None = None,
) -> FactBank:
    """Extract atomic propositions from every useful source unit; dedupe; keep conflicts."""
    from newsagent_v2.article.writer.v4.packet import _is_boilerplate_proposition

    base = ledgers or build_evidence_ledgers(pack)
    raw: list[FactProposition] = []
    for idx, claim in enumerate(base.claims, start=1):
        prop = _claim_to_proposition(claim, pack=pack, prop_id=f"P{idx:02d}")
        # Drop nav/chrome before capacity scoring — junk props were inflating
        # unique_proposition_count while starving the writer packet.
        if _is_boilerplate_proposition(prop.text):
            continue
        raw.append(prop)

    merged: list[FactProposition] = []
    merge_count = 0
    for prop in raw:
        host_idx = None
        for i, existing in enumerate(merged):
            if _should_merge(existing, prop):
                host_idx = i
                break
        if host_idx is None:
            merged.append(prop)
            continue
        host = merged[host_idx]
        sources = tuple(dict.fromkeys(list(host.source_ids) + list(prop.source_ids)))
        merged[host_idx] = FactProposition(
            proposition_id=host.proposition_id,
            text=host.text if word_count(host.text) >= word_count(prop.text) else prop.text,
            subject=host.subject or prop.subject,
            predicate=host.predicate or prop.predicate,
            object=host.object or prop.object,
            entities=tuple(dict.fromkeys(list(host.entities) + list(prop.entities))),
            numbers=host.numbers or prop.numbers,
            units=host.units or prop.units,
            dates=tuple(dict.fromkeys(list(host.dates) + list(prop.dates))),
            asset=host.asset or prop.asset,
            attribution=host.attribution or prop.attribution,
            modality=host.modality or prop.modality,
            polarity=host.polarity,
            source_ids=sources,
            primary_source_support=host.primary_source_support or prop.primary_source_support,
            confidence=max(host.confidence, prop.confidence),
            conflict_group=host.conflict_group,
        )
        merge_count += 1

    conflicts: list[tuple[str, str]] = []
    # Pairwise conflict attribution — keep both propositions.
    for i, left in enumerate(merged):
        for right in merged[i + 1 :]:
            if _is_conflict(left, right):
                conflicts.append((left.proposition_id, right.proposition_id))

    # Re-id stably after merge.
    final: list[FactProposition] = []
    for idx, prop in enumerate(merged, start=1):
        final.append(
            FactProposition(
                proposition_id=f"P{idx:02d}",
                text=prop.text,
                subject=prop.subject,
                predicate=prop.predicate,
                object=prop.object,
                entities=prop.entities,
                numbers=prop.numbers,
                units=prop.units,
                dates=prop.dates,
                asset=prop.asset,
                attribution=prop.attribution,
                modality=prop.modality,
                polarity=prop.polarity,
                source_ids=prop.source_ids,
                primary_source_support=prop.primary_source_support,
                confidence=prop.confidence,
                conflict_group=prop.conflict_group,
            )
        )

    quotes = tuple(
        AuthorizedQuote(
            id=row.quote_id,
            exact_quote=row.text,
            speaker=row.speaker,
            provenance=row.evidence_id,
        )
        for row in base.quotes
    )
    return FactBank(
        event_id=event_id,
        propositions=tuple(final),
        quotes=quotes,
        conflicts=tuple(conflicts),
        dedup_merged_count=merge_count,
        raw_claim_count=len(raw),
    )


def fact_bank_to_writer_packet(
    bank: FactBank,
    *,
    story_topic: str = "",
    source_names: list[str] | None = None,
    max_facts: int = 24,
) -> WriterEvidencePacket:
    """Semantic-only packet. No raw source prose fields."""
    from newsagent_v2.article.writer.v4.packet import _is_boilerplate_proposition

    selected: list[AuthorizedFact] = []
    for row in bank.propositions:
        if _is_boilerplate_proposition(row.text):
            continue
        selected.append(
            AuthorizedFact(
                id=row.proposition_id,
                proposition=row.text,
                attribution=row.attribution,
                numbers=row.numbers,
                polarity=row.polarity,
                modal=row.modality,
                modality=row.modality,
                provenance=row.source_ids,
                subject=row.subject,
                predicate=row.predicate,
                object=row.object,
                status=row.modality or ("negated" if row.polarity == "negated" else "affirmed"),
                time=row.dates[0] if row.dates else "",
                entities=row.entities,
            )
        )
        if len(selected) >= max_facts:
            break
    facts = tuple(selected)
    entities: list[str] = []
    for fact in facts:
        for token in fact.entities:
            if token and token not in entities:
                entities.append(token)
    quotes: list[AuthorizedQuote] = []
    for row in bank.quotes:
        speaker = str(row.speaker or "").strip()
        quote_text = str(row.exact_quote or "").strip()
        if not quote_text:
            continue
        if _is_boilerplate_proposition(quote_text):
            continue
        if not speaker or speaker.lower() in {"unknown", "unknown speaker", "none", "n/a"}:
            inferred = ""
            needle = quote_text.strip().strip('"').strip("'")
            for fact in facts:
                if needle and needle in fact.proposition:
                    match = re.search(
                        r"([A-Z][\w .,'-]{2,80}?)\s+said\b",
                        fact.proposition,
                    )
                    if match:
                        inferred = match.group(1).strip(" ,.-")
                        break
            if not inferred:
                # Unattributed orphan quotes fail fabricated_quote in QA.
                continue
            row = AuthorizedQuote(
                id=row.id,
                exact_quote=quote_text,
                speaker=inferred,
                provenance=row.provenance,
            )
            speaker = inferred
        quotes.append(row)
        if len(quotes) >= 8:
            break
    return WriterEvidencePacket(
        event_id=bank.event_id,
        story_topic=story_topic or bank.event_id,
        authorized_facts=facts,
        authorized_quotes=tuple(quotes),
        authorized_entities=tuple(entities[:24]),
        source_context={
            "source_names": list(source_names or [])[:12],
            "publication_timestamps": [],
            "unique_propositions": bank.unique_proposition_count,
            "conflict_pairs": len(bank.conflicts),
        },
    )
