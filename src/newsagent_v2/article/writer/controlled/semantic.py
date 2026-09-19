"""Renderer-safe semantic facts from existing ledger claims. No LLM. No new facts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from newsagent_v2.article.qa.grounding import content_tokens, STOPWORDS
from newsagent_v2.article.qa.structure import extract_quoted_spans
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, word_count, words
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim

MAX_QUALIFIER_WORDS = 5
LEGAL_TITLE_RE = re.compile(
    r"\b(?:the\s+)?(?:[A-Z][A-Za-z0-9'’.-]*(?:\s+[A-Z][A-Za-z0-9'’.-]*){0,6}\s+)?(?:Act|Bill)\b"
)
PROPER_NAME_RE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b")
DATE_RE = re.compile(
    r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\b"
    r"|\b\d{1,2}:\d{2}\s*(?:a\.m\.|p\.m\.|am|pm|ET|UTC)?\b"
    r"|\b\d+\s+days?\b",
    re.IGNORECASE,
)
ATTRIBUTION_SPLIT_RE = re.compile(
    r"\b(said|says|told|according to|announced|stated|reported|confirmed)\b",
    re.IGNORECASE,
)
RELATION_RE = re.compile(
    r"\b(released|disclosed|announced|confirmed|said|told|includes|include|"
    r"included|would allow|will allow|allow|requires?|required|carry|comes|"
    r"described|reflect(?:s|ed)?|agreed|sent|holding|divest|enforce|"
    r"governing|covering|involving|push|pushed|means|changes|changed|failed|"
    r"hashed|dragged|reacted|gave|needs|elevated|focusing|spent|leave|leaves|"
    r"argued|noted|secure|secured)\b",
    re.IGNORECASE,
)

MAX_SUBJECT_WORDS = 4
MAX_OBJECT_WORDS = 6
LOCATION_RE = re.compile(
    r"\b(?:United States|U\.S\.|Washington|European Union|New York|London)\b"
)
POLARITY_NEG_RE = re.compile(
    r"\b(?:not|never|no|cannot|can't|won't|wouldn't|didn't|doesn't|isn't|aren't|wasn't|weren't)\b",
    re.IGNORECASE,
)
MODAL_RE = re.compile(r"\b(?:would|will|could|can|may|might|must|should)\b", re.IGNORECASE)
SLOT_GLUE = STOPWORDS | frozenset(
    {
        "such",
        "toward",
        "towards",
        "where",
        "while",
        "which",
        "that",
        "this",
        "those",
        "these",
        "could",
        "would",
        "might",
        "may",
        "also",
        "now",
        "still",
        "already",
        "here",
        "how",
        "what",
        "does",
        "leave",
        "unresolved",
        "question",
        "bigger",
        "than",
        "across",
        "finish",
        "line",
        "major",
        "setback",
        "prolonged",
        "needed",
        "need",
        "advance",
        "intense",
        "negotiations",
        "months",
        "trying",
        "bring",
        "hashed",
    }
)


@dataclass(frozen=True)
class SemanticFact:
    claim_id: str
    subject: str
    relation: str
    object: str
    qualifiers: tuple[str, ...]
    numbers: tuple[str, ...]
    dates: tuple[str, ...]
    attribution: str
    evidence_ids: tuple[str, ...]
    proper_names: tuple[str, ...]
    legal_titles: tuple[str, ...]
    polarity: str = "affirmed"
    modal: str = ""
    time: str = ""
    location: str = ""
    complement: str = ""
    predicate: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "subject": self.subject,
            "relation": self.relation,
            "predicate": self.predicate or self.relation,
            "object": self.object,
            "complement": self.complement or self.object,
            "qualifiers": list(self.qualifiers),
            "numbers": list(self.numbers),
            "dates": list(self.dates),
            "time": self.time,
            "location": self.location,
            "polarity": self.polarity,
            "modal": self.modal,
            "attribution": self.attribution,
            "evidence_ids": list(self.evidence_ids),
            "proper_names": list(self.proper_names),
            "legal_titles": list(self.legal_titles),
        }

    def renderer_tokens(self) -> set[str]:
        blob = " ".join(
            [
                self.subject,
                self.relation,
                self.predicate,
                self.object,
                self.complement,
                " ".join(self.qualifiers),
                " ".join(self.numbers),
                " ".join(self.dates),
                self.time,
                self.location,
                self.attribution,
                " ".join(self.proper_names),
                " ".join(self.legal_titles),
            ]
        )
        return set(content_tokens(blob))


def _unique(rows: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        key = re.sub(r"\s+", " ", row).strip()
        if not key or key.lower() in seen:
            continue
        seen.add(key.lower())
        out.append(key)
    return tuple(out)


def _slot_tokens(text: str, *, maximum: int) -> str:
    keep: list[str] = []
    for token in str(text or "").split():
        cleaned = token.strip(" ,;:\"“”")
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if NUMBER_TOKEN_RE.search(cleaned) or DATE_RE.search(cleaned) or LEGAL_TITLE_RE.search(cleaned):
            keep.append(cleaned)
        elif cleaned[:1].isupper() and lowered not in SLOT_GLUE:
            keep.append(cleaned)
        elif len(cleaned) > 3 and lowered not in SLOT_GLUE and not RELATION_RE.fullmatch(lowered):
            keep.append(cleaned)
        if len(keep) >= maximum:
            break
    return " ".join(keep)


def _clip_phrase(text: str, maximum: int = MAX_QUALIFIER_WORDS) -> str:
    slotted = _slot_tokens(text, maximum=maximum)
    if slotted:
        return slotted
    parts = str(text or "").split()
    return " ".join(parts[:maximum]).strip(" ,;:")


def _grammatical_np(text: str) -> str:
    blob = re.sub(r"\s+", " ", str(text or "")).strip(" ,;:")
    if not blob:
        return ""
    if blob[:1].isupper() or NUMBER_TOKEN_RE.match(blob.split()[0]):
        return blob
    return blob


def _strip_quotes(text: str) -> str:
    stripped = text
    for span in extract_quoted_spans(text):
        stripped = stripped.replace(f'"{span}"', " ").replace(f"“{span}”", " ")
    return re.sub(r"\s+", " ", stripped).strip()


def semantic_fact_from_claim(claim: LedgerClaim) -> SemanticFact:
    raw = str(claim.text or "").strip()
    text = _strip_quotes(raw)
    numbers = _unique(NUMBER_TOKEN_RE.findall(text))
    dates = _unique(DATE_RE.findall(text))
    titles = _unique(LEGAL_TITLE_RE.findall(text))
    names = [
        match.group(1)
        for match in PROPER_NAME_RE.finditer(text)
        if match.group(1) not in titles and match.group(1).lower() not in STOPWORDS
    ]
    proper = _unique(names)
    attribution = ""
    attr = ATTRIBUTION_SPLIT_RE.search(text)
    if attr:
        before = text[: attr.start()].strip(" ,;:")
        speaker = before.split(",")[-1].strip()
        if 1 <= word_count(speaker) <= 6:
            attribution = f"{speaker} {attr.group(1).lower()}".strip()
    relation = ""
    polarity = "affirmed"
    modal = ""
    rel = RELATION_RE.search(text)
    if rel:
        relation = rel.group(0).lower()
        window = text[max(0, rel.start() - 24) : min(len(text), rel.end() + 48)]
        if POLARITY_NEG_RE.search(window):
            polarity = "negated"
        modal_hit = MODAL_RE.search(text[rel.end() : rel.end() + 24]) or MODAL_RE.search(window)
        if modal_hit:
            modal = modal_hit.group(0).lower()
        subject = _clip_phrase(text[: rel.start()], MAX_SUBJECT_WORDS)
        after = text[rel.end() :].strip(" ,;:")
        chunks = re.split(r",\s+|\s+with\s+|\s+after\s+|\s+alongside\s+|\s+where\s+", after, maxsplit=2)
        obj = _clip_phrase(chunks[0], MAX_OBJECT_WORDS)
        extra = [_clip_phrase(part, MAX_QUALIFIER_WORDS) for part in chunks[1:] if part.strip()]
    else:
        subject = _clip_phrase(" ".join(text.split()[:8]), MAX_SUBJECT_WORDS)
        obj = _clip_phrase(" ".join(text.split()[4:16]), MAX_OBJECT_WORDS)
        extra = []
        if POLARITY_NEG_RE.search(text):
            polarity = "negated"
    qualifiers = _unique([row for row in extra if row and row.lower() not in {subject.lower(), obj.lower()}])
    locations = _unique(LOCATION_RE.findall(text))
    time_val = dates[0] if dates else ""
    complement = _grammatical_np(obj)
    if not complement and titles:
        complement = titles[0]
    return SemanticFact(
        claim_id=claim.claim_id,
        subject=subject,
        relation=relation,
        object=obj,
        qualifiers=qualifiers,
        numbers=numbers,
        dates=tuple(item.strip(" .,;:") for item in dates),
        attribution=attribution,
        evidence_ids=tuple(claim.evidence_ids),
        proper_names=proper,
        legal_titles=titles,
        polarity=polarity,
        modal=modal,
        time=time_val,
        location=locations[0] if locations else "",
        complement=complement,
        predicate=relation,
    )


def semantic_facts_for_ids(ledgers: EvidenceLedgers, claim_ids: tuple[str, ...] | list[str]) -> list[SemanticFact]:
    index = ledgers.claim_by_id()
    return [semantic_fact_from_claim(index[cid]) for cid in claim_ids if cid in index]


def _norm_token(token: str) -> str:
    return str(token or "").lower().strip(".,;:'\"“”")


def semantic_adds_no_facts(fact: SemanticFact, claim: LedgerClaim) -> bool:
    """Every renderer-safe content token must already exist in the original claim."""
    source = {_norm_token(tok) for tok in content_tokens(claim.text)} | {
        _norm_token(tok) for tok in words(claim.text)
    }
    extra = []
    for token in fact.renderer_tokens():
        key = _norm_token(token)
        if not key or key in STOPWORDS or key in source:
            continue
        if any("-" in item and key in item.split("-") for item in source):
            continue
        extra.append(token)
    return not extra


def distinctive_source_syntax(field: str, claim_text: str) -> bool:
    """True when a semantic slot still contains an 8+ word source span."""
    blob = re.sub(r"\s+", " ", str(field or "")).strip()
    source = re.sub(r"\s+", " ", str(claim_text or "")).strip()
    if len(blob.split()) < 8:
        return False
    return blob.lower() in source.lower()


def verbalize_semantic_fact(fact: SemanticFact) -> str:
    """Proposition-frame wording for the offline fake renderer. Not a similarity-evasion paraphrase."""
    from newsagent_v2.article.writer.controlled.proposition import (
        proposition_frame_from_semantic_fact,
        realize_proposition_frame,
    )

    return realize_proposition_frame(proposition_frame_from_semantic_fact(fact))
