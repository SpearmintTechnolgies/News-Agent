"""Editorial structure helpers for WordPress HTML formatting.

Pure structural transforms only: split long paragraphs, normalize bold
closing markers into H2/H3 markdown, infer descriptive H2s from existing
prose. Does not invent facts or URLs.
"""

from __future__ import annotations

import re

_MAX_PARAGRAPH_WORDS = 110
_TARGET_PARAGRAPH_WORDS = 80
_INTRO_MAX_WORDS = 120
_SECTION_TARGET_WORDS = 150

CONCLUSION_TITLE = "Conclusion / What Happens Next"
FAQ_TITLE = "FAQs"

_BOLD_CONCLUSION_RE = re.compile(
    r"\*\*\s*(Conclusion(?:\s*/\s*What Happens Next)?)\s*\*\*",
    re.IGNORECASE,
)
_BOLD_FAQ_RE = re.compile(
    r"\*\*\s*(FAQs?|Frequently Asked Questions)\s*\*\*",
    re.IGNORECASE,
)
_Q_PREFIX_RE = re.compile(
    r"^\*\*\s*Q:\s*(.+?)\s*\*\*\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_A_PREFIX_RE = re.compile(
    r"^\*\*\s*A:\s*(.+?)\s*\*\*\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_BOLD_QUESTION_RE = re.compile(
    r"\*\*\s*(?:Q:\s*)?([^*?\n][^?*\n]*\?)\s*\*\*"
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_ABBREV_RE = re.compile(
    r"\b(?:a\.m|p\.m|mr|mrs|ms|dr|jr|sr|vs|inc|ltd|u\.s)\.",
    re.IGNORECASE,
)
_PARAGRAPH_SPLIT_RE = re.compile(r"\n\n+")


def word_count(text: str) -> int:
    return len(text.split()) if text else 0


def split_sentences(text: str) -> list[str]:
    if not text or not text.strip():
        return []
    placeholders: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"@@ABBREV{len(placeholders) - 1}@@"

    protected = _ABBREV_RE.sub(_protect, text.strip())
    parts = _SENTENCE_SPLIT_RE.split(protected)
    restored: list[str] = []
    for part in parts:
        item = part
        for index, original in enumerate(placeholders):
            item = item.replace(f"@@ABBREV{index}@@", original)
        item = item.strip()
        if item:
            restored.append(item)
    return restored


def chunk_sentences(
    sentences: list[str],
    *,
    max_words: int = _MAX_PARAGRAPH_WORDS,
    target_words: int = _TARGET_PARAGRAPH_WORDS,
) -> list[str]:
    if not sentences:
        return []
    paragraphs: list[str] = []
    current: list[str] = []
    current_words = 0
    for sentence in sentences:
        sw = word_count(sentence)
        if current and current_words + sw > max_words:
            paragraphs.append(" ".join(current).strip())
            current = [sentence]
            current_words = sw
        else:
            current.append(sentence)
            current_words += sw
            if current_words >= target_words:
                paragraphs.append(" ".join(current).strip())
                current = []
                current_words = 0
    if current:
        paragraphs.append(" ".join(current).strip())
    return [p for p in paragraphs if p]


def split_long_plain_block(block: str) -> list[str]:
    if block.startswith("#") or block.startswith("<"):
        return [block]
    if word_count(block) <= _MAX_PARAGRAPH_WORDS:
        return [block]
    return chunk_sentences(split_sentences(block))


_DAY_OPENER_RE = re.compile(
    r"^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day(?:'s)?\s+",
    re.IGNORECASE,
)
_NARRATIVE_OPENER_RE = re.compile(
    r"^(?:The|A|An)\s+"
    r"(?:activity|surge|move|rally|drop|decline|gain|loss|inflow|outflow|"
    r"funds?|data|report|announcement|increase|decrease)\s+"
    r"(?:follows?|followed|represented?|arrived?|came|marked|showed|"
    r"indicated|suggests?|pointed)\s+(?:a\s+|an\s+|the\s+)?",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"\$[\d,]+(?:\.\d+)?(?:\s*(?:billion|million|thousand|bn|m))?",
    re.IGNORECASE,
)
_TICKER_RE = re.compile(r"\b(?:IBIT|ARKB|FBTC|BITB|GBTC|ETHA|EZET)\b")
# Topic lemmas that may appear in grounded prose (matched case-insensitively).
_TOPIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Spot Bitcoin ETFs", re.compile(r"\bspot\s+bitcoin\s+(?:etfs?|exchange-traded\s+funds?)\b", re.I)),
    ("Bitcoin ETFs", re.compile(r"\bbitcoin\s+(?:etfs?|exchange-traded\s+funds?)\b", re.I)),
    ("Ether ETFs", re.compile(r"\b(?:ether|ethereum|eth)\s+(?:etfs?|exchange-traded\s+funds?)\b", re.I)),
    ("XRP ETFs", re.compile(r"\bxrp\s+(?:etfs?|exchange-traded\s+funds?)\b", re.I)),
    ("Bitcoin", re.compile(r"\bbitcoin\b", re.I)),
    ("Ether", re.compile(r"\b(?:ether|ethereum)\b", re.I)),
    ("XRP", re.compile(r"\bxrp\b", re.I)),
    ("ETFs", re.compile(r"\b(?:etfs?|exchange-traded\s+funds?)\b", re.I)),
]
_FLOW_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Inflows", re.compile(r"\binflows?\b", re.I)),
    ("Outflows", re.compile(r"\boutflows?\b", re.I)),
]
_PRICE_RE = re.compile(r"\b(?:price|traded|trading|climbed|rose|fallen|fell)\b", re.I)
_ENTITY_RE = re.compile(
    r"\b(?:BlackRock|Fidelity|Coinbase|Grayscale|ARK\s*21Shares|Farside|"
    r"CoinGecko|CryptoQuant|Federal\s+Reserve|Senate)\b"
)


