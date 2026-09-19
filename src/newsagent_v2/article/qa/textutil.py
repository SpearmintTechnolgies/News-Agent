from __future__ import annotations

import re

WORD_RE = re.compile(r"[A-Za-z0-9$£€%.-]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
NUMBER_TOKEN_RE = re.compile(
    r"(?<![A-Za-z])(?:[$£€]\s*)?\d+(?:,\d{3})*(?:\.\d+)?(?:\s*(?:million|billion|trillion|%|m|bn))?",
    re.IGNORECASE,
)
# Protect dotted abbreviations so "p.m. ET" is not two sentences.
ABBREVIATION_RE = re.compile(
    r"\b(?:a\.m|p\.m|mr|mrs|ms|dr|jr|sr|vs|inc|ltd|u\.s)\.",
    re.IGNORECASE,
)


def word_count(text: str) -> int:
    if not isinstance(text, str) or not text.strip():
        return 0
    return len(text.split())


def words(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    return [match.group(0).lower() for match in WORD_RE.finditer(text)]


def split_sentences(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip():
        return []
    placeholders: list[str] = []

    def _protect(match: re.Match[str]) -> str:
        placeholders.append(match.group(0))
        return f"@@ABBREV{len(placeholders) - 1}@@"

    protected = ABBREVIATION_RE.sub(_protect, text.strip())
    parts = SENTENCE_SPLIT_RE.split(protected)
    restored: list[str] = []
    for part in parts:
        item = part
        for index, original in enumerate(placeholders):
            item = item.replace(f"@@ABBREV{index}@@", original)
        item = item.strip()
        if item:
            restored.append(item)
    return restored


def split_paragraphs(text: str) -> list[str]:
    if not isinstance(text, str):
        return []
    return [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]


def number_tokens(text: str) -> set[str]:
    found: set[str] = set()
    if not isinstance(text, str):
        return found
    for match in NUMBER_TOKEN_RE.finditer(text):
        token = re.sub(r"\s+", "", match.group(0).lower())
        found.add(token)
        digits = re.sub(r"[^\d.]", "", token)
        if digits:
            found.add(digits)
    return found
