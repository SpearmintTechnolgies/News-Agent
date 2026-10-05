"""Article QA against the fact bank and the research dossier.

Severities:
- BLOCK: cannot publish (invented names/figures/quotes, junk, too short).
- FIX: worth one revision call (copying, repetition, editorializing).
- WARN: shown to the editor on the review card.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from rapidfuzz import fuzz

from newsagent_v2.textutil import split_sentences
from newsagent_v2.facts.bank import FactBank, Quote
from newsagent_v2.research.chrome import has_chrome_phrase
from newsagent_v2.research.dossier import ResearchDossier
from newsagent_v2.write.article import Article
from newsagent_v2.write.prompt import BODY_MIN_WORDS, usable_quotes
from newsagent_v2.write.writer import structural_issues

BLOCK = "block"
FIX = "fix"
WARN = "warn"

COPY_RUN_FIX = 12
COPY_SHINGLE = 8
DUPLICATE_SENTENCE_RATIO = 90

_QUOTED_RE = re.compile(r"[“\"]([^”\"]{3,600})[”\"]")
_NUMBER_RE = re.compile(r"(?<![\w.])\$?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s?(?:%|percent|bn|billion|million|trillion|m|k|x))?", re.I)
_NAME_RE = re.compile(r"\b([A-Z][\w&'’.-]*(?:\s+(?:of|de|the|for)?\s*[A-Z][\w&'’.-]*){0,4})")
_TITLE_WORDS = frozenset(
    "chairman chair chairwoman president vice ceo cfo director secretary under minister prime senator rep "
    "representative governor commissioner analyst founder co-founder chief executive officer head general "
    "attorney speaker leader spokesperson spokesman spokeswoman deputy acting former interim".split()
)
_URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|io|org|net|co|news|finance)\b(?!\S*\s+(?:ETF|Inc))", re.I)
# Outlets and agencies whose proper name is a domain.
_PUBLICATION_DOMAINS_RE = re.compile(r"\b(?:Bitcoin\.com|Investor\.gov|Crypto\.com|Investing\.com|Kraken\.com)\b", re.I)
_MARKUP_RE = re.compile(r"</?[a-z][^>]*>|\*\*|__|^#+\s|\[[^\]]+\]\([^)]+\)", re.I | re.M)
_FIRST_PERSON_RE = re.compile(r"\b(?:I|we|We|our|Our|us)\b")
_READER_RE = re.compile(r"\b(?:you|your|You|Your)\b")

HYPE_PHRASES = (
    "game-changer", "game changer", "seismic", "landmark", "sent shockwaves", "shockwaves", "it remains to be seen",
    "only time will tell", "in a significant development", "amid growing", "notably", "furthermore", "moreover",
    "skyrocket", "soared to new heights", "to the moon", "massive", "stunning", "jaw-dropping", "unprecedented",
    "in conclusion", "in summary", "it is worth noting", "it's worth noting", "needless to say", "a testament to",
    "paving the way", "the stage is set", "buckle up", "explosive",
)
JUDGMENT_WORDS = (
    "significant shift", "polarizing", "aggressive", "controversial", "turning point", "historic", "major blow",
    "bold move", "sweeping", "dramatic", "alarming", "staggering", "remarkable", "crucial", "pivotal",
)
_ALWAYS_OK_NAMES = frozenset(
    "The This That These Those It He She They A An In On At For By With From As After Before While When If But And Or "
    "Conclusion Frequently Asked Questions FAQ Q A U.S. US UK EU AI Monday Tuesday Wednesday Thursday Friday Saturday "
    "Sunday January February March April May June July August September October November December Bitcoin Ether "
    "Ethereum Crypto What Why How Who Where Which Can Does Did Is Are Will Has Have".split()
)


@dataclass
class Issue:
    code: str
    severity: str
    location: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QAReport:
    issues: list[Issue] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def by(self, severity: str) -> list[Issue]:
        return [i for i in self.issues if i.severity == severity]

    @property
    def blocked(self) -> bool:
        return bool(self.by(BLOCK))

    def revision_requests(self) -> list[str]:
        return [f"[{i.location}] {i.message}" for i in self.issues if i.severity in (BLOCK, FIX)]

    def summary(self) -> str:
        return f"{len(self.by(BLOCK))} blocking, {len(self.by(FIX))} to fix, {len(self.by(WARN))} warnings"

    def to_dict(self) -> dict[str, Any]:
        return {"blocked": self.blocked, "summary": self.summary(), "metrics": dict(self.metrics),
                "issues": [i.to_dict() for i in self.issues]}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9$%.' ]+", " ", text.lower().replace("’", "'"))).strip()


def _strip_quotes(text: str) -> str:
    return _QUOTED_RE.sub(" ", text)


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for match in _NUMBER_RE.finditer(text):
        core = match.group(1).replace(",", "")
        if "." in core:
            core = core.rstrip("0").rstrip(".")
        out.add(core)
    return out


def _names(text: str) -> set[str]:
    found: set[str] = set()
    for sentence in split_sentences(text):
        for match in _NAME_RE.finditer(sentence):
            raw = match.group(1).strip(" .,'’")
            parts = [p for p in raw.split() if p not in {"of", "de", "the", "for"}]
            # A lone capitalized word opening a sentence is ordinary capitalization.
            if match.start() == 0 and len(parts) == 1:
                continue
            parts = [p.split("-")[0] if "-" in p and not p.isupper() else p for p in parts]
            while parts and parts[0] in _ALWAYS_OK_NAMES:
                parts = parts[1:]
            if not parts:
                continue
            name = " ".join(parts)
            if name in _ALWAYS_OK_NAMES or len(name) < 2:
                continue
            found.add(name)
    return found


def _name_tokens(name: str) -> list[str]:
    tokens = []
    for token in _norm(name).replace("'s ", " ").removesuffix("'s").split():
        token = token.strip(".'")
        if token and token not in _TITLE_WORDS and token not in {"of", "de", "the", "for"}:
            tokens.append(token)
    return tokens


def _token_in(token: str, haystack: str) -> bool:
    if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", haystack):
        return True
    return len(token) >= 4 and re.search(rf"(?<![a-z0-9]){re.escape(token)}[a-z]{{1,3}}\b", haystack) is not None


def _name_support(name: str, haystack: str) -> float:
    """Share of the name's tokens (titles ignored) found in the haystack."""
    tokens = _name_tokens(name)
    if not tokens:
        return 1.0
    return sum(1 for t in tokens if _token_in(t, haystack)) / len(tokens)


