#!/usr/bin/env python3
"""Shared heading and bare-source-line hygiene for writer validators/autofix."""
from __future__ import annotations

import re

HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(.+)$")
INLINE_HASH_IN_HEADING_RE = re.compile(r"\s#{1,6}\s+")
LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)", re.IGNORECASE)
SENTENCE_MARKERS_RE = re.compile(r"[.!?]\s")


def heading_has_inline_hash(line: str) -> bool:
    m = HEADING_LINE_RE.match(line.strip())
    if not m:
        return False
    return bool(INLINE_HASH_IN_HEADING_RE.search(m.group(2)))


def fix_heading_inline_hash(line: str) -> tuple[str, bool]:
    m = HEADING_LINE_RE.match(line.rstrip())
    if not m:
        return line, False
    marker, text = m.group(1), m.group(2)
    if not INLINE_HASH_IN_HEADING_RE.search(text):
        return line, False
    clean = INLINE_HASH_IN_HEADING_RE.split(text, maxsplit=1)[0].strip()
    if not clean:
        return line, False
    return f"{marker} {clean}", True


def is_bare_source_line(line: str) -> bool:
    """Standalone publisher attribution line (not inline prose)."""
    s = line.strip()
    if not s or len(s) > 140:
        return False
    if s.startswith(("#", "-", "*", "[Word Count")):
        return False
    if re.match(r"^\*\*\d+\.", s):
        return False
    if SENTENCE_MARKERS_RE.search(s):
        return False
    if any(
        w in s.lower()
        for w in (" said ", " will ", " has ", " have ", " which ", " that ", " according ")
    ):
        return False

    labels = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", s)
    if "|" in labels:
        parts = [p.strip() for p in labels.split("|")]
        if len(parts) >= 2 and all(1 <= len(p.split()) <= 5 for p in parts if p):
            return True
    if LINK_RE.search(s):
        stripped = LINK_RE.sub("", s).strip(" |,")
        if not stripped:
            return True
    return False


def find_bare_source_lines(content: str, sources_split_re: re.Pattern[str]) -> list[str]:
    body = sources_split_re.split(content, maxsplit=1)[0]
    return [ln for ln in body.splitlines() if is_bare_source_line(ln)]


def find_heading_inline_hash_lines(content: str) -> list[str]:
    bad: list[str] = []
    for line in content.splitlines():
        if heading_has_inline_hash(line):
            bad.append(line.strip()[:80])
    return bad


def fix_content_hygiene(content: str, sources_split_re: re.Pattern[str]) -> tuple[str, list[str]]:
    """Return (fixed_content, list of change descriptions)."""
    changes: list[str] = []
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        bare = is_bare_source_line(line.rstrip("\n"))
        if bare:
            changes.append(f"bare_source_line: removed {line.strip()[:60]!r}")
            continue
        fixed, changed = fix_heading_inline_hash(line.rstrip("\n"))
        if changed:
            changes.append(f"heading_inline_hash: fixed {line.strip()[:60]!r}")
            out.append(fixed + ("\n" if line.endswith("\n") else ""))
        else:
            out.append(line)
    return "".join(out), changes


def slug_contains_keyword(slug: str, primary_kw: str) -> bool:
    """All significant tokens from primary_kw appear in the slug."""
    kw_tokens = [
        t
        for t in re.findall(r"[a-z0-9]+", primary_kw.lower())
        if t not in {"the", "a", "an", "of", "and", "or", "for", "to", "in", "on"}
    ]
    if not kw_tokens:
        return bool(primary_kw.lower().replace(" ", "-") in slug.lower())
    slug_tokens = set(re.findall(r"[a-z0-9]+", slug.lower()))
    return all(t in slug_tokens or t.replace(" ", "-") in slug.lower() for t in kw_tokens)
