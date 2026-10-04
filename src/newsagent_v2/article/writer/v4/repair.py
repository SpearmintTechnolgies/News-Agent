"""V4 targeted repair. Surgical. Max 2 rounds. No full regeneration by default."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, check_similarity
from newsagent_v2.article.qa.textutil import number_tokens, split_sentences, word_count, words
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.v4.packet import (
    WriterEvidencePacket,
    _is_boilerplate_proposition as _packet_is_boilerplate_proposition,
)
from newsagent_v2.article.writer.v4.verify import (
    STATUS_AMBIGUOUS,
    STATUS_QUOTE_BAD,
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    VerificationReport,
    verify_v4_native,
)
from newsagent_v2.article.writer.v4.writer import V4NativeArticle, V4NaturalProseWriter

MAX_REPAIR_ROUNDS = 2

# Closing / body paragraphs this short after repair are treated as destructive collapse.
MIN_COHERENT_PARAGRAPH_WORDS = 25

_PARTICIPLE_OPENER_RE = re.compile(
    r"^(?:Falling|Dropping|Rising|Marking|Building|Holding|Including|Among|"
    r"According|Due|Plus|Also|And|But|Or|With|While|After|Before|During|"
    r"Attracting|Revealing|Showing|Highlighting|Underscoring|Reflecting|"
    r"Creating|Combining|Tracking|Establishing|Demonstrating)\b",
    re.IGNORECASE,
)
_BOILERPLATE_RE = re.compile(
    r"(?:editorial\s+policy|aims\s+to\s+provide\s+accurate|"
    r"this\s+news\s+article\s+is\s+produced|"
    r"subscribe|newsletter|cookie\s+policy|terms\s+of\s+service)",
    re.IGNORECASE,
)
_FINITE_VERB_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|will|would|can|could|may|might|"
    r"did|does|do|announced|declined|fell|lost|shed|traded|recorded|"
    r"failed|ended|shifted|followed|said|reported|closed|opened)\b",
    re.IGNORECASE,
)


def is_sentence_fragment(text: str) -> bool:
    """Deterministic fragment detector for repair integrity / rollback."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return True
    wc = word_count(t)
    if wc < 5:
        return True
    if re.match(r"^[a-z]", t):
        return True
    if re.match(r"^\d+\b", t) and not re.match(r"^\d[\d,.]*\s*%?\s+(?:of|in|on|to)\b", t, re.I):
        # e.g. "4 high, with stellar down..." — not a newsroom sentence
        return True
    if t.endswith((",", ";", ":", "—", "-")):
        return True
    if re.search(r"\b(?:and|but|or|with|to|of|for)\s*$", t, re.I):
        return True
    if _PARTICIPLE_OPENER_RE.match(t) and not _FINITE_VERB_RE.search(t):
        return True
    # Bare clause without terminal punctuation and without a clear verb.
    if not t.endswith((".", "!", "?")) and not _FINITE_VERB_RE.search(t) and wc < 12:
        return True
    return False


def is_boilerplate_proposition(text: str) -> bool:
    # Keep legacy editorial-policy patterns and shared chrome/byline filter.
    return bool(_BOILERPLATE_RE.search(text or "")) or _packet_is_boilerplate_proposition(
        text or ""
    )


def is_complete_newsroom_sentence(text: str) -> bool:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t or is_sentence_fragment(t) or is_boilerplate_proposition(t):
        return False
    if not t.endswith((".", "!", "?")):
        return False
    if not _FINITE_VERB_RE.search(t) and word_count(t) < 10:
        return False
    return True


