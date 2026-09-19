"""
Deterministic cleanup of extracted article text.

Removes page chrome and ticker widgets. Does not call a model.
Does not drop in-story prices, attributions, or news paragraphs.
"""

from __future__ import annotations

import re

from newsagent_v2.article.qa.textutil import split_sentences, word_count

TICKER = (
    r"(?:DOGE|TRX|LINK|ZEC|ADA|XRP|ETH|BTC|XMR|BNB|XLM|SOL|HYPE|USDT|USDC|"
    r"AVAX|DOT|ATOM|NEAR|APT|SUI|TON|PEPE|SHIB|WIF|LTC|BCH|UNI|AAVE|MKR|"
    r"ARB|OP|FIL|INJ|TIA|SEI|RENDER|FET|TAO)"
)
TICKER_TRIPLE = rf"{TICKER}\s+\$[0-9,]+\.?\d*\s+[+\-]?[\d.]+%"
TICKER_BLOCK_RE = re.compile(rf"(?:{TICKER_TRIPLE}\s*){{4,}}", re.IGNORECASE)

BYLINE_UNIT_RE = re.compile(
    r"Written by\s+.+?(?:staff (?:editor|writer|reporter))"
    r"(?:\s+Reviewed by\s+.+?(?:staff (?:editor|writer|reporter)))?",
    re.IGNORECASE,
)
LATEST_NEWS_PUBLISHED_RE = re.compile(
    r"\bLatest News\s+Published\s+[A-Z][a-z]+\s+\d{1,2},\s+\d{4}\b",
    re.IGNORECASE,
)
RELATED_RE = re.compile(
    r"\bRelated:\s*(?:[^\n.]{0,240})",
    re.IGNORECASE,
)
SOURCE_CHROME_RE = re.compile(r"(?:^|\.\s+)Source:\s*$", re.IGNORECASE | re.MULTILINE)
WS_RE = re.compile(r"\s+")


def _collapse_duplicate_bylines(text: str) -> str:
    # Bylines are page chrome. Story attributions ("Lummis said") remain.
    return BYLINE_UNIT_RE.sub(" ", text)


def clean_extracted_article_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    cleaned = TICKER_BLOCK_RE.sub(" ", text)
    cleaned = _collapse_duplicate_bylines(cleaned)
    cleaned = LATEST_NEWS_PUBLISHED_RE.sub(" ", cleaned)
    cleaned = RELATED_RE.sub(" ", cleaned)
    cleaned = SOURCE_CHROME_RE.sub(" ", cleaned)
    cleaned = WS_RE.sub(" ", cleaned).strip()
    sentences = split_sentences(cleaned)
    seen: set[str] = set()
    kept: list[str] = []
    for sentence in sentences:
        key = WS_RE.sub(" ", sentence).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(sentence.strip())
    return " ".join(kept).strip()


def cleaned_word_count(text: str) -> int:
    return word_count(clean_extracted_article_text(text))