def _title_case_token(token: str) -> str:
    if not token:
        return token
    if token.startswith("$") or token.isupper() and len(token) <= 5:
        return token
    if token.lower() in {"etf", "etfs", "btc", "eth", "xrp", "us", "uk"}:
        return token.upper() if token.lower() != "etfs" else "ETFs"
    if "-" in token:
        return "-".join(_title_case_token(p) for p in token.split("-"))
    return token[:1].upper() + token[1:]


def _title_case_phrase(words: list[str]) -> str:
    return " ".join(_title_case_token(w) for w in words if w)


def _normalize_money(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw.strip())
    parts = text.split(" ", 1)
    if len(parts) == 2:
        return f"{parts[0]} {_title_case_token(parts[1])}"
    return parts[0]


# Relationship verbs already present in grounded prose (forms must match body).
_RELATION_VERBS = frozenset({
    "attracted", "attracting", "recorded", "recording", "reached", "reaching",
    "posted", "posting", "led", "leading", "followed", "following", "climbed",
    "climbing", "risen", "rose", "rising", "extended", "extending", "surged",
    "surging", "traded", "trading", "drew", "drawing", "marked", "marking",
    "moved", "moving", "saw", "seeing", "gained", "gaining", "fell", "fallen",
    "falling", "outperformed", "outperforming", "hit", "hits", "hitting",
    "confirmed", "confirm", "snap", "snapped", "recovered",
})

# Noun-only flow labels that must not form "Topic Inflows $X" metric titles.
_FLOW_NOUNS = frozenset({"inflow", "inflows", "outflow", "outflows"})

_INCOMPLETE_TRAILING = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "as",
    "at", "by", "from", "into", "over", "after", "before", "largest", "biggest",
    "smallest", "highest", "lowest", "since", "amid", "nearly", "almost",
})



_HEADING_STOP = frozenset({
    "a", "an", "the", "and", "or", "but", "as", "of", "to", "in", "on", "for",
    "with", "by", "from", "into", "over", "after", "before", "its", "their",
    "this", "that", "these", "those", "at", "than", "then", "also", "about",
    "above", "below", "under", "near", "across", "amid", "despite", "while",
    "when", "where", "which", "who", "whom", "whose", "been", "being", "be",
    "is", "are", "was", "were", "has", "have", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "can", "not", "no",
    "so", "if", "up", "out", "off", "per", "via", "vs", "versus", "recent",
    "approximately", "around", "nearly", "almost", "briefly", "significant",
    "activity", "according", "data", "time", "past", "during",
})

_NOUN_PILE_TAILS = frozenset({
    "recovery", "rally", "surge", "decline", "drop", "gain", "loss",
    "interest", "volatility", "streak", "tally", "haul",
})

_PRICE_VERB_RE = re.compile(
    r"\b(climbed|climb|climbs|climbing|rose|rise|rises|rising|risen|"
    r"fell|fall|falls|falling|fallen|traded|trade|trades|trading|"
    r"surged|surge|surges|surging|gained|gain|gains|gaining|"
    r"reached|reach|reaches|reaching|moved|move|moves|moving|"
    r"hit|hits|hitting)\b",
    re.IGNORECASE,
)

