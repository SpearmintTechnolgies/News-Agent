"""V4 WriterEvidencePacket — structured authorized facts for natural prose. No plan graph."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote

# Common scraped chrome / bylines that must not enter the writer packet.
_BOILERPLATE_FACT_RE = re.compile(
    r"("
    r"skip to (search|main content)|"
    r"official website of the united states|"
    r"here'?s how you know|"
    r"\.gov website belongs to an official|"
    r"secure \.gov websites use https|"
    r"first appeared on|"
    r"appeared first on|"
    r"this post .+ first appeared|"
    r"bitcoin magazine .{0,80}proposes|"
    r"proposes new rules on crypto custody the regulator|"
    r"share (this|on)|"
    r"subscribe to (our )?newsletter|"
    r"cookie (policy|consent|settings)|"
    r"all rights reserved|"
    r"espa[nñ]ol\s+sections|"
    r"\bsections?\b.{0,20}\b(?:bitcoin|defi|ethereum|nfts?|web3)\b"
    r")",
    re.IGNORECASE,
)


def _is_boilerplate_proposition(text: str) -> bool:
    cleaned = str(text or "").strip()
    if not cleaned:
        return True
    if _BOILERPLATE_FACT_RE.search(cleaned):
        return True
    # Ultra-short chrome leftovers.
    if len(cleaned.split()) < 6 and any(
        token in cleaned.lower() for token in ("skip to", ".gov", "https", "cookie")
    ):
        return True
    return False


@dataclass(frozen=True)
class AuthorizedFact:
    id: str
    proposition: str
    attribution: str = ""
    numbers: tuple[str, ...] = ()
    polarity: str = "affirmed"
    modal: str = ""
    provenance: tuple[str, ...] = ()
    subject: str = ""
    predicate: str = ""
    object: str = ""
    status: str = ""
    time: str = ""
    location: str = ""
    entities: tuple[str, ...] = ()
    modality: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "proposition": self.proposition,
            "attribution": self.attribution,
            "numbers": list(self.numbers),
            "polarity": self.polarity,
            "modal": self.modal or self.modality,
            "provenance": list(self.provenance),
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "status": self.status,
            "time": self.time,
            "location": self.location,
            "entities": list(self.entities),
            "modality": self.modality or self.modal,
        }


@dataclass(frozen=True)
class AuthorizedQuote:
    id: str
    exact_quote: str
    speaker: str
    provenance: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "exact_quote": self.exact_quote,
            "speaker": self.speaker,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class WriterEvidencePacket:
    event_id: str
    story_topic: str
    authorized_facts: tuple[AuthorizedFact, ...]
    authorized_quotes: tuple[AuthorizedQuote, ...] = ()
    authorized_entities: tuple[str, ...] = ()
    source_context: dict[str, Any] = field(default_factory=dict)
    forbidden: tuple[str, ...] = (
        "unsupported motives",
        "unsupported predictions",
        "unsupported causality",
        "unsupported background",
        "unsupported numbers",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "story_topic": self.story_topic,
            "authorized_facts": [row.as_dict() for row in self.authorized_facts],
            "authorized_quotes": [row.as_dict() for row in self.authorized_quotes],
            "authorized_entities": list(self.authorized_entities),
            "source_context": dict(self.source_context),
            "forbidden": list(self.forbidden),
        }

    def claim_texts(self) -> list[str]:
        return [row.proposition for row in self.authorized_facts]

    def fact_ids(self) -> list[str]:
        return [row.id for row in self.authorized_facts]


def _fact_from_claim(claim: LedgerClaim) -> AuthorizedFact:
    """Structured semantic fact. Proposition is clean ledger English — never garbled compact."""
    semantic = semantic_fact_from_claim(claim)
    proposition = str(claim.text or "").strip()
    entities = tuple(
        dict.fromkeys(
            list(semantic.proper_names) + list(semantic.legal_titles)
        )
    )
    return AuthorizedFact(
        id=claim.claim_id,
        proposition=proposition,
        attribution=semantic.attribution,
        numbers=semantic.numbers,
        polarity=semantic.polarity or "affirmed",
        modal=semantic.modal,
        modality=semantic.modal,
        provenance=tuple(claim.evidence_ids),
        subject=semantic.subject,
        predicate=semantic.predicate or semantic.relation,
        object=semantic.complement or semantic.object,
        status=semantic.modal or ("negated" if semantic.polarity == "negated" else "affirmed"),
        time=semantic.time,
        location=semantic.location,
        entities=entities,
    )


def _quote_from_ledger(quote: LedgerQuote) -> AuthorizedQuote:
    return AuthorizedQuote(
        id=quote.quote_id,
        exact_quote=quote.text,
        speaker=quote.speaker or "",
        provenance=quote.evidence_id,
    )


def build_writer_evidence_packet(
    *,
    event_id: str,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any] | None = None,
    story_topic: str = "",
    max_facts: int = 18,
) -> WriterEvidencePacket:
    """Deterministic compact packet from ledgers. No LLM. No plan graph."""
    pack = article_input if isinstance(article_input, dict) else {}
    topic = str(story_topic or pack.get("representative_title") or pack.get("title") or event_id).strip()
    selected: list[AuthorizedFact] = []
    for row in ledgers.claims:
        if _is_boilerplate_proposition(getattr(row, "text", "") or ""):
            continue
        selected.append(_fact_from_claim(row))
        if len(selected) >= max_facts:
            break
    facts = tuple(selected)
    quotes = tuple(_quote_from_ledger(row) for row in ledgers.quotes[:8])
    entities: list[str] = []
    for fact in facts:
        for token in fact.entities:
            if token and token not in entities:
                entities.append(token)
        for token in fact.proposition.replace(".", " ").split():
            if token[:1].isupper() and len(token) > 2 and token not in entities:
                entities.append(token)
            if len(entities) >= 24:
                break
    evidence = pack.get("evidence") if isinstance(pack.get("evidence"), list) else []
    sources: list[str] = []
    timestamps: list[str] = []
    for row in evidence:
        if not isinstance(row, dict):
            continue
        src = str(row.get("source") or "").strip()
        if src and src not in sources:
            sources.append(src)
        published = str(row.get("published") or "").strip()
        if published and published not in timestamps:
            timestamps.append(published)
    return WriterEvidencePacket(
        event_id=str(event_id or ledgers.event_id or ""),
        story_topic=topic,
        authorized_facts=facts,
        authorized_quotes=quotes,
        authorized_entities=tuple(entities[:24]),
        source_context={
            "source_names": sources[:12],
            "publication_timestamps": timestamps[:12],
        },
    )
