"""Publisher chrome stripping for extracted article paragraphs.

Extraction libraries keep the main column, but publishers put bylines,
"add us as a preferred source" prompts, editorial-policy footers and
related-link rails inside it. None of that is reportable fact.
"""

from __future__ import annotations

import re

# Publisher boilerplate that never appears in reporting: chrome at any length.
_STRONG_CHROME_RE = re.compile(
    r"("
    r"preferred source|add .{0,30} on google\b|"
    r"produced in accordance with|editorial (policy|guidelines|standards)|"
    r"(this|the) (content|article) (is|was) (for informational|not intended)|"
    r"not (financial|investment|trading) advice|do your own research|"
    r"the views (and opinions )?expressed|^disclaimer\s*:|^disclosure\s*:|all rights reserved|"
    r"ai[- ]generated (content|output)|readers are advised to|do not warrant|disclaim any liability|"
    r"this (article|story|post) (is|was) posted in|check it out for more|"
    r"^transparency note|(produced|written|generated) with the (assistance|help|use) of (ai|artificial intelligence)|"
    r"^(written|edited|reviewed|fact[- ]checked|reporting) by\b|"
    r"\bis an? (senior |staff |contributing |freelance )?(reporter|writer|editor|journalist|correspondent)\b"
    r".{0,80}\b(covering|based in|who covers)\b"
    r")",
    re.IGNORECASE,
)
# Phrases that are chrome in a short line but can occur in real reporting
# ("a bill sponsored by", "investors subscribe to the fund").
_WEAK_CHROME_RE = re.compile(
    r"("
    r"follow us\b|follow .{0,30} on (x|twitter|google news)|"
    r"subscribe (to|for|now)|newsletter|sign up (for|to)|join our (telegram|discord|community|channel)|"
    r"download (the|our) app|get the app|cookie|privacy policy|terms of (use|service)|"
    r"click here|tap here|share this (article|story)|"
    r"image (credit|source)|photo(graph)? (credit|by)|getty images|shutterstock|"
    r"^(read|see) (more|also)\b|^also read\b|^related\b|^recommended\b|^trending\b|"
    r"advertisement|sponsored|partner content|"
    r"\bsign in\b|\blog in\b|\bcomments?\b.{0,20}\b(below|section)\b|your reply|"
    r"(story|article) continues below|listen to (this|the) (article|story)|"
    r"\b\d+\s+min(ute)?s? read\b|^updated\b.{0,40}\b(20\d\d|ago)\b|^published\b.{0,40}\b(20\d\d|ago)\b"
    r")",
    re.IGNORECASE,
)
_WEAK_MAX_WORDS = 15
_BYLINE_RE = re.compile(r"^By [A-Z][\w'’.-]+(?: [A-Z][\w'’.-]+){0,3}\b")
_BYLINE_MAX_WORDS = 14

# Once one of these appears as its own paragraph, everything after it is
# a related-links rail or footer.
_TAIL_MARKER_RE = re.compile(
    r"^\W*("
    r"more on (the|this) (subject|topic|story)|related (articles|stories|news|coverage|reading)|"
    r"read (next|more)|you (may|might) also like|more (from|stories|news)\b|"
    r"recommended (for you|stories)|latest (news|stories)|popular (now|stories)|"
    r"about the author|share this|tags?:|topics?:"
    r")\b",
    re.IGNORECASE,
)

_SENTENCE_END_RE = re.compile(r"[.!?…:\"”’)\]]\s*$")
_MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)[^)]*\)")
_MD_PREFIX_RE = re.compile(r"^\s*(?:[#>*\-+]+|\d+[.)])\s*")
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]{0,200}>")
_WS_RE = re.compile(r"\s+")


def strip_markdown(text: str) -> tuple[str, list[str]]:
    """Return plain text and the link targets that were embedded in it."""
    links = [m.group(2) for m in _MD_LINK_RE.finditer(text)]
    plain = _MD_LINK_RE.sub(lambda m: m.group(1), text)
    plain = _HTML_TAG_RE.sub("", plain)
    plain = plain.replace("**", "").replace("__", "")
    plain = _MD_PREFIX_RE.sub("", plain)
    plain = re.sub(r"(?<!\w)[*_`](?!\s)|(?<!\s)[*_`](?!\w)", "", plain)
    return _WS_RE.sub(" ", plain).strip(), links


def _is_titlecase_list(text: str) -> bool:
    tokens = [t for t in re.findall(r"[A-Za-z][\w'’&.-]*", text)]
    if len(tokens) < 4:
        return False
    capped = sum(1 for t in tokens if t[0].isupper())
    return capped / len(tokens) >= 0.7 and not _SENTENCE_END_RE.search(text)


def has_chrome_phrase(text: str) -> bool:
    """Boilerplate wording only, without the length/shape heuristics used for scraped blocks."""
    words = text.split()
    return bool(
        _STRONG_CHROME_RE.search(text) or (len(words) <= _WEAK_MAX_WORDS and _WEAK_CHROME_RE.search(text))
    )


def is_chrome(paragraph: str) -> bool:
    text = paragraph.strip()
    if not text:
        return True
    words = text.split()
    if _STRONG_CHROME_RE.search(text):
        return True
    if len(words) <= _WEAK_MAX_WORDS and _WEAK_CHROME_RE.search(text):
        return True
    if len(words) <= _BYLINE_MAX_WORDS and _BYLINE_RE.match(text):
        return True
    if len(words) < 8 and not _SENTENCE_END_RE.search(text):
        return True
    if len(words) < 5:
        return True
    if len(words) <= 8 and text.endswith("?"):
        return True
    if _is_titlecase_list(text):
        return True
    return False


def clean_paragraphs(raw_blocks: list[str]) -> tuple[list[str], list[str]]:
    """Clean extracted markdown blocks into fact-bearing paragraphs.

    Returns (paragraphs, links-found-inside-kept-paragraphs).
    """
    kept: list[str] = []
    links: list[str] = []
    seen: set[str] = set()
    for block in raw_blocks:
        raw = block.strip()
        if not raw:
            continue
        if raw.lstrip().startswith("#"):
            heading, _ = strip_markdown(raw)
            if kept and _TAIL_MARKER_RE.search(heading):
                break
            continue
        plain, block_links = strip_markdown(raw)
        if not plain:
            continue
        if kept and _TAIL_MARKER_RE.search(plain) and len(plain.split()) <= 12:
            break
        if is_chrome(plain):
            continue
        key = _WS_RE.sub(" ", plain.lower())
        if key in seen:
            continue
        seen.add(key)
        kept.append(plain)
        links.extend(block_links)
    return kept, links