_ABOVE_MONEY_RE = re.compile(
    r"\b(?:climbed|rose|risen|moved|surged|traded|reached|hit)\s+above\s+"
    r"(\$[\d,]+(?:\.\d+)?(?:\s*(?:billion|million|thousand|bn|m))?)",
    re.IGNORECASE,
)
_TRADED_AT_RE = re.compile(
    r"\b(?:traded|trading|trades)\s+at\s+"
    r"(\$[\d,]+(?:\.\d+)?(?:\s*(?:billion|million|thousand|bn|m))?)",
    re.IGNORECASE,
)


def _stem_token(tok: str) -> str:
    t = tok.lower().strip(".,:;!?\"'")
    if t.startswith("$") or any(ch.isdigit() for ch in t):
        return t
    for suf in ("ing", "ied", "ed", "ies", "es", "s"):
        if len(t) > len(suf) + 2 and t.endswith(suf):
            return t[: -len(suf)]
    return t


def _content_tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9$][A-Za-z0-9$.,%-]*", text or "")
    out: list[str] = []
    for tok in raw:
        low = tok.lower().strip(".,:;!?")
        if not low or low in _HEADING_STOP:
            continue
        if len(low) <= 1 and not low.startswith("$"):
            continue
        out.append(_stem_token(low))
    return out


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    if not needle:
        return False
    i = 0
    for tok in haystack:
        if tok == needle[i]:
            i += 1
            if i == len(needle):
                return True
    return False


def _has_money_token(lower_words: list[str]) -> bool:
    return any(w.startswith("$") or bool(re.search(r"\d", w)) for w in lower_words)


def _is_noun_pile_heading(lower_words: list[str]) -> bool:
    """Reject adjective/participle noun stacks like 'Bitcoin Extended Price Recovery'."""
    if not lower_words:
        return True
    if lower_words[-1] in _NOUN_PILE_TAILS:
        modifiers = {"extended", "extending", "recent", "price", "continued", "ongoing"}
        if any(w in modifiers for w in lower_words[:-1]):
            return True
        if lower_words[-1] == "recovery" and not _has_money_token(lower_words):
            verbs = [w for w in lower_words if w in _RELATION_VERBS]
            if not verbs or all(v.startswith("extend") for v in verbs):
                return True
    if 3 <= len(lower_words) <= 6 and not _has_money_token(lower_words):
        mid = lower_words[1:-1]
        participle_like = {
            "extended", "extending", "posted", "posting", "recorded", "recording",
        }
        if any(m in participle_like for m in mid) and lower_words[-1] not in _FLOW_NOUNS:
            if lower_words[-1] in _NOUN_PILE_TAILS or lower_words[-1] == "price":
                return True
    return False


def heading_overlaps_body_phrase(heading: str, section_text: str) -> bool:
    """True when H2 largely duplicates/repackages a nearby body phrase.

    Allows short subject+verb+(flow|metric) compressions; rejects noun-pile
    repackaging of a single sentence phrase (e.g. recovery stacks).
    """
    h_toks = _content_tokens(heading)
    if len(h_toks) < 3:
        return False

    lower_words = [w.lower().strip(".,:;!?") for w in heading.split()]
    has_verb = any(
        w in _RELATION_VERBS or w.rstrip("s") in _RELATION_VERBS
        for w in lower_words
        if w not in _FLOW_NOUNS
    )
    has_complement = any(w in _FLOW_NOUNS for w in lower_words) or _has_money_token(lower_words)
    # Proper editorial SVO compressions are allowed even when token-overlapping
    if has_verb and has_complement and not _is_noun_pile_heading(lower_words):
        return False
    if _is_noun_pile_heading(lower_words):
        # Noun piles that match a nearby phrase are always overlaps
        pass

    h_bi = _ngrams(h_toks, 2)
    h_tri = _ngrams(h_toks, 3)
    for sent in split_sentences(section_text):
        s_toks = _content_tokens(sent)
        if len(s_toks) < 3:
            continue
        s_bi = _ngrams(s_toks, 2)
        bi_ratio = (len(h_bi & s_bi) / len(h_bi)) if h_bi else 0.0
        tri_ratio = 0.0
        if h_tri:
            s_tri = _ngrams(s_toks, 3)
            if s_tri:
                tri_ratio = len(h_tri & s_tri) / len(h_tri)

        # Strong phrase copy: high n-gram overlap or content subsequence
        if bi_ratio >= 0.6 or tri_ratio >= 0.5 or (
            len(h_toks) >= 3 and _is_subsequence(h_toks, s_toks)
        ):
            return True

        if len(h_toks) >= 3:
            h_set = set(h_toks)
            for i in range(len(s_toks)):
                window = set(s_toks[i : i + len(h_toks) + 1])
                if not window:
                    continue
                jacc = len(h_set & window) / len(h_set | window)
                if jacc >= 0.75 and len(h_set & window) >= 3:
                    return True
    return False