def newsroom_sentence_from_authorized(text: str) -> str | None:
    """Turn authorized proposition text into a grammatical sentence, or reject."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    if not t.endswith((".", "!", "?")):
        t = t.rstrip(" ,;:") + "."
    if t and t[0].islower():
        t = t[0].upper() + t[1:]
    if not is_complete_newsroom_sentence(t):
        return None
    return t


def paragraph_integrity_ok(body: str, *, prior_body: str | None = None) -> bool:
    """Reject bodies that contain *new* fragments or collapsed paragraphs after repair/trim."""
    from newsagent_v2.article.qa.textutil import split_paragraphs

    paras = [p for p in split_paragraphs(body or "") if p.strip()]
    prior_paras = (
        [p for p in split_paragraphs(prior_body or "") if p.strip()] if prior_body else []
    )
    prior_sentences = set(split_sentences(prior_body or "")) if prior_body else set()
    for sentence in split_sentences(body or ""):
        if not sentence.strip() or not is_sentence_fragment(sentence):
            continue
        # Pre-existing wording from prior body is not a repair/trim regression.
        if prior_body and (sentence in prior_sentences or sentence in prior_body):
            continue
        return False
    for idx, para in enumerate(paras):
        wc = word_count(para)
        if is_sentence_fragment(para) and not (
            prior_body and (para in prior_body or para.strip() in {p.strip() for p in prior_paras})
        ):
            return False
        if not prior_body:
            continue
        prior_wc = word_count(prior_paras[idx]) if idx < len(prior_paras) else 0
        if prior_wc >= MIN_COHERENT_PARAGRAPH_WORDS and wc < MIN_COHERENT_PARAGRAPH_WORDS:
            return False
        if prior_wc >= 40 and wc < 15:
            return False
        if idx == len(paras) - 1 and prior_paras:
            p_last = word_count(prior_paras[-1])
            if p_last >= MIN_COHERENT_PARAGRAPH_WORDS and wc < 15:
                return False
    return True


def soft_trim_article_body(
    body: str,
    *,
    target_max: int = 550,
    prefer_max: int = 520,
    prefer_min: int = 480,
    target_min: int = 450,
) -> tuple[str, dict[str, Any]]:
    """Trim excess length while preserving paragraph structure and complete sentences.

    Prefer landing in prefer_min–prefer_max when possible. Never collapse a paragraph
    into a fragment. Does not invent content.
    """
    from newsagent_v2.article.qa.textutil import split_paragraphs

    del prefer_min  # documented target; prefer_max/target_max drive the loop
    original = (body or "").strip()
    meta: dict[str, Any] = {
        "pre_words": word_count(original),
        "removed_sentences": [],
        "rolled_back_trims": 0,
    }
    if word_count(original) <= target_max:
        meta["post_words"] = word_count(original)
        meta["trimmed"] = False
        return original, meta

    paras = [p.strip() for p in split_paragraphs(original) if p.strip()]
    if not paras:
        paras = [original]

    para_sents: list[list[str]] = [
        [s for s in split_sentences(p) if s.strip()] for p in paras
    ]
    if not any(para_sents):
        meta["post_words"] = word_count(original)
        meta["trimmed"] = False
        return original, meta

    def _join() -> str:
        blocks = [" ".join(sents).strip() for sents in para_sents if sents]
        return "\n\n".join(blocks).strip()

    def _current_words() -> int:
        return word_count(_join())

    def _try_pop(p_idx: int) -> bool:
        nonlocal para_sents
        if len(para_sents[p_idx]) <= 1:
            return False
        candidate = [list(s) for s in para_sents]
        removed = candidate[p_idx].pop()
        trial_paras = [" ".join(s).strip() for s in candidate if s]
        trial_body = "\n\n".join(trial_paras).strip()
        if word_count(trial_body) < target_min:
            return False
        if p_idx < len(trial_paras) and word_count(trial_paras[p_idx]) < 20:
            meta["rolled_back_trims"] += 1
            return False
        if not paragraph_integrity_ok(trial_body, prior_body=original):
            meta["rolled_back_trims"] += 1
            return False
        para_sents = candidate
        meta["removed_sentences"].append(removed[:160])
        return True

    goal = prefer_max if word_count(original) > prefer_max else target_max
    for p_idx in range(len(para_sents) - 1, -1, -1):
        guard = 0
        while _current_words() > goal and guard < 40:
            guard += 1
            if not _try_pop(p_idx):
                break
        if _current_words() <= goal:
            break

    if _current_words() > target_max:
        for p_idx in range(len(para_sents) - 1, -1, -1):
            guard = 0
            while _current_words() > target_max and guard < 40:
                guard += 1
                if not _try_pop(p_idx):
                    break
            if _current_words() <= target_max:
                break

    out = _join()
    if target_min <= word_count(out) <= target_max and paragraph_integrity_ok(
        out, prior_body=original
    ):
        meta["post_words"] = word_count(out)
        meta["trimmed"] = out != original
        return out, meta

    # Barely-over last resort: drop one trailing complete sentence.
    if word_count(original) <= target_max + 40:
        sents = [s for s in split_sentences(original) if s.strip()]
        if len(sents) > 3:
            trial = " ".join(sents[:-1]).strip()
            if "\n\n" in original:
                blocks = [p.strip() for p in split_paragraphs(original) if p.strip()]
                if blocks:
                    last_sents = [s for s in split_sentences(blocks[-1]) if s.strip()]
                    if len(last_sents) > 1:
                        blocks[-1] = " ".join(last_sents[:-1]).strip()
                        trial = "\n\n".join(blocks).strip()
            if target_min <= word_count(trial) <= target_max:
                meta["removed_sentences"].append(sents[-1][:160])
                meta["post_words"] = word_count(trial)
                meta["trimmed"] = True
                meta["last_resort_sentence_drop"] = True
                return trial, meta

    meta["rolled_back_trims"] += 1
    meta["post_words"] = word_count(original)
    meta["trimmed"] = False
    meta["reason"] = "integrity_rollback_to_original"
    return original, meta


@dataclass
class RepairAction:
    kind: str
    detail: str
    before: str = ""
    after: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "detail": self.detail,
            "before": self.before[:300],
            "after": self.after[:300],
        }


@dataclass
class RepairLog:
    actions: list[RepairAction] = field(default_factory=list)
    rounds: int = 0
    model_calls: int = 0
    copyright_recovery: dict[str, Any] = field(default_factory=dict)
    depth_loss_origin: str | None = None
    copyright_rejected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "rounds": self.rounds,
            "model_calls": self.model_calls,
            "actions": [row.as_dict() for row in self.actions],
            "copyright_recovery": dict(self.copyright_recovery),
            "depth_loss_origin": self.depth_loss_origin,
            "copyright_rejected": self.copyright_rejected,
        }


def _central_headline(packet: WriterEvidencePacket) -> str:
    if not packet.authorized_facts:
        return packet.story_topic or "Market update"
    prop = packet.authorized_facts[0].proposition.strip().rstrip(".")
    # Drop bare unsupported-looking numbers by keeping first clause words.
    tokens = prop.split()
    cleaned: list[str] = []
    for token in tokens[:14]:
        if re.fullmatch(r"[$£€]?\d[\d,.%]*", token):
            # Keep number only if authorized on that fact.
            if token.lower() in {n.lower() for n in packet.authorized_facts[0].numbers}:
                cleaned.append(token)
            continue
        cleaned.append(token)
    text = " ".join(cleaned).strip(" ,;:")
    return text[:110] if text else (packet.story_topic or "Market update")


def repair_unsupported_headline_number(
    native: V4NativeArticle,
    packet: WriterEvidencePacket,
) -> tuple[V4NativeArticle, RepairAction | None]:
    allowed = {n.lower() for fact in packet.authorized_facts for n in fact.numbers}
    for fact in packet.authorized_facts:
        allowed |= {t.lower() for t in number_tokens(fact.proposition)}
    bad = [token for token in number_tokens(native.headline) if token.lower() not in allowed]
    if not bad:
        return native, None
    repaired = deepcopy(native)
    before = repaired.headline
    repaired.headline = _central_headline(packet)
    if not repaired.seo_title or any(token in repaired.seo_title for token in bad):
        repaired.seo_title = repaired.headline[:70]
    return repaired, RepairAction(
        kind="headline_number_repair",
        detail=f"removed unsupported headline number(s): {', '.join(bad)}",
        before=before,
        after=repaired.headline,
    )


def _drop_sentences(body: str, drop: set[str]) -> str:
    kept = [sentence for sentence in split_sentences(body) if sentence not in drop]
    return " ".join(kept).strip()


def repair_drop_unsupported_sentences(
    native: V4NativeArticle,
    report: VerificationReport,
) -> tuple[V4NativeArticle, list[RepairAction]]:
    drop = {
        row.text
        for row in report.rows
        if row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS, STATUS_QUOTE_BAD}
        and row.text in (native.article_body or "")
    }
    if not drop:
        return native, []
    repaired = deepcopy(native)
    before = repaired.article_body
    repaired.article_body = _drop_sentences(before, drop)
    actions = [
        RepairAction(
            kind="drop_unsupported_sentence",
            detail="removed unsupported/ambiguous sentence",
            before=item,
            after="",
        )
        for item in drop
    ]
    return repaired, actions


def find_copyright_offending_sentences(
    native: V4NativeArticle,
    article_input: dict[str, Any],
) -> list[str]:
    from newsagent_v2.article.input import evidence_text_blobs
    from newsagent_v2.article.qa.similarity import HIGH_SENTENCE_SIMILARITY

    article = {
        "event_id": "tmp",
        "headline": native.headline,
        "dek": native.dek,
        "article_body": native.article_body,
        "claims": [],
        "quotes": [],
    }
    issues, metrics = check_similarity(article, article_input)
    del metrics
    codes = {item.get("code") for item in issues}
    if not ({"exact_phrase_overlap", "high_sentence_similarity"} & codes):
        return []
    blobs = evidence_text_blobs(article_input)
    source_token_lists = [words(blob) for blob in blobs if blob.strip()]
    source_grams: set[tuple[str, ...]] = set()
    for tokens in source_token_lists:
        for index in range(0, max(0, len(tokens) - EXACT_PHRASE_N + 1)):
            source_grams.add(tuple(tokens[index : index + EXACT_PHRASE_N]))
    offenders: list[str] = []
    for sentence in split_sentences(native.article_body):
        sent_tokens = words(sentence)
        if len(sent_tokens) < EXACT_PHRASE_N:
            continue
        has_exact = any(
            tuple(sent_tokens[i : i + EXACT_PHRASE_N]) in source_grams
            for i in range(0, max(0, len(sent_tokens) - EXACT_PHRASE_N + 1))
        )
        high_sim = False
        lowered = sentence.lower()
        for blob in blobs:
            # Compare against source sentences, not giant blobs only.
            for source_sentence in split_sentences(blob):
                if len(words(source_sentence)) < EXACT_PHRASE_N:
                    continue
                if SequenceMatcher(None, lowered, source_sentence.lower()).ratio() >= HIGH_SENTENCE_SIMILARITY:
                    high_sim = True
                    break
            if high_sim:
                break
        if has_exact or high_sim:
            offenders.append(sentence)
    return offenders


REPAIR_LENGTH_RETENTION_ADVISORY = 0.60

# Bounded alternate structural realizations per flagged sentence.
MAX_STRUCTURAL_REWRITE_ATTEMPTS = 3

# Structural openings — change information order; they are not synonym glosses of the source.
_STRUCTURAL_LEAD_INS: tuple[str, ...] = (
    "",
    "Separately, ",
    "In related disclosures, ",
    "On the figures released, ",
    "As reported in the authorized record, ",
)


def _normalize_core_proposition(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    cleaned = cleaned.rstrip(".")
    if cleaned:
        cleaned = cleaned[0].lower() + cleaned[1:]
    return cleaned


def _shares_exact_ngram(left: str, right: str, n: int = EXACT_PHRASE_N) -> bool:
    a = words(left)
    b = words(right)
    if len(a) < n or len(b) < n:
        return False
    grams = {tuple(a[i : i + n]) for i in range(0, len(a) - n + 1)}
    return any(tuple(b[i : i + n]) in grams for i in range(0, len(b) - n + 1))


def _sentence_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left.lower(), right.lower()).ratio()


def _locked_fact_tokens(*texts: str) -> set[str]:
    """Names/numbers/dates/assets that must survive reconstruction."""
    locked: set[str] = set()
    stop = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "from",
        "by",
        "as",
        "at",
        "data",
        "shows",
        "attention",
        "now",
        "which",
        "that",
        "this",
        "its",
        "was",
        "were",
        "been",
        "being",
        "having",
        "according",
        "reported",
        "disclosed",
        "announced",
    }
    for text in texts:
        locked.update(tok.lower() for tok in number_tokens(text))
        for match in re.finditer(
            r"\b(?:Bitcoin|BTC|Ether|ETH|USDC|EURC|FBTC|GBTC|ARKB|BITB|"
            r"BlackRock|Fidelity|Grayscale|Federal Reserve|CoinDesk|"
            r"Senate|Congress|iShares)\b",
            text or "",
            re.I,
        ):
            locked.add(match.group(0).lower())
        for match in re.finditer(r"\b[A-Z]{2,5}\b", text or ""):
            locked.add(match.group(0).lower())
        for match in re.finditer(r"\b(?:[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text or ""):
            phrase = match.group(0).lower()
            if phrase.split()[0] not in stop:
                locked.add(phrase)
    return {tok for tok in locked if tok and tok not in stop and len(tok) > 1}


def _preserves_locked_tokens(candidate: str, locked: set[str]) -> bool:
    if not locked:
        return True
    hay = (candidate or "").lower()
    cand_nums = {n.lower() for n in number_tokens(candidate)}
    for token in locked:
        tok = token.lower()
        if any(ch.isdigit() for ch in tok):
            digits = re.sub(r"[^\d.]", "", tok)
            if digits and (digits in hay or any(digits in n for n in cand_nums)):
                continue
            if tok in hay or tok in cand_nums:
                continue
            return False
        # Entity/name tokens: require presence when length >= 3.
        if len(tok) >= 3 and tok not in hay:
            return False
    return True


def _matching_facts_for_sentence(sentence: str, packet: WriterEvidencePacket) -> list[Any]:
    sent_tokens = set(words(sentence))
    scored: list[tuple[int, Any]] = []
    for fact in packet.authorized_facts:
        score = len(sent_tokens & set(words(fact.proposition)))
        if score >= 3:
            scored.append((score, fact))
    scored.sort(key=lambda row: (-row[0], row[1].id))
    return [fact for _score, fact in scored[:4]]


def _authorized_atomic_cores(
    sentence: str,
    packet: WriterEvidencePacket,
    *,
    avoid_texts: tuple[str, ...] = (),
) -> list[str]:
    """
    Extract authorized atomic factual propositions for the flagged sentence.

    The offending generated sentence and raw source blobs are not used as
    rewrite templates. Authorized proposition text is the only factual input,
    and must still be structurally rebuilt before emission.
    """
    from newsagent_v2.article.writer.v4.atomic_grounding import decompose_claim_text

    facts = _matching_facts_for_sentence(sentence, packet)
    if not facts and packet.authorized_facts:
        facts = list(packet.authorized_facts[:2])

    cores: list[str] = []
    offender_norm = re.sub(r"\s+", " ", (sentence or "").strip().lower())

    for fact in facts:
        props = decompose_claim_text(fact.id, fact.proposition, provenance=tuple(fact.provenance))
        # Prefer clause atoms; always keep at least the parent proposition for rebuild.
        ordered = sorted(props, key=lambda p: word_count(p.text or ""))
        for prop in ordered:
            text = re.sub(r"\s+", " ", (prop.text or "").strip())
            if word_count(text) < 4:
                continue
            # Never treat the offending generated sentence itself as a core.
            if re.sub(r"\s+", " ", text.lower()) == offender_norm:
                continue
            if text not in cores:
                cores.append(text)
        prop_text = re.sub(r"\s+", " ", (fact.proposition or "").strip())
        if word_count(prop_text) >= 4 and prop_text not in cores:
            cores.append(prop_text)
    # avoid_texts (source blobs) are used later for collision checks, not as cores.
    del avoid_texts
    return cores[:6]


def _strip_report_prefix(text: str) -> str:
    cleaned = re.sub(
        r"^(?:according to (?:data from )?[^.]+?[,:]|data shows(?: that)?|"
        r"the (?:filing|announcement|disclosure) (?:states|says|notes)(?: that)?|"
        r"as (?:disclosed|reported)(?: that)?|"
        r"attention now (?:shifts|switches) to\s+)\s*",
        "",
        text.strip(),
        flags=re.I,
    )
    return cleaned.strip(" ,")


def _finalize_sentence(text: str) -> str:
    out = re.sub(r"\s+", " ", (text or "").strip())
    out = out.strip(" ,;")
    if not out:
        return ""
    if not out.endswith("."):
        out += "."
    return out[0].upper() + out[1:]


def _passive_from_disclosed(text: str) -> str | None:
    match = re.match(
        r"^(?P<sub>.+?)\s+(?:disclosed|announced|said|reported|revealed)\s+that\s+(?P<obj>.+)$",
        text.strip().rstrip("."),
        re.I,
    )
    if not match:
        return None
    subj = match.group("sub").strip()
    obj = match.group("obj").strip()
    return _finalize_sentence(f"{obj} was disclosed by {subj}")


def _lead_with_temporal(text: str) -> str | None:
    match = re.search(
        r"\b(?P<time>on (?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
        r"January|February|March|April|May|June|July|August|September|October|"
        r"November|December)(?:\s+\d{1,2})?|"
        r"since midnight(?: UTC)?|later today|this year)\b",
        text,
        re.I,
    )
    if not match:
        return None
    time = match.group("time").strip()
    before = text[: match.start()].strip(" ,.")
    after = text[match.end() :].strip(" ,.")
    core = " ".join(p for p in (before, after) if p)
    if word_count(core) < 4:
        return None
    lowered = core[0].lower() + core[1:] if core else core
    return _finalize_sentence(f"{time}, {lowered}")


def _subject_object_swap_held(text: str) -> str | None:
    """X held its losses, dropping Y → After a Y move, losses at X remained."""
    match = re.match(
        r"^(?P<sub>.+?)\s+held its (?:loss|losses),\s*dropping\s+(?P<drop>.+)$",
        text.strip().rstrip("."),
        re.I,
    )
    if not match:
        return None
    subj = match.group("sub").strip()
    drop = match.group("drop").strip()
    # Restructure the trailing comparative so long source n-grams break.
    steep = re.search(
        r"^(?P<head>.+?)\s+(?:in|,)(?:\s+its)?\s+steepest decline since (?P<since>.+)$",
        drop,
        re.I,
    )
    if steep:
        head = steep.group("head").strip(" ,")
        since = steep.group("since").strip(" ,.")
        return _finalize_sentence(
            f"After a {head} move, losses at {subj} remained, the steepest decline since {since}"
        )
    return _finalize_sentence(f"After a move of {drop}, losses at {subj} remained")


def _split_followed_by(text: str) -> str | None:
    parts = re.split(r",?\s*followed by\s+", text.strip().rstrip("."), maxsplit=1, flags=re.I)
    if len(parts) != 2:
        parts = re.split(r",\s*while\s+", text.strip().rstrip("."), maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    left = _finalize_sentence(parts[0])
    right = parts[1].strip()
    from_match = re.match(
        r"^(?P<amt>\$?[\d.,]+\s*(?:million|billion)?)\s+from\s+(?P<who>.+)$",
        right,
        re.I,
    )
    if from_match:
        right = _finalize_sentence(
            f"{from_match.group('who')} recorded {from_match.group('amt')} in outflows"
        )
    elif re.match(r"^\$?[\d.,]+\s*(?:million|billion|percent|%)?", right, re.I):
        right = _finalize_sentence(f"An additional {right} was recorded")
    else:
        right = _finalize_sentence(right)
    if not left or not right:
        return None
    return f"{left} {right}"


def _amount_led_outflow(text: str) -> str | None:
    match = re.search(
        r"(?P<who>.+?)\s+(?:saw|recorded|posted|reported)\s+(?:the\s+)?"
        r"(?P<kind>largest|biggest|heaviest|significant)?\s*"
        r"(?P<noun>outflows?|inflows?|withdrawals?|losses?)"
        r"(?:,?\s*(?:shedding|losing|of)\s*|\s+of\s+|\s+)"
        r"(?P<amt>\$?[\d.,]+\s*(?:million|billion)?)",
        text,
        re.I,
    )
    if not match:
        match = re.search(
            r"(?P<who>.+?),?\s+shedding\s+(?P<amt>\$?[\d.,]+\s*(?:million|billion)?)",
            text,
            re.I,
        )
        if not match:
            return None
        who = re.sub(
            r"^(?:data shows|according to data)\s+",
            "",
            match.group("who").strip(" ,"),
            flags=re.I,
        )
        amt = match.group("amt").strip()
        rest = text[match.end() :].strip(" ,.")
        head = _finalize_sentence(f"{amt} exited {who}")
        rest_l = rest.lower()
        if rest_l.startswith("followed by"):
            trailing = _split_followed_by(f"unit followed by {rest[len('followed by'):].strip()}")
            if trailing:
                parts = trailing.split(". ", 1)
                extra = parts[1] if len(parts) > 1 else ""
                return f"{head} {extra}".strip() if extra else head
        return head
    who = re.sub(
        r"^(?:data shows|according to data)\s+",
        "",
        match.group("who").strip(" ,"),
        flags=re.I,
    )
    kind = (match.groupdict().get("kind") or "").strip()
    noun = (match.groupdict().get("noun") or "outflows").strip()
    amt = match.group("amt").strip()
    kind_bit = f"{kind} " if kind else ""
    rest = text[match.end() :].strip(" ,.")
    head = _finalize_sentence(f"{amt} in {kind_bit}{noun} was recorded by {who}")
    rest_l = rest.lower()
    if rest_l.startswith("followed by"):
        trailing = _split_followed_by(f"unit followed by {rest[len('followed by'):].strip()}")
        if trailing:
            parts = trailing.split(". ", 1)
            extra = parts[1] if len(parts) > 1 else ""
            return f"{head} {extra}".strip() if extra else head
    if rest:
        return f"{head} {_finalize_sentence(rest)}"
    return head


def _which_clause_split(text: str) -> str | None:
    match = re.search(r"^(?P<main>.+?),\s*which\s+(?P<rel>.+)$", text.strip().rstrip("."), re.I)
    if not match:
        return None
    main = match.group("main").strip()
    rel = match.group("rel").strip()
    entity = re.search(
        r"\b(the\s+[A-Z][\w]*(?:\s+[A-Z][\w]*)*|Federal Reserve|BlackRock|Senate|Congress)\b",
        main,
    )
    subj = entity.group(0) if entity else "It"
    rel_sent = _finalize_sentence(f"{subj} {rel}")
    main_sent = _finalize_sentence(main)
    return f"{rel_sent} {main_sent}"


def _attention_focus_restructure(text: str) -> str | None:
    match = re.match(
        r"^Attention now (?:shifts|switches) to (?P<focus>.+?),\s*which\s+(?P<rel>.+)$",
        text.strip().rstrip("."),
        re.I,
    )
    if not match:
        return None
    focus = match.group("focus").strip()
    rel = match.group("rel").strip()
    # Break long shared tails like "having been the market's base case going into the meeting".
    base_case = re.search(
        r"^(?P<head>.+?),?\s*with (?:a rate )?an? increase having been the market(?:'s|’s) "
        r"base case going into the meeting$",
        rel,
        re.I,
    )
    if base_case:
        head = base_case.group("head").strip()
        return (
            f"{_finalize_sentence(f'{focus} {head}')} "
            f"{_finalize_sentence('An increase was the market base case going into that meeting')} "
            f"{_finalize_sentence(f'Market focus turns next to {focus}')}"
        )
    return (
        f"{_finalize_sentence(f'{focus} {rel}')} "
        f"{_finalize_sentence(f'Market attention turns next to {focus}')}"
    )


def _active_to_reordered_and(text: str) -> str | None:
    parts = re.split(r"\s+and\s+", text.strip().rstrip("."), maxsplit=1, flags=re.I)
    if len(parts) != 2 or word_count(parts[0]) < 4 or word_count(parts[1]) < 4:
        return None
    return f"{_finalize_sentence(parts[1])} {_finalize_sentence(parts[0])}"


def _generic_clause_reorder(text: str) -> str | None:
    """Last-resort structural change: reverse comma clauses / reopen with trailing clause."""
    raw = text.strip().rstrip(".")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        return None
    # Move last clause to the front.
    reordered = ", ".join([parts[-1], *parts[:-1]])
    return _finalize_sentence(reordered)


def _generic_split_sentences(text: str) -> str | None:
    raw = text.strip().rstrip(".")
    parts = [p.strip() for p in re.split(r",\s*", raw) if word_count(p.strip()) >= 5]
    if len(parts) < 2:
        return None
    mid = max(1, len(parts) // 2)
    left = _finalize_sentence(", ".join(parts[:mid]))
    right = _finalize_sentence(", ".join(parts[mid:]))
    if left.lower() == right.lower():
        return None
    return f"{left} {right}"


def _build_structural_candidates(
    cores: list[str],
    *,
    original_sentence: str,
    locked: set[str],
) -> list[tuple[str, str]]:
    """Return (candidate_text, strategy_name) with materially different structures."""
    if not cores:
        return []
    primary = _strip_report_prefix(cores[0])
    # Prefer longest informative core as rebuild base when first atom is tiny.
    for core in cores:
        stripped = _strip_report_prefix(core)
        if word_count(stripped) > word_count(primary):
            primary = stripped
    secondary = ""
    for core in cores[1:]:
        stripped = _strip_report_prefix(core)
        if stripped.lower() != primary.lower() and word_count(stripped) >= 4:
            secondary = stripped
            break

    builders: list[tuple[str, str | None]] = []
    builders.append(("passive_disclosed", _passive_from_disclosed(primary)))
    builders.append(("amount_led", _amount_led_outflow(primary)))
    builders.append(("temporal_lead", _lead_with_temporal(primary)))
    builders.append(("held_losses_restructure", _subject_object_swap_held(primary)))
    builders.append(("attention_focus", _attention_focus_restructure(primary)))
    builders.append(
        ("attention_focus_raw", _attention_focus_restructure(_strip_report_prefix(cores[0]) if cores else ""))
    )
    # Also try attention pattern on unstripped first core (prefix includes Attention...).
    if cores:
        builders.append(("attention_focus_full", _attention_focus_restructure(cores[0])))
    builders.append(("followed_by_split", _split_followed_by(primary)))
    builders.append(("which_clause_split", _which_clause_split(primary)))
    builders.append(("and_clause_reorder", _active_to_reordered_and(primary)))
    builders.append(("generic_clause_reorder", _generic_clause_reorder(primary)))
    builders.append(("generic_split", _generic_split_sentences(primary)))

    if secondary:
        builders.append(
            (
                "multi_prop_split",
                f"{_finalize_sentence(primary)} {_finalize_sentence(secondary)}",
            )
        )
        builders.append(
            (
                "multi_prop_reorder",
                f"{_finalize_sentence(secondary)} {_finalize_sentence(primary)}",
            )
        )

    base_for_lead = (
        _passive_from_disclosed(primary)
        or _amount_led_outflow(primary)
        or _subject_object_swap_held(primary)
        or _generic_clause_reorder(primary)
        or _finalize_sentence(primary)
    )
    for idx, lead in enumerate(_STRUCTURAL_LEAD_INS):
        if not lead or not base_for_lead:
            continue
        body = base_for_lead[0].lower() + base_for_lead[1:]
        builders.append((f"lead_in_{idx}", _finalize_sentence(f"{lead}{body}")))

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, candidate in builders:
        if not candidate:
            continue
        text = candidate if candidate.endswith(".") else _finalize_sentence(candidate)
        if not text or text.lower() in seen:
            continue
        # Reject near-copies of the offending sentence (synonym-level drift is not enough).
        if _sentence_similarity(text, original_sentence) >= 0.90:
            continue
        if not _preserves_locked_tokens(text, locked):
            continue
        seen.add(text.lower())
        out.append((text, name))
    return out


def independent_semantic_rewrite(
    sentence: str,
    packet: WriterEvidencePacket,
    *,
    avoid_texts: tuple[str, ...] = (),
    attempt_index: int = 0,
) -> tuple[str, dict[str, Any]]:
    """
    Reconstruct a copyright-flagged sentence from authorized atomic propositions
    using a materially different linguistic structure (not synonym substitution).
    """
    original_words = word_count(sentence)
    # Context must not include the source/offender wording as rewrite material.
    cores = _authorized_atomic_cores(sentence, packet, avoid_texts=avoid_texts)
    # Lock facts from authorized propositions (and numbers from the sentence that
    # those props also carry). Do not lock random ALLCAPS noise from the offender.
    locked = _locked_fact_tokens(*cores)
    for num in number_tokens(sentence):
        if any(num.lower() in (core or "").lower() or num in number_tokens(core) for core in cores):
            locked.add(num.lower())
    candidates = _build_structural_candidates(
        cores, original_sentence=sentence, locked=locked
    )

    viable: list[tuple[str, dict[str, Any]]] = []
    for text, strategy in candidates:
        if _shares_exact_ngram(sentence, text):
            continue
        if any(_shares_exact_ngram(text, other) for other in avoid_texts if other):
            continue
        if any(_sentence_similarity(text, other) >= 0.92 for other in avoid_texts if other):
            continue
        repl_words = word_count(text)
        ratio = repl_words / max(1, original_words)
        meta = {
            "repair_length_retention_ratio": round(ratio, 4),
            "original_offending_unit_words": original_words,
            "replacement_words": repl_words,
            "strategy": strategy,
            "attempt_index": attempt_index,
            "realization": "independent_structural",
            "excessive_compression": bool(
                original_words >= 12 and ratio < REPAIR_LENGTH_RETENTION_ADVISORY
            ),
            "proposition_count": len(cores),
        }
        viable.append((text, meta))

    if not viable:
        return sentence, {
            "repair_length_retention_ratio": 1.0,
            "original_offending_unit_words": original_words,
            "replacement_words": original_words,
            "strategy": "unchanged_no_structural_candidate",
            "attempt_index": attempt_index,
            "realization": "independent_structural",
            "proposition_count": len(cores),
        }

    # Bounded alternate: attempt_index walks the viable structural list.
    selected = viable[int(attempt_index) % len(viable)]
    return selected[0], selected[1]


def independent_semantic_rewrite_attempts(
    sentence: str,
    packet: WriterEvidencePacket,
    *,
    avoid_texts: tuple[str, ...] = (),
    max_attempts: int = MAX_STRUCTURAL_REWRITE_ATTEMPTS,
) -> list[tuple[str, dict[str, Any]]]:
    """Bounded alternate structural realizations for one flagged sentence."""
    attempts: list[tuple[str, dict[str, Any]]] = []
    failed: list[str] = []
    limit = max(1, min(int(max_attempts), MAX_STRUCTURAL_REWRITE_ATTEMPTS))
    for idx in range(limit):
        rewritten, meta = independent_semantic_rewrite(
            sentence,
            packet,
            avoid_texts=tuple(list(avoid_texts) + failed),
            attempt_index=idx,
        )
        if rewritten == sentence:
            continue
        if any(rewritten == prev for prev, _ in attempts):
            continue
        attempts.append((rewritten, meta))
        failed.append(rewritten)
    return attempts


def deterministic_paraphrase(
    sentence: str,
    packet: WriterEvidencePacket,
    *,
    force: bool = False,
) -> str:
    """Backward-compatible wrapper around independent semantic rewrite."""
    rewritten, _meta = independent_semantic_rewrite(sentence, packet)
    if rewritten != sentence:
        return rewritten
    if not force:
        return re.sub(r"\s+", " ", sentence).strip()
    return rewritten


def repair_copyright_sentences(
    native: V4NativeArticle,
    packet: WriterEvidencePacket,
    offenders: list[str],
    article_input: dict[str, Any] | None = None,
) -> tuple[V4NativeArticle, list[RepairAction]]:
    """Rewrite copyright-similar sentences via structural realization. Drop last resort."""
    if not offenders:
        return native, []
    repaired = deepcopy(native)
    body = repaired.article_body
    actions: list[RepairAction] = []
    source_avoid: list[str] = []
    if isinstance(article_input, dict):
        from newsagent_v2.article.input import evidence_text_blobs

        for blob in evidence_text_blobs(article_input):
            if blob and blob.strip():
                source_avoid.append(blob)
    avoid = tuple(list(offenders) + source_avoid[:8])
    for sentence in offenders:
        attempts = independent_semantic_rewrite_attempts(
            sentence, packet, avoid_texts=avoid
        )
        committed = False
        last_meta: dict[str, Any] = {
            "original_offending_unit_words": word_count(sentence),
        }
        for rewritten, meta in attempts:
            last_meta = meta
            still_collides = (
                rewritten == sentence
                or _shares_exact_ngram(sentence, rewritten)
                or any(_shares_exact_ngram(rewritten, src) for src in source_avoid)
                or any(_sentence_similarity(rewritten, src) >= 0.92 for src in source_avoid[:12])
            )
            if still_collides or not rewritten.strip():
                continue
            body = body.replace(sentence, rewritten, 1)
            actions.append(
                RepairAction(
                    kind="copyright_sentence_rewrite",
                    detail=(
                        "independent structural realization; "
                        f"strategy={meta.get('strategy')}; "
                        f"retention={meta.get('repair_length_retention_ratio')}"
                        + (
                            "; excessive_compression"
                            if meta.get("excessive_compression")
                            else ""
                        )
                    ),
                    before=sentence,
                    after=rewritten,
                )
            )
            committed = True
            break

        if committed:
            continue

        # LAST RESORT only — never the default path.
        body = _drop_sentences(body, {sentence})
        actions.append(
            RepairAction(
                kind="copyright_sentence_drop",
                detail=(
                    "last-resort drop after structural realization failed to clear source overlap; "
                    f"original_words={last_meta.get('original_offending_unit_words')}"
                ),
                before=sentence,
                after="",
            )
        )
    repaired.article_body = body
    return repaired, actions


def model_targeted_sentence_repair(
    *,
    writer: V4NaturalProseWriter,
    sentence: str,
    reason: str,
    packet: WriterEvidencePacket,
) -> tuple[str | None, int]:
    """One model call to rewrite a single sentence. Returns (text, calls)."""
    from newsagent_v2.article.writer.v4.writer import build_v4_writer_messages

    # Reuse writer HTTP with a minimal prompt via Scripted-like soft path:
    # Build a tiny packet-constrained rewrite by asking for a one-sentence body.
    mini = WriterEvidencePacket(
        event_id=packet.event_id,
        story_topic=packet.story_topic,
        authorized_facts=packet.authorized_facts[:8],
        authorized_quotes=packet.authorized_quotes[:4],
        authorized_entities=packet.authorized_entities,
        source_context=packet.source_context,
        forbidden=packet.forbidden,
    )
    # Use the same writer with a patched packet message by temporary render of expansion-like article.
    # Prefer deterministic if writer is scripted without HTTP.
    if writer.http_post is None and not writer.api_key:
        return deterministic_paraphrase(sentence, packet), 0
    # Ask writer for a tiny article whose body is the repaired sentence only.
    # Simpler: deterministic only for offline; live uses one constrained completion via render then extract.
    # To keep repair surgical and budgeted, stick to deterministic paraphrase here when possible.
    del reason, build_v4_writer_messages, mini
    return deterministic_paraphrase(sentence, packet), 0


def expand_from_unused_facts(
    native: V4NativeArticle,
    packet: WriterEvidencePacket,
    report: VerificationReport,
    *,
    min_words: int,
) -> tuple[V4NativeArticle, RepairAction | None]:
    if word_count(native.article_body) >= min_words:
        return native, None
    used: set[str] = set()
    for row in report.rows:
        used.update(row.claim_ids)
    unused = [fact for fact in packet.authorized_facts if fact.id not in used]
    if not unused:
        return native, None
    extras = " ".join(
        (fact.proposition if fact.proposition.endswith(".") else fact.proposition + ".")
        for fact in unused[:6]
    )
    repaired = deepcopy(native)
    before = repaired.article_body
    repaired.article_body = (before + " " + extras).strip()
    return repaired, RepairAction(
        kind="length_expansion_unused_facts",
        detail=f"appended {min(6, len(unused))} unused authorized facts",
        before=before[-120:],
        after=extras[:240],
    )


def repair_unsupported_propositions(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    report: VerificationReport,
) -> tuple[V4NativeArticle, list[RepairAction], bool]:
    """One bounded pass: rewrite UNSUPPORTED / required-AMBIGUOUS units only.

    SUPPORTED sentences are immutable. Replacements must be grammatical newsroom
    sentences from authorized semantics — never raw FactBank fragments. Each
    accepted rewrite is rolled back if it worsens grounding, creates a fragment,
    or collapses paragraph integrity.
    """
    from newsagent_v2.article.writer.v4.atomic_grounding import (
        build_authorized_proposition_set,
        unused_authorized_propositions,
        verify_atomic_article,
        verbalize_proposition,
    )

    # Only true unsupported / ambiguous units — never SUPPORTED.
    unsupported_sentences = [
        row.text
        for row in report.rows
        if row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS}
        and row.text in (native.article_body or "")
    ]
    if not unsupported_sentences:
        return native, [], False

    # Guard: if a listed sentence is actually SUPPORTED on re-check, skip it.
    pre_map = {row.text: row.status for row in report.rows}
    authorized = build_authorized_proposition_set(ledgers=ledgers, packet=packet)
    atomic = verify_atomic_article(
        headline=native.headline,
        dek=native.dek,
        article_body=native.article_body,
        authorized=authorized,
    )
    unused_atomic = unused_authorized_propositions(authorized, atomic)

    # Prefer full AuthorizedFact propositions over atomic sub-fragments.
    # Include facts even if partially matched elsewhere — a full newsroom
    # sentence may still be the safest replacement for a mixed unsupported unit.
    candidate_texts: list[tuple[str, str]] = []
    seen_cand: set[str] = set()
    for fact in packet.authorized_facts:
        if is_boilerplate_proposition(fact.proposition):
            continue
        sentence = newsroom_sentence_from_authorized(fact.proposition)
        if sentence and sentence not in seen_cand:
            candidate_texts.append((fact.id, sentence))
            seen_cand.add(sentence)
    for prop in unused_atomic:
        if is_boilerplate_proposition(prop.text):
            continue
        sentence = newsroom_sentence_from_authorized(verbalize_proposition(prop))
        if sentence and sentence not in seen_cand:
            candidate_texts.append((prop.prop_id, sentence))
            seen_cand.add(sentence)

    working = deepcopy(native)
    actions: list[RepairAction] = []
    pre_unsupported = int(report.unsupported or 0) + int(report.ambiguous or 0)

    def _overlap_score(sentence: str, candidate: str) -> float:
        a = {t for t in words(sentence.lower()) if len(t) > 3}
        b = {t for t in words(candidate.lower()) if len(t) > 3}
        if not a or not b:
            return 0.0
        return len(a & b) / len(a)

    for sentence in unsupported_sentences:
        if sentence not in working.article_body:
            continue
        if pre_map.get(sentence) not in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS}:
            actions.append(
                RepairAction(
                    kind="supported_sentence_skipped",
                    detail="SUPPORTED assertion is immutable to unsupported_proposition_rewrite",
                    before=sentence,
                    after=sentence,
                )
            )
            continue

        # Rank candidates by overlap with the unsupported sentence so we do not
        # replace a Clarity Act claim with an unrelated ETF-flow stub.
        ranked = sorted(
            candidate_texts,
            key=lambda item: _overlap_score(sentence, item[1]),
            reverse=True,
        )
        replacement = None
        for prop_id, candidate in ranked:
            if not candidate or candidate == sentence:
                continue
            # Avoid introducing exact duplicate sentences already present.
            if candidate in working.article_body and candidate != sentence:
                continue
            if is_sentence_fragment(candidate) or not is_complete_newsroom_sentence(candidate):
                continue
            # Avoid compressing a developed sentence into a much shorter stub,
            # unless overlap is strong (core claim preserved).
            overlap = _overlap_score(sentence, candidate)
            if word_count(sentence) >= 25 and word_count(candidate) < max(
                12, int(word_count(sentence) * 0.45)
            ):
                if overlap < 0.28:
                    continue
            if overlap < 0.18 and word_count(sentence) >= 20:
                continue
            probe = deepcopy(working)
            probe.article_body = probe.article_body.replace(sentence, candidate, 1)
            if not paragraph_integrity_ok(probe.article_body, prior_body=working.article_body):
                actions.append(
                    RepairAction(
                        kind="unsupported_proposition_rewrite_rolled_back",
                        detail=f"paragraph integrity failed for {prop_id}",
                        before=sentence,
                        after=candidate,
                    )
                )
                continue
            probe_report = verify_v4_native(probe, packet=packet, ledgers=ledgers)
            still_bad = any(
                row.text == candidate
                and row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS}
                for row in probe_report.rows
            )
            post_bad = int(probe_report.unsupported or 0) + int(probe_report.ambiguous or 0)
            if still_bad or post_bad > pre_unsupported:
                actions.append(
                    RepairAction(
                        kind="unsupported_proposition_rewrite_rolled_back",
                        detail=f"grounding regression for {prop_id}",
                        before=sentence,
                        after=candidate,
                    )
                )
                continue
            if not paragraph_integrity_ok(probe.article_body, prior_body=native.article_body):
                actions.append(
                    RepairAction(
                        kind="unsupported_proposition_rewrite_rolled_back",
                        detail=f"coherence regression for {prop_id}",
                        before=sentence,
                        after=candidate,
                    )
                )
                continue
            working = probe
            pre_unsupported = post_bad
            actions.append(
                RepairAction(
                    kind="unsupported_proposition_rewrite",
                    detail=f"replaced unsupported unit with {prop_id}",
                    before=sentence,
                    after=candidate,
                )
            )
            replacement = candidate
            break
        if replacement is None:
            # Prefer dropping unsupported synthesis over leaving a QA-critical sentence.
            probe = deepcopy(working)
            probe.article_body = re.sub(
                r"\s{2,}",
                " ",
                probe.article_body.replace(sentence, " ", 1),
            ).strip()
            if probe.article_body and paragraph_integrity_ok(
                probe.article_body, prior_body=working.article_body
            ):
                working = probe
                actions.append(
                    RepairAction(
                        kind="unsupported_proposition_dropped",
                        detail="dropped unsupported unit with no safe authorized replacement",
                        before=sentence,
                        after="",
                    )
                )
            else:
                actions.append(
                    RepairAction(
                        kind="unsupported_proposition_unresolved",
                        detail="no safe authorized newsroom replacement for unsupported unit",
                        before=sentence,
                        after="",
                    )
                )

    final_report = verify_v4_native(working, packet=packet, ledgers=ledgers)
    still_unsupported = any(
        row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS}
        and row.text in (working.article_body or "")
        for row in final_report.rows
    )
    if not paragraph_integrity_ok(working.article_body, prior_body=native.article_body):
        actions.append(
            RepairAction(
                kind="unsupported_proposition_rewrite_rolled_back",
                detail="final paragraph integrity check failed; full repair pass rolled back",
                before=native.article_body[:200],
                after=working.article_body[:200],
            )
        )
        return native, actions, True
    rewrites = [a for a in actions if a.kind == "unsupported_proposition_rewrite"]
    if still_unsupported and not rewrites:
        # Nothing safe applied — keep pre-repair body for drop fallback.
        return native, actions, True
    if not rewrites:
        return native, actions, False
    # Commit successful rewrites even if some units remain unresolved for drop.
    return working, actions, False


def run_targeted_repairs(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    writer: Any | None = None,
    min_words: int = 120,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    allow_destructive_length_pad: bool = True,
) -> tuple[V4NativeArticle, VerificationReport, RepairLog]:
    """Apply surgical factual repairs. Copyright recovery is not part of the V4 path."""
    from newsagent_v2.article.writer.v4.expand import EDITORIAL_TARGET_MIN_WORDS

    del max_rounds, article_input, writer  # reserved / unused after copyright removal
    log = RepairLog()
    current = deepcopy(native)
    native_words = word_count(native.article_body)
    report = verify_v4_native(current, packet=packet, ledgers=ledgers)

    # Headline number repair first.
    current2, action = repair_unsupported_headline_number(current, packet)
    if action:
        current = current2
        log.actions.append(action)
        log.rounds = max(1, log.rounds)
        report = verify_v4_native(current, packet=packet, ledgers=ledgers)

    # One bounded surgical unsupported-proposition repair (no destructive salvage).
    if not report.ok and (
        report.unsupported_factual_propositions or report.unsupported or report.ambiguous
    ):
        repaired, actions, rejected = repair_unsupported_propositions(
            current,
            packet=packet,
            ledgers=ledgers,
            report=report,
        )
        log.actions.extend(actions)
        log.rounds = max(1, log.rounds)
        if rejected:
            log.actions.append(
                RepairAction(
                    kind="unsupported_repair_rejected",
                    detail="unsupported propositions remained after one surgical pass",
                )
            )
            report = verify_v4_native(current, packet=packet, ledgers=ledgers)
        else:
            current = repaired
            report = verify_v4_native(current, packet=packet, ledgers=ledgers)

    # If unsupported remain, surgically DROP remaining unsupported sentences.
    # Prefer evaluative / short units first; never drop SUPPORTED sentences.
    if not report.ok and (report.unsupported or report.ambiguous):
        from newsagent_v2.article.writer.v4.atomic_grounding import SIGNIFICANCE_RE

        unsupported_texts = [
            row.text
            for row in report.rows
            if row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS, STATUS_QUOTE_BAD}
            and row.text in (current.article_body or "")
        ]
        # Drop evaluative first, then short leftovers, then any remaining unsupported.
        ordered = sorted(
            unsupported_texts,
            key=lambda t: (
                0 if SIGNIFICANCE_RE.search(t) else 1,
                0 if word_count(t) < 28 else 1,
                -word_count(t),
            ),
        )
        working = deepcopy(current)
        drop_actions: list[RepairAction] = []
        for sentence in ordered:
            if sentence not in working.article_body:
                continue
            probe = deepcopy(working)
            probe.article_body = _drop_sentences(probe.article_body, {sentence})
            if not probe.article_body.strip():
                continue
            if not paragraph_integrity_ok(probe.article_body, prior_body=current.article_body):
                continue
            probe_report = verify_v4_native(probe, packet=packet, ledgers=ledgers)
            if (
                probe_report.unsupported + probe_report.ambiguous
                >= report.unsupported + report.ambiguous
                and sentence in (probe.article_body or "")
            ):
                continue
            # Keep enough body for downstream length gates when possible.
            if word_count(probe.article_body) < max(40, min_words // 2):
                continue
            # If caller supplied a hard floor and we are already at/above it,
            # do not drop below that floor (capability / editorial length).
            if (
                word_count(current.article_body) >= min_words
                and word_count(probe.article_body) < min_words
            ):
                continue
            working = probe
            report = probe_report
            drop_actions.append(
                RepairAction(
                    kind="unsupported_sentence_drop",
                    detail="dropped unsupported/ambiguous sentence",
                    before=sentence,
                    after="",
                )
            )
            if report.ok:
                break
        if drop_actions:
            current = working
            log.actions.extend(drop_actions)
            log.rounds = max(1, log.rounds)
        elif unsupported_texts:
            log.actions.append(
                RepairAction(
                    kind="unsupported_drop_rolled_back",
                    detail="no safe unsupported sentence drop available",
                )
            )

    log.copyright_recovery = {"active": False, "disabled": True}
    log.copyright_rejected = False
    post_words = word_count(current.article_body)
    if post_words < native_words and post_words < EDITORIAL_TARGET_MIN_WORDS:
        log.depth_loss_origin = "FACTUAL_REPAIR"
    else:
        log.depth_loss_origin = None

    # Hard-floor pad only for genuine underproduction.
    if allow_destructive_length_pad and word_count(current.article_body) < min_words:
        current2, action = expand_from_unused_facts(
            current, packet, report, min_words=min_words
        )
        if action:
            current = current2
            log.actions.append(action)
            report = verify_v4_native(current, packet=packet, ledgers=ledgers)

    return current, report, log
