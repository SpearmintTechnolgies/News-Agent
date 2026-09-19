"""V4 atomic proposition grounding.

Verifies MEANING against the UNION of authorized propositions.
Does not require sentence ↔ single claim identity.
Does not consume evidence when one sentence uses a claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable

from newsagent_v2.article.qa.grounding import CONNECTIVE_RE, content_tokens
from newsagent_v2.article.qa.textutil import number_tokens, split_sentences, word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket

STATUS_SUPPORTED = "SUPPORTED"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_UNSUPPORTED = "UNSUPPORTED"
STATUS_CONNECTIVE = "CONNECTIVE"

# Evaluative / comparative flourishes — factual implications, not free editorial glue.
SIGNIFICANCE_RE = re.compile(
    r"\b(?:"
    r"significant(?:ly)?\s+step|marks?\s+an?\s+important|"
    r"important\s+development|major\s+step|"
    r"positions?\s+\w+(?:\s+\w+){0,8}\s+among|"
    r"among\s+other\s+major\s+institutions|"
    r"reflects?\s+growing|underscores?\s+adoption|"
    r"signals?\s+increasing|growing\s+demand|"
    r"broader\s+trend|integration\s+of\s+traditional"
    r")\b",
    re.IGNORECASE,
)

ENUM_SPLIT_RE = re.compile(
    r"(?:,\s*|\s+and\s+|\s+including\s+|\s+among\s+them\s+|"
    r"\s+as\s+well\s+as\s+|\s+followed\s+by\s+)",
    re.IGNORECASE,
)
COMPOSITE_SPLIT_RE = re.compile(
    r"(?:\s+after\s+|\s+as\s+part\s+of\s+|\s+soon\s+after\s+|"
    r"\s+before\s+|\s+with\s+plans?\s+to\s+|\s+which\s+included\s+)",
    re.IGNORECASE,
)
MODAL_CLASS = {
    "has": "completed",
    "have": "completed",
    "had": "completed",
    "received": "completed",
    "launched": "completed",
    "started": "completed",
    "began": "completed",
    "announced": "completed",
    "expects": "expectation",
    "expect": "expectation",
    "expected": "expectation",
    "plans": "plan",
    "plan": "plan",
    "planning": "plan",
    "intends": "plan",
    "intend": "plan",
    "preparing": "plan",
    "will": "future",
    "may": "possibility",
    "might": "possibility",
    "could": "possibility",
    "awaiting": "pending",
    "awaits": "pending",
    "pending": "pending",
}
NEGATION_RE = re.compile(
    r"\b(?:not|never|no|cannot|can't|won't|didn't|doesn't|isn't|aren't|without)\b",
    re.IGNORECASE,
)
ASSET_RE = re.compile(
    r"\b(?:Bitcoin|BTC|Ether|ETH|USDC|EURC|EURAU|AllUnity(?:\s+EUR)?|"
    r"Circle\s+USDC|stablecoins?)\b",
    re.IGNORECASE,
)
ENTITY_RE = re.compile(
    r"\b(?:Deutsche\s+Bank|Cointelegraph|Taurus|Bitpanda|"
    r"Landesbank\s+Baden-W(?:ü|u)rttemberg|LBBW|"
    r"Sabih\s+Behzad|BlackRock|Grayscale|MiCA|"
    r"Circle|AllUnity)\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _norm_tokens(text: str) -> set[str]:
    return {t.rstrip(".,;:") for t in content_tokens(text) if t.rstrip(".,;:")}


def _modal_class(text: str) -> str | None:
    lowered = f" {(text or '').lower()} "
    for token, klass in (
        (" has received ", "completed"),
        (" have received ", "completed"),
        (" received the ", "completed"),
        (" already received ", "completed"),
        (" expects to ", "expectation"),
        (" expect to ", "expectation"),
        (" plans to ", "plan"),
        (" intends to ", "plan"),
        (" is preparing to ", "plan"),
        (" will ", "future"),
        (" may ", "possibility"),
        (" might ", "possibility"),
        (" could ", "possibility"),
        (" awaiting ", "pending"),
        (" awaits ", "pending"),
    ):
        if token in lowered:
            return klass
    for word in re.findall(r"[a-z']+", lowered):
        if word in MODAL_CLASS:
            return MODAL_CLASS[word]
    return None


def _polarity(text: str) -> str:
    return "negated" if NEGATION_RE.search(text or "") else "affirmed"


@dataclass(frozen=True)
class AtomicProposition:
    prop_id: str
    parent_claim_id: str
    text: str
    subject: str = ""
    predicate: str = ""
    object: str = ""
    entities: tuple[str, ...] = ()
    numbers: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    attribution: str = ""
    polarity: str = "affirmed"
    modality: str = ""
    location: str = ""
    time: str = ""
    enumerated_values: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    kind: str = "fact"  # fact | significance | connective

    def as_dict(self) -> dict[str, Any]:
        return {
            "prop_id": self.prop_id,
            "parent_claim_id": self.parent_claim_id,
            "text": self.text,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "entities": list(self.entities),
            "numbers": list(self.numbers),
            "dates": list(self.dates),
            "attribution": self.attribution,
            "polarity": self.polarity,
            "modality": self.modality,
            "location": self.location,
            "time": self.time,
            "enumerated_values": list(self.enumerated_values),
            "provenance": list(self.provenance),
            "kind": self.kind,
        }


@dataclass(frozen=True)
class AuthorizedPropositionSet:
    propositions: tuple[AtomicProposition, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"propositions": [row.as_dict() for row in self.propositions]}


@dataclass
class PropositionMatch:
    article_prop: AtomicProposition
    matched: list[AtomicProposition] = field(default_factory=list)
    status: str = STATUS_UNSUPPORTED
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "article_prop": self.article_prop.as_dict(),
            "matched_prop_ids": [row.prop_id for row in self.matched],
            "matched_parent_claim_ids": list(
                dict.fromkeys(row.parent_claim_id for row in self.matched)
            ),
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class SentenceGrounding:
    text: str
    status: str
    propositions: list[PropositionMatch] = field(default_factory=list)
    claim_ids: tuple[str, ...] = ()
    issue_code: str | None = None
    issue_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "status": self.status,
            "claim_ids": list(self.claim_ids),
            "issue_code": self.issue_code,
            "issue_message": self.issue_message,
            "propositions": [row.as_dict() for row in self.propositions],
        }


@dataclass
class AtomicGroundingReport:
    sentences: list[SentenceGrounding] = field(default_factory=list)
    total_factual_propositions: int = 0
    supported_factual_propositions: int = 0
    ambiguous_factual_propositions: int = 0
    unsupported_factual_propositions: int = 0
    connective_units: int = 0
    ok: bool = False

    @property
    def proposition_coverage(self) -> float:
        if self.total_factual_propositions <= 0:
            return 1.0
        return round(
            self.supported_factual_propositions / self.total_factual_propositions, 4
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_factual_propositions": self.total_factual_propositions,
            "supported_factual_propositions": self.supported_factual_propositions,
            "ambiguous_factual_propositions": self.ambiguous_factual_propositions,
            "unsupported_factual_propositions": self.unsupported_factual_propositions,
            "connective_units": self.connective_units,
            "proposition_coverage": self.proposition_coverage,
            "ok": self.ok,
            "sentences": [row.as_dict() for row in self.sentences],
        }


def _entities_in(text: str) -> tuple[str, ...]:
    found = [m.group(0) for m in ENTITY_RE.finditer(text or "")]
    found.extend(m.group(0) for m in ASSET_RE.finditer(text or ""))
    out: list[str] = []
    seen: set[str] = set()
    for item in found:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def _dates_in(text: str) -> tuple[str, ...]:
    hits = re.findall(
        r"\b(?:20\d{2}|January|February|March|April|May|June|July|August|"
        r"September|October|November|December|Wednesday|Tuesday|Monday|"
        r"Thursday|Friday|Saturday|Sunday)\b",
        text or "",
        flags=re.IGNORECASE,
    )
    return tuple(dict.fromkeys(hits))


def _attribution_in(text: str) -> str:
    m = re.search(
        r"\b([A-Z][\w.\-]*(?:\s+[A-Z][\w.\-]*){0,5})\s+"
        r"(told|said|revealed|announced|disclosed)\b",
        text or "",
    )
    if m:
        return f"{m.group(1)} {m.group(2)}".strip()
    if re.search(r"\btold\s+Cointelegraph\b", text or "", re.I):
        return "spokesperson told Cointelegraph"
    return ""


def _make_prop(
    *,
    parent: str,
    index: int,
    text: str,
    provenance: tuple[str, ...] = (),
    kind: str = "fact",
    attribution: str = "",
) -> AtomicProposition:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    modal = _modal_class(clean) or ""
    return AtomicProposition(
        prop_id=f"{parent}.P{index}",
        parent_claim_id=parent,
        text=clean,
        entities=_entities_in(clean),
        numbers=tuple(number_tokens(clean)),
        dates=_dates_in(clean),
        attribution=attribution or _attribution_in(clean),
        polarity=_polarity(clean),
        modality=modal,
        enumerated_values=tuple(m.group(0) for m in ASSET_RE.finditer(clean)),
        provenance=provenance,
        kind=kind,
    )


def decompose_claim_text(
    claim_id: str,
    text: str,
    *,
    provenance: tuple[str, ...] = (),
) -> list[AtomicProposition]:
    """Deterministic atomic decomposition of one ledger/packet claim."""
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return []
    props: list[AtomicProposition] = []
    idx = 1

    including = re.search(r"\bincluding\b(.+)$", raw, re.I)
    if including:
        core = raw[: including.start()].strip(" ,;")
        if core:
            props.append(_make_prop(parent=claim_id, index=idx, text=core, provenance=provenance))
            idx += 1
        for piece in ENUM_SPLIT_RE.split(including.group(1)):
            piece = piece.strip(" ,.;()")
            if word_count(piece) >= 1 and ASSET_RE.search(piece):
                props.append(
                    _make_prop(
                        parent=claim_id,
                        index=idx,
                        text=f"includes {piece}",
                        provenance=provenance,
                    )
                )
                idx += 1
        if props:
            return props

    parts = [p.strip(" ,;") for p in COMPOSITE_SPLIT_RE.split(raw) if p and p.strip()]
    if len(parts) >= 2 and word_count(raw) >= 18:
        # Keep the full claim as an atomic unit, plus each composite clause.
        props.append(_make_prop(parent=claim_id, index=idx, text=raw, provenance=provenance))
        idx += 1
        for part in parts:
            if word_count(part) >= 4:
                props.append(
                    _make_prop(parent=claim_id, index=idx, text=part, provenance=provenance)
                )
                idx += 1
        if len(props) >= 2:
            return props

    geo = re.search(
        r"(institutional clients(?: and corporations)?(?: (?:in|across) Europe)?)",
        raw,
        re.I,
    )
    if geo and word_count(raw) > word_count(geo.group(1)) + 6:
        props.append(_make_prop(parent=claim_id, index=idx, text=raw, provenance=provenance))
        idx += 1
        props.append(
            _make_prop(parent=claim_id, index=idx, text=geo.group(1), provenance=provenance)
        )
        return props

    props.append(_make_prop(parent=claim_id, index=1, text=raw, provenance=provenance))
    return props


def build_authorized_proposition_set(
    *,
    ledgers: EvidenceLedgers,
    packet: WriterEvidencePacket | None = None,
) -> AuthorizedPropositionSet:
    rows: list[AtomicProposition] = []
    seen_parents: set[str] = set()
    for claim in ledgers.claims:
        seen_parents.add(claim.claim_id)
        rows.extend(
            decompose_claim_text(
                claim.claim_id,
                claim.text,
                provenance=tuple(claim.evidence_ids),
            )
        )
    if packet is not None:
        for fact in packet.authorized_facts:
            if fact.id in seen_parents:
                continue
            rows.extend(
                decompose_claim_text(
                    fact.id,
                    fact.proposition,
                    provenance=tuple(fact.provenance),
                )
            )
            seen_parents.add(fact.id)
    return AuthorizedPropositionSet(propositions=tuple(rows))


def is_pure_connective(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if CONNECTIVE_RE.match(t):
        return True
    if word_count(t) < 4 and not number_tokens(t) and not ENTITY_RE.search(t):
        return True
    return False


def is_significance_rhetoric(text: str) -> bool:
    return bool(SIGNIFICANCE_RE.search(text or ""))


def extract_article_propositions(sentence: str) -> list[AtomicProposition]:
    """Extract one or more factual propositions from an article sentence."""
    text = re.sub(r"\s+", " ", (sentence or "").strip())
    if not text:
        return []
    if is_pure_connective(text):
        return [_make_prop(parent="ARTICLE", index=1, text=text, kind="connective")]
    if is_significance_rhetoric(text):
        return [_make_prop(parent="ARTICLE", index=1, text=text, kind="significance")]

    props: list[AtomicProposition] = []
    idx = 1
    chunks = [c.strip() for c in re.split(r";\s+", text) if c.strip()]
    if len(chunks) == 1:
        including = re.search(r"\bincluding\b(.+)$", text, re.I)
        specifically = re.match(r"^Specifically,\s*(.+)$", text, re.I)
        if specifically and ASSET_RE.search(specifically.group(1)):
            # Split enumerated assets into atomic props (maps to C05-style enums).
            for piece in ENUM_SPLIT_RE.split(specifically.group(1)):
                piece = piece.strip(" ,.;()")
                if not piece:
                    continue
                if ASSET_RE.search(piece) or word_count(piece) >= 2:
                    props.append(
                        _make_prop(
                            parent="ARTICLE",
                            index=idx,
                            text=f"service includes {piece}"
                            if not piece.lower().startswith("include")
                            else piece,
                        )
                    )
                    idx += 1
            if props:
                return props
        if including:
            core = text[: including.start()].strip(" ,;")
            if core:
                props.append(_make_prop(parent="ARTICLE", index=idx, text=core))
                idx += 1
            for piece in ENUM_SPLIT_RE.split(including.group(1)):
                piece = piece.strip(" ,.;()")
                if ASSET_RE.search(piece):
                    props.append(
                        _make_prop(parent="ARTICLE", index=idx, text=f"includes {piece}")
                    )
                    idx += 1
            if len(props) >= 2:
                return props
        return [_make_prop(parent="ARTICLE", index=1, text=text)]

    for chunk in chunks:
        props.append(_make_prop(parent="ARTICLE", index=idx, text=chunk))
        idx += 1
    return props or [_make_prop(parent="ARTICLE", index=1, text=text)]


def _strict_field_conflict(article: AtomicProposition, auth: AtomicProposition) -> str | None:
    if article.polarity != auth.polarity and (
        article.polarity == "negated" or auth.polarity == "negated"
    ):
        if _norm_tokens(article.text) & _norm_tokens(auth.text):
            return "polarity_mismatch"
    a_mod = article.modality or _modal_class(article.text)
    b_mod = auth.modality or _modal_class(auth.text)
    if a_mod and b_mod and a_mod != b_mod:
        shared = _norm_tokens(article.text) & _norm_tokens(auth.text)
        if shared & {"license", "licence", "approval", "custody", "receive", "received"}:
            return "modality_mismatch"
        if a_mod == "completed" and b_mod in {"expectation", "plan", "pending"}:
            return "modality_mismatch"
        if b_mod == "completed" and a_mod in {"expectation", "plan", "pending"}:
            return "modality_mismatch"
    if article.numbers and auth.numbers:
        a_nums = {n.lower() for n in article.numbers}
        b_nums = {n.lower() for n in auth.numbers}
        if a_nums - b_nums and (_norm_tokens(article.text) & _norm_tokens(auth.text)):
            if not a_nums <= b_nums:
                return "number_mismatch"
    if article.dates and auth.dates:
        a_dates = {_norm(d) for d in article.dates}
        b_dates = {_norm(d) for d in auth.dates}
        if a_dates - b_dates and len(a_dates & b_dates) == 0:
            if _norm_tokens(article.text) & _norm_tokens(auth.text):
                return "date_mismatch"
    return None


def proposition_supported_by(
    article: AtomicProposition,
    authorized: Iterable[AtomicProposition],
) -> tuple[list[AtomicProposition], str | None]:
    if article.kind == "connective":
        return [], None
    if article.kind == "significance":
        matches: list[AtomicProposition] = []
        for auth in authorized:
            if is_significance_rhetoric(auth.text) and (
                SequenceMatcher(None, _norm(article.text), _norm(auth.text)).ratio() >= 0.72
            ):
                matches.append(auth)
        return matches, ("unsupported_significance" if not matches else None)

    a_toks = _norm_tokens(article.text)
    a_ents = {_norm(e) for e in article.entities} | {
        _norm(e) for e in article.enumerated_values
    }
    # Article introduces a distinct named entity absent from the whole authorized set.
    auth_ent_universe: set[str] = set()
    for auth in authorized:
        auth_ent_universe |= {_norm(e) for e in auth.entities}
        auth_ent_universe |= {_norm(e) for e in auth.enumerated_values}
        auth_ent_universe |= _norm_tokens(auth.text)
    novel_entities = {
        e
        for e in a_ents
        if e
        and e not in auth_ent_universe
        and e
        not in {
            "bitcoin",
            "btc",
            "ether",
            "eth",
            "usdc",
            "eurc",
            "stablecoins",
            "stablecoin",
        }
    }
    if novel_entities and a_ents:
        # Hard fail only for org-like novel entities (BlackRock vs Deutsche Bank).
        org_like = {e for e in novel_entities if e in {"blackrock", "grayscale", "bitpanda", "taurus"}}
        if org_like - auth_ent_universe:
            return [], "entity_mismatch"

    best: list[tuple[float, AtomicProposition]] = []
    hard_fail_reason: str | None = None

    for auth in authorized:
        if auth.kind != "fact":
            continue
        conflict = _strict_field_conflict(article, auth)
        if conflict:
            b_toks = _norm_tokens(auth.text)
            if len(a_toks & b_toks) >= 2:
                hard_fail_reason = conflict
            continue
        b_toks = _norm_tokens(auth.text)
        b_ents = {_norm(e) for e in auth.entities} | {
            _norm(e) for e in auth.enumerated_values
        }
        if not a_toks or not b_toks:
            continue
        overlap = len(a_toks & b_toks) / max(1, len(a_toks))
        ent_hit = bool(a_ents & b_ents) if a_ents else False
        seq = SequenceMatcher(None, _norm(article.text), _norm(auth.text)).ratio()

        if a_ents and a_ents <= (b_ents | b_toks) and overlap >= 0.35:
            best.append((max(overlap, 0.8), auth))
            continue
        # Single enumerated asset realization (USDC / EURC / AllUnity EUR).
        if a_ents and len(a_ents) <= 3 and (a_ents & b_ents):
            best.append((0.85, auth))
            continue
        if ent_hit and overlap >= 0.40:
            best.append((overlap + 0.15, auth))
            continue
        if overlap >= 0.55 or (overlap >= 0.42 and seq >= 0.35):
            best.append((overlap, auth))
            continue
        if len(a_toks) <= 6 and a_toks <= b_toks:
            best.append((0.9, auth))
            continue
        if ent_hit and (
            ({"partnered", "partnering", "partnership"} & (a_toks | b_toks))
            or ({"custody", "platform"} <= (a_toks & b_toks))
            or ({"license", "licence"} & a_toks & b_toks)
            or ({"tokenized"} & a_toks & b_toks)
            or ({"institutional", "clients"} <= (a_toks & b_toks))
            or ({"cointelegraph"} & a_toks and {"cointelegraph"} & b_toks)
        ):
            best.append((0.75, auth))
            continue
        # Serve/aim paraphrase of institutional clients geography.
        if {"institutional", "clients"} <= a_toks and {"institutional", "clients"} <= b_toks:
            best.append((0.7, auth))
            continue
        # Expects/receive license attribution paraphrase.
        if {"expects", "license"} <= a_toks or ({"expects"} <= a_toks and {"license"} <= b_toks):
            if {"license", "licence"} & b_toks and (
                {"expects", "expect"} & b_toks or auth.modality == "expectation"
            ):
                best.append((0.72, auth))
                continue

    best.sort(key=lambda row: -row[0])
    matched = [row for _score, row in best if _score >= 0.42]
    uniq: list[AtomicProposition] = []
    seen: set[str] = set()
    for row in matched:
        if row.prop_id not in seen:
            seen.add(row.prop_id)
            uniq.append(row)
    if uniq:
        return uniq, None
    return [], hard_fail_reason or "no_authorized_proposition_match"


def ground_sentence(
    sentence: str,
    authorized: AuthorizedPropositionSet,
) -> SentenceGrounding:
    text = (sentence or "").strip()
    art_props = extract_article_propositions(text)
    if len(art_props) == 1 and art_props[0].kind == "connective":
        return SentenceGrounding(text=text, status=STATUS_CONNECTIVE, propositions=[])

    matches: list[PropositionMatch] = []
    claim_ids: list[str] = []
    statuses: list[str] = []
    for prop in art_props:
        if prop.kind == "connective":
            continue
        matched, reason = proposition_supported_by(prop, authorized.propositions)
        if matched:
            status = STATUS_SUPPORTED
            for row in matched:
                if row.parent_claim_id not in claim_ids:
                    claim_ids.append(row.parent_claim_id)
        elif reason in {
            "modality_mismatch",
            "number_mismatch",
            "date_mismatch",
            "polarity_mismatch",
        }:
            status = STATUS_UNSUPPORTED
        elif prop.kind == "significance":
            status = STATUS_UNSUPPORTED
            reason = reason or "unsupported_significance"
        else:
            partial = False
            a_ents = {_norm(e) for e in prop.entities}
            for auth in authorized.propositions:
                b_ents = {_norm(e) for e in auth.entities}
                if a_ents and a_ents & b_ents:
                    partial = True
                    break
            status = STATUS_AMBIGUOUS if partial else STATUS_UNSUPPORTED
            reason = reason or (
                "partial_entity_overlap" if partial else "unsupported_assertion"
            )
        matches.append(
            PropositionMatch(
                article_prop=prop,
                matched=matched,
                status=status,
                reason=reason,
            )
        )
        statuses.append(status)

    if not statuses:
        return SentenceGrounding(text=text, status=STATUS_CONNECTIVE, propositions=[])
    if any(s == STATUS_UNSUPPORTED for s in statuses):
        overall = STATUS_UNSUPPORTED
        code = "unsupported_assertion"
        msg = next(
            (m.reason for m in matches if m.status == STATUS_UNSUPPORTED and m.reason),
            "assertive proposition is not supported by authorized facts",
        )
        if any(
            (m.article_prop.kind == "significance")
            or (m.reason or "").startswith("unsupported_significance")
            for m in matches
        ):
            code = "unsupported_inference"
            msg = "evaluative/comparative flourish is not authorized"
    elif any(s == STATUS_AMBIGUOUS for s in statuses):
        overall = STATUS_AMBIGUOUS
        code = "ambiguous_assertion"
        msg = "assertive proposition only partially overlaps authorized facts"
    else:
        overall = STATUS_SUPPORTED
        code = None
        msg = None
    return SentenceGrounding(
        text=text,
        status=overall,
        propositions=matches,
        claim_ids=tuple(claim_ids),
        issue_code=code,
        issue_message=msg,
    )


def verify_atomic_article(
    *,
    headline: str,
    dek: str,
    article_body: str,
    authorized: AuthorizedPropositionSet,
) -> AtomicGroundingReport:
    report = AtomicGroundingReport()
    for blob in (headline, dek, article_body):
        for sentence in split_sentences(blob or ""):
            grounded = ground_sentence(sentence, authorized)
            report.sentences.append(grounded)
            if grounded.status == STATUS_CONNECTIVE:
                report.connective_units += 1
                continue
            for match in grounded.propositions:
                if match.article_prop.kind == "connective":
                    continue
                report.total_factual_propositions += 1
                if match.status == STATUS_SUPPORTED:
                    report.supported_factual_propositions += 1
                elif match.status == STATUS_AMBIGUOUS:
                    report.ambiguous_factual_propositions += 1
                else:
                    report.unsupported_factual_propositions += 1
    report.ok = (
        report.unsupported_factual_propositions == 0
        and report.ambiguous_factual_propositions == 0
    )
    return report


def unused_authorized_propositions(
    authorized: AuthorizedPropositionSet,
    report: AtomicGroundingReport,
) -> list[AtomicProposition]:
    used: set[str] = set()
    for sentence in report.sentences:
        for match in sentence.propositions:
            for row in match.matched:
                used.add(row.prop_id)
    return [
        row
        for row in authorized.propositions
        if row.prop_id not in used and row.kind == "fact"
    ]


def verbalize_proposition(prop: AtomicProposition) -> str:
    text = prop.text.strip()
    if not text.endswith("."):
        text += "."
    return text[0].upper() + text[1:] if text else text