def _flow_dominance(text: str) -> str:
    """Return 'Inflows', 'Outflows', or '' based on grounded dominance (not contrast-only)."""
    primary = re.split(r"(?i)\b(?:however|but|yet|although|though)\b", text)[0]
    in_n = len(re.findall(r"(?i)\binflows?\b", primary))
    out_n = len(re.findall(r"(?i)\boutflows?\b", primary))
    in_all = len(re.findall(r"(?i)\binflows?\b", text))
    out_all = len(re.findall(r"(?i)\boutflows?\b", text))
    if out_all and not out_n and in_all >= out_all:
        return "Inflows" if in_all else ""
    if out_all > in_all:
        return "Outflows"
    if in_all > out_all:
        return "Inflows"
    if in_all and out_all == in_all:
        m_in = re.search(r"(?i)\binflows?\b", primary)
        m_out = re.search(r"(?i)\boutflows?\b", primary)
        if m_out and (not m_in or m_out.start() < m_in.start()):
            return "Outflows"
        if m_in:
            return "Inflows"
    if in_all:
        return "Inflows"
    if out_all:
        return "Outflows"
    return ""


def _heading_from_price_action(section_text: str, topic_label: str) -> str:
    """Subject + price verb (+ above/at metric) from grounded price prose."""
    if not topic_label:
        return ""
    m_above = _ABOVE_MONEY_RE.search(section_text)
    if m_above:
        verb = re.search(
            r"\b(climbed|rose|risen|moved|surged|traded|reached|hit)\b",
            m_above.group(0),
            re.I,
        )
        if verb:
            money = _normalize_money(m_above.group(1))
            title = _finalize_heading_words(
                f"{topic_label} {verb.group(1)} Above {money}".split()
            )
            if title and not is_rejected_heading(title):
                return title
    m_at = _TRADED_AT_RE.search(section_text)
    if m_at:
        money = _normalize_money(m_at.group(1))
        title = _finalize_heading_words(f"{topic_label} Traded At {money}".split())
        if title and not is_rejected_heading(title):
            return title
    for sent in split_sentences(section_text):
        if not _PRICE_VERB_RE.search(sent):
            continue
        vm = _PRICE_VERB_RE.search(sent)
        if not vm:
            continue
        after = sent[vm.end():]
        money_m = _MONEY_RE.search(after)
        if money_m and money_m.start() <= 56:
            money = _normalize_money(money_m.group(0))
            bridge = after[: money_m.start()].lower()
            if "above" in bridge:
                raw = f"{topic_label} {vm.group(1)} Above {money}"
            elif re.search(r"\bat\b", bridge):
                raw = f"{topic_label} {vm.group(1)} At {money}"
            elif re.search(r"\bto\b", bridge):
                raw = f"{topic_label} {vm.group(1)} To {money}"
            else:
                raw = f"{topic_label} {vm.group(1)} {money}"
            title = _finalize_heading_words(raw.split())
            if title and not is_rejected_heading(title):
                return title
    return ""