def _shingles(words: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


class _Context:
    def __init__(self, article: Article, bank: FactBank, dossier: ResearchDossier | None) -> None:
        self.article = article
        self.facts = {f.id: f for f in bank.facts}
        self.bank = bank
        source_text = [p for s in (dossier.sources if dossier else []) for p in s.paragraphs]
        source_text += [f.text for f in bank.facts] + [q.text for q in bank.quotes]
        source_text += [q.speaker for q in bank.quotes if q.speaker]
        outlets: set[str] = set()
        for doc in dossier.sources if dossier else []:
            outlets.update({doc.publisher, doc.host, *doc.host.split(".")})
        for fact in bank.facts:
            outlets.update(fact.publishers)
        outlets.update(q.publisher for q in bank.quotes)
        self.outlets = _norm(" ".join(o for o in outlets if o))
        self.all_text = _norm(" ".join(source_text)) + " " + self.outlets
        self.all_numbers = _numbers(" ".join(source_text))
        self.quotes = bank.quotes
        self.source_shingles: set[tuple[str, ...]] = set()
        for para in source_text:
            self.source_shingles.update(_shingles(_norm(para).split(), COPY_SHINGLE))

    def cited_text(self, fact_ids: list[str]) -> str:
        return " ".join(self.facts[f].text for f in fact_ids if f in self.facts)


def _units(article: Article) -> list[tuple[str, str, list[str]]]:
    """(location, text, cited fact ids) for every checkable text unit."""
    units = [("headline", article.headline, []), ("dek", article.dek, [])]
    for location, para in article.all_paragraphs():
        units.append((location, para.text, para.facts))
    for index, item in enumerate(article.faq, start=1):
        units.append((f"faq{index}.question", item.question, item.facts))
    units.append(("meta_description", article.seo.meta_description, []))
    return units


def _minor_number(number: str) -> bool:
    try:
        value = float(number)
    except ValueError:
        return False
    return value <= 10 or (1990 <= value <= 2035 and "." not in number)


def _check_grounding(ctx: _Context, issues: list[Issue]) -> dict[str, int]:
    stats = {"numbers_checked": 0, "names_checked": 0}
    whole_article_cited = _norm(ctx.cited_text(sorted(ctx.article.cited_facts()))) + " " + ctx.outlets
    for location, text, cited in _units(ctx.article):
        prose = _strip_quotes(text)
        cited_text = ctx.cited_text(cited)
        cited_numbers = _numbers(cited_text)
        cited_norm = _norm(cited_text) + " " + ctx.outlets
        for number in sorted(_numbers(prose)):
            stats["numbers_checked"] += 1
            if number in cited_numbers:
                continue
            if number not in ctx.all_numbers:
                issues.append(Issue("invented_figure", BLOCK, location,
                                    f'the figure "{number}" does not appear in any source; remove it or use a sourced figure'))
            elif cited and not location.endswith("question"):
                severity = WARN if _minor_number(number) else FIX
                issues.append(Issue("uncited_figure", severity, location,
                                    f'the figure "{number}" is not in the facts this paragraph cites; cite the fact it comes from'))
        if location == "headline":
            continue
        for name in sorted(_names(prose)):
            stats["names_checked"] += 1
            if _name_support(name, cited_norm) == 1.0 or _name_support(name, whole_article_cited) == 1.0:
                continue
            support = _name_support(name, ctx.all_text)
            if support < 0.5 and len(_name_tokens(name)) >= 2:
                issues.append(Issue("invented_name", BLOCK, location,
                                    f'"{name}" does not appear in any source; remove it or replace it with a sourced name'))
            elif support < 0.5:
                # Lone words ("Pentagon", "Beijing") are usually well-known synonyms; the editor decides.
                issues.append(Issue("unsourced_name", FIX, location,
                                    f'"{name}" does not appear in any source; use the wording the sources use'))
            elif support < 1.0:
                issues.append(Issue("unverified_name", WARN, location,
                                    f'"{name}" is only partly matched in the sources (check spelling or abbreviation)'))
            elif cited:
                issues.append(Issue("uncited_name", WARN, location,
                                    f'"{name}" is in the sources but not in the facts this paragraph cites'))
    return stats


def _check_quotes(ctx: _Context, issues: list[Issue]) -> int:
    count = 0
    quote_texts = [(_norm(q.text), q) for q in ctx.quotes]
    for location, text, _ in _units(ctx.article):
        for match in _QUOTED_RE.finditer(text):
            span = match.group(1)
            if len(span.split()) < 3:
                continue
            count += 1
            norm = _norm(span).rstrip(". ,")
            hit: Quote | None = next((q for qn, q in quote_texts if norm and norm in qn), None)
            if hit is None:
                issues.append(Issue("invented_quote", BLOCK, location,
                                    f'quoted text "{span[:90]}" does not match any verbatim source quote; '
                                    "quote exactly or paraphrase without quotation marks"))
            elif hit.speaker and not _token_in(_norm(hit.speaker.split()[-1]), _norm(text)):
                issues.append(Issue("quote_speaker_missing", FIX, location,
                                    f'the quote "{span[:60]}" must be attributed to {hit.speaker} in the same paragraph'))
    return count


def _cased_tokens(text: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for raw in text.split():
        capital = raw[:1].isupper() or raw[:1].isdigit() or raw[:1] == "$"
        out.extend((tok, capital) for tok in _norm(raw).split())
    return out


def _check_copying(ctx: _Context, issues: list[Issue]) -> dict[str, Any]:
    """Flag runs of source wording; runs made mostly of names, titles and figures are allowed."""
    copied_words = 0
    total_words = 0
    longest = 0
    for location, text, _ in _units(ctx.article):
        cased = _cased_tokens(_strip_quotes(text))
        words = [t for t, _ in cased]
        total_words += len(words)
        if len(words) < COPY_SHINGLE:
            continue
        covered = [False] * len(words)
        for i, shingle in enumerate(_shingles(words, COPY_SHINGLE)):
            if shingle in ctx.source_shingles:
                for j in range(i, i + COPY_SHINGLE):
                    covered[j] = True
        runs: list[tuple[int, int]] = []
        start = None
        for i, flag in enumerate(covered + [False]):
            if flag and start is None:
                start = i
            elif not flag and start is not None:
                runs.append((start, i))
                start = None
        worst: tuple[int, str] | None = None
        for a, b in runs:
            length = b - a
            names_share = sum(1 for _, cap in cased[a:b]) and sum(1 for _, cap in cased[a:b] if cap) / length
            if names_share >= 0.5:
                continue
            copied_words += length
            longest = max(longest, length)
            if length >= COPY_RUN_FIX and (worst is None or length > worst[0]):
                worst = (length, " ".join(words[a:b]))
        if worst:
            issues.append(Issue("copied_phrasing", FIX, location,
                                f'{worst[0]} consecutive words copied from a source ("{worst[1][:120]}"); '
                                "rewrite in original wording, keeping the facts"))
    ratio = round(copied_words / total_words, 3) if total_words else 0.0
    if ratio > 0.15:
        issues.append(Issue("high_copy_ratio", WARN, "article",
                            f"{ratio:.0%} of the article's wording matches sources in 8-word runs"))
    return {"copy_ratio": ratio, "longest_copied_run": longest}


def _check_duplication(article: Article, issues: list[Issue]) -> None:
    seen: list[tuple[str, str]] = []
    for location, para in article.all_paragraphs():
        for sentence in split_sentences(para.text):
            if len(sentence.split()) < 10:
                continue
            for other_loc, other in seen:
                if fuzz.ratio(sentence.lower(), other.lower()) >= DUPLICATE_SENTENCE_RATIO:
                    issues.append(Issue("repeated_sentence", FIX, location,
                                        f'repeats a sentence from {other_loc}: "{sentence[:80]}"; say something new or cut it'))
                    break
            seen.append((location, sentence))
    body_texts = [(loc, p.text) for loc, p in article.all_paragraphs() if loc.startswith("section")]
    for location, para in article.all_paragraphs():
        if location.startswith("section"):
            continue
        for body_loc, body_text in body_texts:
            if fuzz.token_set_ratio(para.text, body_text) >= 88 and len(para.text.split()) >= 25:
                issues.append(Issue("closing_copies_body", FIX, location,
                                    f"closely restates {body_loc}; closing sections must summarize, not repeat"))
                break


def _check_style_and_junk(article: Article, issues: list[Issue]) -> None:
    for location, text, _ in _units(article):
        prose = _strip_quotes(text)
        lowered = prose.lower()
        if _MARKUP_RE.search(text):
            issues.append(Issue("markup_artifact", BLOCK, location, "contains markup or a link; write plain prose"))
        if _URL_RE.search(_PUBLICATION_DOMAINS_RE.sub(" ", prose)):
            issues.append(Issue("web_address", FIX, location,
                                "names an outlet by web address; use the publication name"))
        for sentence in split_sentences(prose):
            if has_chrome_phrase(sentence):
                issues.append(Issue("site_junk", BLOCK, location, f'reads like website boilerplate: "{sentence[:80]}"'))
        if _FIRST_PERSON_RE.search(prose):
            issues.append(Issue("first_person", FIX, location, "uses first person outside a quote"))
        if not location.endswith("question") and _READER_RE.search(prose):
            issues.append(Issue("reader_address", FIX, location, "addresses the reader directly"))
        if not location.endswith("question") and prose.rstrip().endswith("?"):
            issues.append(Issue("rhetorical_question", FIX, location, "ends with a rhetorical question"))
        for phrase in HYPE_PHRASES:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                issues.append(Issue("hype_language", FIX, location, f'remove the filler/hype phrase "{phrase}"'))
        for phrase in JUDGMENT_WORDS:
            if re.search(rf"\b{re.escape(phrase)}\b", lowered):
                issues.append(Issue("unattributed_judgment", WARN, location,
                                    f'"{phrase}" is a judgment; make sure a source said it and attribute it'))


def run_qa(article: Article, bank: FactBank, dossier: ResearchDossier | None = None) -> QAReport:
    ctx = _Context(article, bank, dossier)
    issues: list[Issue] = []
    if article.body_words < BODY_MIN_WORDS:
        issues.append(Issue("too_short", BLOCK, "body", f"body is {article.body_words} words; minimum is {BODY_MIN_WORDS}"))
    quote_map = dict(usable_quotes(bank))
    for message in structural_issues(article, ctx.facts, quote_map):
        if "body is" in message or "quotation marks" in message:
            continue
        issues.append(Issue("structure", WARN if message.startswith(("meta_", "use three", "slug", "category")) else FIX,
                            "structure", message))
    grounding = _check_grounding(ctx, issues)
    quotes_checked = _check_quotes(ctx, issues)
    copying = _check_copying(ctx, issues)
    _check_duplication(article, issues)
    _check_style_and_junk(article, issues)
    report = QAReport(issues=_dedupe(issues))
    report.metrics = {
        "body_words": article.body_words,
        "total_words": article.total_words,
        "facts_cited": len(article.cited_facts()),
        "quotes_checked": quotes_checked,
        **grounding,
        **copying,
    }
    return report


def _dedupe(issues: list[Issue]) -> list[Issue]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Issue] = []
    for issue in issues:
        key = (issue.code, issue.location, issue.message)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out
