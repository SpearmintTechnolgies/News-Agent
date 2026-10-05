"""Fact bank construction.

Every fact is a sentence that appears in at least one fetched source, tagged
with every source that reported it. Sentences that are the publisher talking
(reader advice, "our coverage", rhetorical questions) are not facts.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from rapidfuzz import fuzz

from newsagent_v2.textutil import number_tokens, split_sentences
from newsagent_v2.research.chrome import is_chrome
from newsagent_v2.research.dossier import KIND_PRIMARY, ResearchDossier, SourceDoc
from newsagent_v2.research.pipeline import event_terms

MIN_FACT_WORDS = 8
MAX_FACT_WORDS = 70
MIN_QUOTE_WORDS = 5
DUPLICATE_RATIO = 82

KIND_FACT = "fact"
KIND_CONTEXT = "context"

_QUOTE_RE = re.compile(r"[“\"]([^”\"]{10,600})[”\"]")
_SPEAKER_AFTER_RE = re.compile(
    r"[”\"],?\s+(?:said|says|told|wrote|added|stated|noted)\s+((?:[A-Z][\w.'’-]+\s?){1,3})"
)
_SPEAKER_BEFORE_RE = re.compile(
    r"((?:[A-Z][\w.'’-]+\s){0,2}[A-Z][\w.'’-]+)\s+(?:said|says|told|wrote|added|stated|noted)\b[^“\"]{0,60}[“\"]"
)
_SPEAKER_TRAIL_RE = re.compile(r"[”\"],?\s+((?:[A-Z][\w.'’-]+\s?){1,3})\s+(?:said|says|told|wrote|added)\b")
_ATTRIBUTION_RE = re.compile(
    r"\b(said|says|told|according to|reported|announced|stated|confirmed|wrote|filed|disclosed|showed)\b",
    re.IGNORECASE,
)
_READER_ADDRESS_RE = re.compile(r"\b(you|your|yours)\b", re.IGNORECASE)
# Case-sensitive on purpose: "US" is the country, "I" in "Phase I" is rare.
_PUBLISHER_VOICE_RE = re.compile(
    r"\b(?:[Ww]e|[Oo]ur|us|[Mm]y|I)\b|\b(?:[Tt]his|[Tt]he) (?:article|report|piece|analysis) (?:maps|explains|looks)"
)
# The outlet's own analysis voice, not reporting.
_OPINION_RE = re.compile(
    r"\b(it'?s (obvious|clear|hard to see|easy to see|worth noting)|keep in mind|simple math|case in point|"
    r"in other words|in short|of equal concern|big ask|make no mistake|the bottom line|needless to say|"
    r"to be sure|that said|the takeaway|the question is|remains to be seen|only time will tell|"
    r"is worth sitting with|rewards a closer look|we believe|i believe|in my view|arguably)\b",
    re.IGNORECASE,
)
_IMPERATIVE_START = frozenset(
    "understand settle consider remember check make keep read watch learn see note think look take "
    "avoid buy sell hold start stop try imagine picture compare".split()
)
_PRONOUN_SPEAKERS = frozenset({"He", "She", "They", "It", "We", "I"})


@dataclass
class Quote:
    text: str
    speaker: str
    source_url: str
    publisher: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class Fact:
    id: str
    text: str
    kind: str
    core: bool
    source_urls: list[str] = field(default_factory=list)
    publishers: list[str] = field(default_factory=list)
    primary: bool = False
    numbers: list[str] = field(default_factory=list)
    attributed: bool = False
    position: float = 0.0

    @property
    def corroboration(self) -> int:
        return len(set(self.publishers))

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["corroboration"] = self.corroboration
        return out


@dataclass
class FactBank:
    event_id: str
    title: str
    facts: list[Fact] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    dropped: dict[str, int] = field(default_factory=dict)

    @property
    def core_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.core and f.kind == KIND_FACT]

    def stats(self) -> dict[str, Any]:
        core = self.core_facts
        return {
            "facts": len(self.facts),
            "core_facts": len(core),
            "context_facts": sum(1 for f in self.facts if f.kind == KIND_CONTEXT),
            "numeric_core_facts": sum(1 for f in core if f.numbers),
            "corroborated_core_facts": sum(1 for f in core if f.corroboration >= 2),
            "primary_facts": sum(1 for f in self.facts if f.primary),
            "attributed_quotes": sum(1 for q in self.quotes if q.speaker),
            "fact_words": sum(len(f.text.split()) for f in core),
            "dropped": dict(self.dropped),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "stats": self.stats(),
            "facts": [f.to_dict() for f in self.facts],
            "quotes": [asdict(q) for q in self.quotes],
        }


def _outside_quotes(sentence: str) -> str:
    return _QUOTE_RE.sub(" ", sentence)


def _reject_reason(sentence: str) -> str:
    words = sentence.split()
    if len(words) < MIN_FACT_WORDS:
        return "too_short"
    if len(words) > MAX_FACT_WORDS:
        return "too_long"
    if is_chrome(sentence):
        return "chrome"
    voice = _outside_quotes(sentence)
    if voice.rstrip().endswith("?"):
        return "rhetorical_question"
    if _READER_ADDRESS_RE.search(voice):
        return "reader_address"
    if _PUBLISHER_VOICE_RE.search(voice):
        return "publisher_voice"
    if words[0].lower().strip("“\"'") in _IMPERATIVE_START:
        return "imperative"
    if _OPINION_RE.search(voice):
        return "publisher_opinion"
    return ""


_PRONOUN_SAID_RE = re.compile(r"[”\"],?\s+(?:he|she)\s+(?:said|says|added|told|wrote)\b|\b(?:He|She) (?:said|says|added|told)\b")
_NAMED_SUBJECT_RE = re.compile(r"\b([A-Z][\w'’-]+(?:\s[A-Z][\w'’-]+){0,2})\s+(?:said|says|told|wrote|added|called|argued)\b")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,200}>")


def _speaker(sentence: str) -> str:
    for pattern in (_SPEAKER_AFTER_RE, _SPEAKER_TRAIL_RE, _SPEAKER_BEFORE_RE):
        match = pattern.search(sentence)
        if match:
            name = match.group(1).strip(" ,.")
            if name and name.split()[0] not in _PRONOUN_SPEAKERS:
                return name
    return ""


def _named_subject(sentence: str) -> str:
    match = _NAMED_SUBJECT_RE.search(sentence)
    if match and match.group(1).split()[0] not in _PRONOUN_SPEAKERS | {"The", "This", "A"}:
        return match.group(1)
    return ""


def _quotes_in(sentence: str, doc: SourceDoc, previous_speaker: str = "") -> list[Quote]:
    found: list[Quote] = []
    speaker = _speaker(sentence)
    if not speaker and previous_speaker and _PRONOUN_SAID_RE.search(sentence):
        speaker = previous_speaker
    for match in _QUOTE_RE.finditer(sentence):
        text = match.group(1).strip().rstrip(",")
        if len(text.split()) >= MIN_QUOTE_WORDS:
            found.append(Quote(text=text, speaker=speaker, source_url=doc.url, publisher=doc.publisher or doc.host))
    return found


# Names that appear in nearly every crypto/markets story; they do not make a
# sentence about *this* event.
GENERIC_NAMES = frozenset(
    "bitcoin btc ethereum ether eth crypto cryptocurrency cryptocurrencies stablecoin stablecoins "
    "token tokens market markets price prices us u.s. united states america american dollar "
    "investors traders etf etfs fund funds defi blockchain digital assets asset".split()
)


def distinctive_markers(title: str, entities: list[str]) -> tuple[list[str], set[str]]:
    names = [e for e in entities if e.lower() not in GENERIC_NAMES and len(e) >= 3]
    terms = {t for t in event_terms(title, []) if t not in GENERIC_NAMES}
    return names, terms


def _is_core(sentence: str, names: list[str], terms: set[str]) -> bool:
    lowered = sentence.lower()
    if any(re.search(rf"\b{re.escape(n.lower())}\b", lowered) for n in names):
        return True
    tokens = set(re.findall(r"[a-z0-9$%.,'-]+", lowered))
    tokens |= {t.strip(".,") for t in tokens}
    return len(terms & tokens) >= 2


def _number_signature(text: str) -> frozenset[str]:
    return frozenset(n.replace(" ", "").lower() for n in number_tokens(text))


def build_fact_bank(dossier: ResearchDossier) -> FactBank:
    bank = FactBank(event_id=dossier.event_id, title=dossier.title)
    names, terms = distinctive_markers(dossier.title, dossier.entities)
    sources = sorted(dossier.full_sources, key=lambda d: (d.kind != KIND_PRIMARY, -d.relevance))
    seen_quotes: set[str] = set()
    for doc in sources:
        publisher = doc.publisher or doc.host
        sentences = [
            s.strip()
            for p in doc.paragraphs
            for s in split_sentences(_HTML_TAG_RE.sub("", p))
            if s.strip()
        ]
        total = max(1, len(sentences))
        last_speaker = ""
        for index, sentence in enumerate(sentences):
            for quote in _quotes_in(sentence, doc, last_speaker):
                key = re.sub(r"\W+", " ", quote.text.lower()).strip()
                if key not in seen_quotes:
                    seen_quotes.add(key)
                    bank.quotes.append(quote)
            last_speaker = _speaker(sentence) or _named_subject(sentence) or last_speaker
            reason = _reject_reason(sentence)
            if reason:
                bank.dropped[reason] = bank.dropped.get(reason, 0) + 1
                continue
            numbers = _number_signature(sentence)
            attributed = bool(_ATTRIBUTION_RE.search(sentence))
            core = _is_core(sentence, names, terms)
            kind = KIND_FACT if (numbers or attributed or _speaker(sentence) or core) else KIND_CONTEXT
            match = _find_duplicate(bank.facts, sentence, numbers)
            if match is not None:
                if publisher not in match.publishers:
                    match.publishers.append(publisher)
                    match.source_urls.append(doc.url)
                match.primary = match.primary or doc.kind == KIND_PRIMARY
                if len(sentence) > len(match.text) and doc.kind != KIND_PRIMARY and not match.primary:
                    match.text = sentence
                bank.dropped["merged_duplicate"] = bank.dropped.get("merged_duplicate", 0) + 1
                continue
            bank.facts.append(
                Fact(
                    id=f"F{len(bank.facts) + 1}",
                    text=sentence,
                    kind=kind,
                    core=core,
                    source_urls=[doc.url],
                    publishers=[publisher],
                    primary=doc.kind == KIND_PRIMARY,
                    numbers=sorted(numbers),
                    attributed=attributed,
                    position=round(index / total, 3),
                )
            )
    return bank


def _find_duplicate(facts: list[Fact], sentence: str, numbers: frozenset[str]) -> Fact | None:
    for fact in facts:
        ratio = fuzz.token_set_ratio(sentence, fact.text)
        if ratio >= DUPLICATE_RATIO:
            return fact
        if numbers and len(numbers) >= 2 and numbers == frozenset(fact.numbers) and ratio >= 65:
            return fact
    return None