def is_rejected_heading(title: str) -> bool:
    """Reject metric fragments, noun piles, entity+number, sentence scraps.

    Examples rejected: "Bitcoin $85,430", "Bitcoin Extended Price Recovery",
    "Spot Bitcoin ETFs Inflows $1.31 Billion".
    """
    cleaned = re.sub(r"\s+", " ", str(title or "")).strip()
    if not cleaned:
        return True
    words = cleaned.split()
    if len(words) > 10 or len(cleaned) > 72:
        return True
    if len(words) < 2:
        return True
    if len(words) > 8:
        return True

    lower_words = [w.lower().strip(".,:;!?") for w in words]
    has_verb = any(
        w in _RELATION_VERBS or w.rstrip("s") in _RELATION_VERBS
        for w in lower_words
        if w not in _FLOW_NOUNS
    )

    has_money = bool(re.search(r"\$[\d,]", cleaned))
    has_pct = bool(re.search(r"\d+%", cleaned))
    has_bare_num = bool(re.search(r"\b\d[\d,.]*\b", cleaned))
    has_number = has_money or has_pct or has_bare_num

    if has_number and not has_verb:
        return True

    if has_money and any(w in _FLOW_NOUNS for w in lower_words) and not has_verb:
        return True

    if has_money and re.match(
        r"(?i)^[A-Za-z][\w\s.-]*\s+\$[\d,]+(?:\.\d+)?(?:\s*(?:Billion|Million|Thousand|Bn|M))?$",
        cleaned,
    ):
        before_money = cleaned[: cleaned.index("$")]
        if not any(
            w.lower().strip(".,") in _RELATION_VERBS
            for w in before_money.split()
        ):
            return True

    if _is_noun_pile_heading(lower_words):
        return True

    joined = " ".join(lower_words)
    if joined.startswith(("monday", "tuesday", "wednesday", "thursday", "friday")):
        return True
    if "represented the" in joined or "follows a period" in joined:
        return True
    if lower_words[-1] in _INCOMPLETE_TRAILING:
        return True
    if lower_words[-1] in _RELATION_VERBS and not any(
        w in _FLOW_NOUNS or w.startswith("$") or any(ch.isdigit() for ch in w)
        for w in lower_words[:-1]
    ):
        if len(lower_words) <= 3:
            return True

    return False



def _strip_heading_openers(lead: str) -> str:
    lead = _DAY_OPENER_RE.sub("", lead.strip())
    lead = _NARRATIVE_OPENER_RE.sub("", lead)
    lead = re.sub(
        r"^(?:activity|surge|move)\s+(?:represented|marked|was)\s+(?:the\s+)?",
        "",
        lead,
        flags=re.IGNORECASE,
    ).strip()
    return lead


def _finalize_heading_words(words: list[str]) -> str:
    filler = {
        "a", "an", "the", "this", "that", "these", "those", "its", "their",
        "and", "or", "but", "as", "of", "to", "in", "on", "for", "with",
        "by", "from", "into", "over", "after", "before",
    }
    while words and words[0].lower().strip(".,") in filler:
        words = words[1:]
    if len(words) > 8:
        words = words[:8]
    title = _title_case_phrase(words).strip()
    title = re.sub(r"[.:;,\-\u2013\u2014\"']+$", "", title).strip()
    while title.split() and title.split()[-1].lower().strip(".,") in _INCOMPLETE_TRAILING:
        title = " ".join(title.split()[:-1]).strip()
    if word_count(title) > 10 or len(title) > 72:
        title = _title_case_phrase(title.split()[:6])
    return title


def _heading_from_relation_sentence(sent: str, *, section_text: str = "") -> str:
    """Build a concise editorial H2 from a sentence that already has a relation verb."""
    lead = _strip_heading_openers(sent)
    if "," in lead:
        first_clause = lead.split(",", 1)[0].strip()
        clause_verbs = {w.lower().strip(".,") for w in first_clause.split()}
        if 3 <= word_count(first_clause) <= 12 and (_RELATION_VERBS & clause_verbs):
            lead = first_clause

    topic_label = ""
    for label, pattern in _TOPIC_PATTERNS:
        if pattern.search(lead) or pattern.search(section_text or sent):
            topic_label = label
            break

    verb_match = re.search(
        r"\b("
        + "|".join(re.escape(v) for v in sorted(_RELATION_VERBS, key=len, reverse=True))
        + r")\b",
        lead,
        flags=re.IGNORECASE,
    )
    if not verb_match:
        return ""
    verb = verb_match.group(1)
    after = lead[verb_match.end():].strip()

    if verb.lower().startswith("extend") and re.search(r"\brecovery\b", lead, re.I):
        return ""

    money = ""
    m_money = _MONEY_RE.search(after) if after else None
    if m_money and m_money.start() <= 48:
        money = _normalize_money(m_money.group(0))

    flow = ""
    for label, pattern in _FLOW_PATTERNS:
        if pattern.search(lead):
            flow = label
            break

    entities = _ENTITY_RE.findall(lead)
    tickers = _TICKER_RE.findall(lead)
    candidates: list[str] = []

    if (entities or tickers) and verb:
        head = entities[0] if entities else tickers[0]
        if entities and tickers and tickers[0].lower() not in head.lower():
            head = f"{entities[0]} {tickers[0]}"
        if flow:
            candidates.append(f"{head} {verb} {flow}")
        elif money:
            candidates.append(f"{head} {verb} {money}")

    money_verbs = {
        "reached", "reaching", "attracted", "attracting", "posted", "posting",
        "recorded", "recording", "hit", "hits", "hitting", "drew", "drawing",
        "saw", "seeing", "climbed", "climbing", "rose", "risen", "rising",
        "traded", "trading", "surged", "surging", "moved", "moving",
    }
    if topic_label and verb:
        if flow and money and verb.lower() in money_verbs:
            candidates.append(f"{topic_label} {verb} {money}")
            candidates.append(f"{topic_label} {verb} {flow}")
        elif flow:
            candidates.append(f"{topic_label} {verb} {flow}")
        elif money and verb.lower() in money_verbs:
            candidates.append(f"{topic_label} {verb} {money}")
        elif re.search(r"\binflows?\b", sent, re.I):
            candidates.append(f"{topic_label} {verb} Inflows")
        elif re.search(r"\boutflows?\b", sent, re.I):
            candidates.append(f"{topic_label} {verb} Outflows")

    ctx = section_text or sent
    dominant_flow = _flow_dominance(ctx)

    def _tokens_grounded(title: str) -> bool:
        sent_l = (section_text or sent).lower()
        for tok in re.findall(r"[A-Za-z0-9$]+", title):
            tl = tok.lower()
            if tl in {"etf", "etfs"} and ("etf" in sent_l or "exchange-traded" in sent_l):
                continue
            if tl in {"above", "at", "to"}:
                continue
            if tl in sent_l or tl.rstrip("s") in sent_l:
                continue
            if topic_label and any(tl == p.lower() for p in topic_label.split()):
                continue
            return False
        return True

    for raw in candidates:
        words = re.findall(r"[A-Za-z0-9$][A-Za-z0-9$.,%-]*", raw)
        title = _finalize_heading_words(words)
        if not title or not _tokens_grounded(title) or is_rejected_heading(title):
            continue
        low = title.lower()
        if "outflow" in low and dominant_flow != "Outflows":
            continue
        if "inflow" in low and dominant_flow == "Outflows":
            continue
        if heading_overlaps_body_phrase(title, ctx):
            continue
        return title
    return ""



def derive_section_heading(text: str) -> str:
    """Short editorial H2 from existing section tokens only — no new facts.

    Builds natural subject + verb (+ object/metric) labels. Rejects metric-only
    titles, noun piles, and headings that merely repackage a nearby body phrase.
    """
    sentences = split_sentences(text)
    if not sentences:
        return "Key Developments"

    window = " ".join(sentences[:6]).strip()
    section_text = " ".join(sentences).strip() or text

    topic_label = ""
    for label, pattern in _TOPIC_PATTERNS:
        if pattern.search(window) or pattern.search(section_text):
            topic_label = label
            break

    def _accept(title: str) -> bool:
        if not title or is_rejected_heading(title):
            return False
        if heading_overlaps_body_phrase(title, section_text):
            return False
        wc = word_count(title)
        if wc < 3 or wc > 8:
            return False
        return True

    price_hits = len(_PRICE_VERB_RE.findall(window)) + len(_PRICE_RE.findall(window))
    money_hits = len(_MONEY_RE.findall(window))
    if topic_label and price_hits and money_hits:
        title = _heading_from_price_action(section_text, topic_label)
        if _accept(title):
            return title

    for sent in sentences[:6]:
        if not any(v in sent.lower() for v in _RELATION_VERBS):
            continue
        if re.search(r"\bextended?\b", sent, re.I) and re.search(r"\brecovery\b", sent, re.I):
            continue
        title = _heading_from_relation_sentence(sent, section_text=section_text)
        if _accept(title):
            return title

    flow_label = _flow_dominance(section_text)
    window_verbs = [
        v for v in _RELATION_VERBS
        if re.search(rf"\b{re.escape(v)}\b", window, re.I)
    ]
    preferred_verbs = [
        v for v in window_verbs
        if v.lower() in {
            "attracted", "attracting", "reached", "reaching", "recorded", "recording",
            "posted", "posting", "drew", "drawing", "saw", "seeing", "led", "leading",
            "climbed", "climbing", "rose", "risen", "traded", "trading", "surged",
        }
    ]
    verb_choice = (preferred_verbs or window_verbs or [""])[0]
    if topic_label and flow_label and verb_choice:
        money = ""
        for sent in sentences[:6]:
            if (
                flow_label.lower().rstrip("s") in sent.lower()
                or verb_choice.lower() in sent.lower()
            ):
                mm = _MONEY_RE.search(sent)
                if mm:
                    money = _normalize_money(mm.group(0))
                    break
        if money and verb_choice.lower() in {
            "reached", "reaching", "attracted", "attracting", "recorded", "recording",
            "posted", "posting", "drew", "drawing", "saw", "seeing", "hit", "hits",
        }:
            title = _title_case_phrase(f"{topic_label} {verb_choice} {money}".split())
            if _accept(title):
                return title
        title = _title_case_phrase(f"{topic_label} {verb_choice} {flow_label}".split())
        if _accept(title):
            return title

    tickers = _TICKER_RE.findall(window)
    entities = _ENTITY_RE.findall(window)
    if (entities or tickers) and verb_choice:
        head = entities[0] if entities else tickers[0]
        if entities and tickers and tickers[0].lower() not in head.lower():
            head = f"{entities[0]} {tickers[0]}"
        tail = flow_label or ""
        title = _title_case_phrase(f"{head} {verb_choice} {tail}".split())
        if _accept(title):
            return title

    if topic_label:
        title = _heading_from_price_action(section_text, topic_label)
        if _accept(title):
            return title

    lead = _strip_heading_openers(sentences[0])
    if "," in lead:
        first_clause = lead.split(",", 1)[0].strip()
        if 3 <= word_count(first_clause) <= 12:
            lead = first_clause
    words = [w for w in re.findall(r"[A-Za-z0-9$][A-Za-z0-9$.,%-]*", lead)]
    title = _finalize_heading_words(words)
    if not _accept(title):
        words_no_num = [
            w for w in words
            if not re.match(r"^\$?\d", w)
            and w.lower() not in {"billion", "million", "thousand"}
        ]
        title = _finalize_heading_words(words_no_num)
    if not _accept(title):
        if topic_label and flow_label:
            title = _title_case_phrase(f"{topic_label} {flow_label}".split())
            if (
                title
                and not re.search(r"\$|\d", title)
                and 2 <= word_count(title) <= 8
                and not heading_overlaps_body_phrase(title, section_text)
            ):
                return title
        return "Key Developments"
    return title or "Key Developments"



def normalize_structural_markers(content: str) -> str:
    text = content.replace("\r\n", "\n").replace("\r", "\n")
    text = _BOLD_CONCLUSION_RE.sub(f"\n\n## {CONCLUSION_TITLE}\n\n", text)
    text = _BOLD_FAQ_RE.sub(f"\n\n## {FAQ_TITLE}\n\n", text)
    text = _Q_PREFIX_RE.sub(lambda m: f"### {m.group(1).strip()}", text)
    text = _A_PREFIX_RE.sub(lambda m: m.group(1).strip(), text)
    text = _BOLD_QUESTION_RE.sub(
        lambda m: f"\n\n### {m.group(1).strip()}\n\n",
        text,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def partition_closing(content: str) -> tuple[str, str, str]:
    concl_match = re.search(
        r"(?im)^##\s+Conclusion(?:\s*/\s*What Happens Next)?\s*$",
        content,
    )
    faq_match = re.search(
        r"(?im)^##\s+(?:FAQs?|Frequently Asked Questions)\s*$",
        content,
    )
    indices: list[tuple[str, int]] = []
    if concl_match:
        indices.append(("conclusion", concl_match.start()))
    if faq_match:
        indices.append(("faq", faq_match.start()))
    indices.sort(key=lambda x: x[1])
    if not indices:
        return content.strip(), "", ""

    first_kind, first_pos = indices[0]
    main = content[:first_pos].strip()
    rest = content[first_pos:]
    if len(indices) == 1:
        if first_kind == "conclusion":
            return main, rest.strip(), ""
        return main, "", rest.strip()

    _second_kind, second_abs = indices[1]
    second_rel = second_abs - first_pos
    first_block = rest[:second_rel].strip()
    second_block = rest[second_rel:].strip()
    if first_kind == "conclusion":
        return main, first_block, second_block
    return main, second_block, first_block


def ensure_conclusion_h2(conclusion_block: str) -> str:
    if not conclusion_block.strip():
        return ""
    text = conclusion_block.strip()
    if not re.match(r"(?im)^##\s+Conclusion", text):
        text = f"## {CONCLUSION_TITLE}\n\n{text}"
    else:
        text = re.sub(
            r"(?im)^##\s+Conclusion(?:\s*/\s*What Happens Next)?\s*$",
            f"## {CONCLUSION_TITLE}",
            text,
            count=1,
        )
    parts: list[str] = []
    for block in _PARAGRAPH_SPLIT_RE.split(text):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            parts.append(block)
        else:
            parts.extend(split_long_plain_block(block))
    return "\n\n".join(parts).strip()


def ensure_faq_h3(faq_block: str) -> str:
    if not faq_block.strip():
        return ""
    lines = faq_block.strip().split("\n")
    out: list[str] = [f"## {FAQ_TITLE}", ""]
    buffer: list[str] = []

    def flush_buffer() -> None:
        nonlocal buffer
        text = " ".join(x.strip() for x in buffer if x.strip()).strip()
        buffer = []
        if not text:
            return
        if text.endswith("?") and not text.startswith("#"):
            out.append(f"### {text}")
            out.append("")
        else:
            out.append(text)
            out.append("")

    for line in lines:
        stripped = line.strip()
        if re.match(r"(?i)^##\s+(?:FAQs?|Frequently Asked Questions)\s*$", stripped):
            continue
        if stripped.startswith("### "):
            flush_buffer()
            out.append(stripped)
            out.append("")
            continue
        buffer.append(stripped)
    flush_buffer()
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


def sectionize_main_body(main: str) -> tuple[str, str]:
    main = main.strip()
    if not main:
        return "", ""

    existing_h2 = bool(re.search(r"(?m)^##\s+\S", main))
    expanded: list[str] = []
    for block in _PARAGRAPH_SPLIT_RE.split(main):
        block = block.strip()
        if not block:
            continue
        if block.startswith("#"):
            expanded.append(block)
        else:
            expanded.extend(split_long_plain_block(block))

    if existing_h2:
        return "", "\n\n".join(expanded).strip()

    plain_paras = [b for b in expanded if not b.startswith("#")]
    if not plain_paras:
        return "", "\n\n".join(expanded).strip()

    intro_parts: list[str] = []
    intro_words = 0
    rest: list[str] = []
    for i, para in enumerate(plain_paras):
        wc = word_count(para)
        if not rest and (intro_words + wc <= _INTRO_MAX_WORDS or not intro_parts):
            intro_parts.append(para)
            intro_words += wc
            if intro_words >= min(90, _INTRO_MAX_WORDS) or (intro_words >= 60 and i >= 1):
                rest = plain_paras[i + 1 :]
                break
        else:
            rest = plain_paras[i:]
            break
    else:
        rest = []

    intro = "\n\n".join(intro_parts).strip()

    if not rest:
        if word_count(intro) >= 80:
            sents = split_sentences(intro)
            mid = max(1, len(sents) // 2)
            intro = " ".join(sents[:mid]).strip()
            rest = chunk_sentences(sents[mid:])
        else:
            return intro, ""

    sections: list[tuple[str, list[str]]] = []
    bucket: list[str] = []
    bucket_words = 0
    for para in rest:
        wc = word_count(para)
        if bucket and bucket_words + wc > _SECTION_TARGET_WORDS and bucket_words >= 60:
            heading = derive_section_heading("\n\n".join(bucket))
            sections.append((heading, bucket[:]))
            bucket = [para]
            bucket_words = wc
        else:
            bucket.append(para)
            bucket_words += wc
    if bucket:
        sections.append((derive_section_heading("\n\n".join(bucket)), bucket))

    if len(sections) == 1 and sum(word_count(p) for p in sections[0][1]) >= 120:
        paras = sections[0][1]
        mid = max(1, len(paras) // 2)
        left, right = paras[:mid], paras[mid:]
        if right:
            sections = [
                (derive_section_heading("\n\n".join(left)), left),
                (derive_section_heading("\n\n".join(right)), right),
            ]

    seen: set[str] = set()
    section_blocks: list[str] = []
    for heading, paras in sections:
        base = heading
        n = 2
        while heading.lower() in seen:
            alt_idx = min(n - 2, len(paras) - 1)
            alt = derive_section_heading("\n\n".join(paras))
            if alt.lower() not in seen:
                heading = alt
            else:
                heading = f"{base} ({n})"
            n += 1
        seen.add(heading.lower())
        section_blocks.append(f"## {heading}")
        section_blocks.append("")
        section_blocks.extend(paras)
        section_blocks.append("")

    return intro, "\n".join(section_blocks).strip()


def structure_article_body(article_body: str) -> tuple[str, str]:
    """Return (intro_markdown, body_markdown_with_headings)."""
    normalized = normalize_structural_markers(article_body)
    main, conclusion, faqs = partition_closing(normalized)
    intro, sections = sectionize_main_body(main)
    conclusion = ensure_conclusion_h2(conclusion)
    faqs = ensure_faq_h3(faqs)
    body_parts = [p for p in (sections, conclusion, faqs) if p]
    return intro, "\n\n".join(body_parts).strip()
