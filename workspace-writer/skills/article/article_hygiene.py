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


FAQ_HEADER_RE = re.compile(r"^##\s+FAQs?\s*$", re.IGNORECASE)
FAQ_QUESTION_RE = re.compile(r"^\s*\*\*\d+\.\s+")
ASTERISK_LINE_RE = re.compile(r"^\s*\*(?!\*)\s+")
UNICODE_BULLET_LINE_RE = re.compile(r"^\s*[•·]\s+")
NUMBERED_LINE_RE = re.compile(r"^\s*\d+\.\s+")
INLINE_ASTERISK_BULLET_RE = re.compile(r"(?:^|[:.!?]\s+)\*\s+\S")
CANONICAL_BULLET_LINE_RE = re.compile(r"^\s*-\s+")


def _in_faq_section(faq_start: int | None, line_no: int) -> bool:
    return faq_start is not None and line_no >= faq_start


def _is_skipped_bullet_line(line: str, in_faq: bool) -> bool:
    s = line.strip()
    if not s:
        return True
    if s.startswith("#"):
        return True
    if s.startswith("[Word Count"):
        return True
    if s.startswith("**Sources:") or s.startswith("Sources:"):
        return True
    if in_faq and FAQ_QUESTION_RE.match(s):
        return True
    if CANONICAL_BULLET_LINE_RE.match(s):
        return True
    return False


def find_noncanonical_bullet_issues(content: str, sources_split_re: re.Pattern[str]) -> list[str]:
    """Return short snippets describing non-dash bullet formatting in the body."""
    body = sources_split_re.split(content, maxsplit=1)[0]
    faq_match = FAQ_HEADER_RE.search(body)
    faq_start = faq_match.start() if faq_match else None
    faq_line = body[:faq_start].count("\n") if faq_start is not None else None

    issues: list[str] = []
    for i, line in enumerate(body.splitlines()):
        in_faq = _in_faq_section(faq_line, i)
        if _is_skipped_bullet_line(line, in_faq):
            continue
        s = line.strip()
        if ASTERISK_LINE_RE.match(s):
            issues.append(f"asterisk_line: {s[:70]}")
            continue
        if UNICODE_BULLET_LINE_RE.match(s):
            issues.append(f"unicode_bullet: {s[:70]}")
            continue
        if not in_faq and NUMBERED_LINE_RE.match(s):
            issues.append(f"numbered_line: {s[:70]}")
            continue
        if INLINE_ASTERISK_BULLET_RE.search(s):
            issues.append(f"inline_asterisk: {s[:70]}")
    return issues


def _split_inline_asterisk_items(line: str) -> tuple[str, list[str]] | None:
    """Parse `: * one. * two.` or `text. * one. * two.` into intro + items."""
    if not INLINE_ASTERISK_BULLET_RE.search(line):
        return None
    intro = line.strip()
    items: list[str] = []
    if re.search(r":\s*\*\s+", intro):
        intro, rest = re.split(r":\s*\*\s+", intro, maxsplit=1)
        intro = intro.rstrip() + ":"
        chunks = re.split(r"\.\s+\*\s+|\s+\*\s+", rest)
    else:
        chunks = re.split(r"\.\s+\*\s+|\s+\*\s+", intro, maxsplit=1)
        if len(chunks) < 2:
            return None
        intro, rest = chunks[0], chunks[1]
        chunks = re.split(r"\.\s+\*\s+|\s+\*\s+", rest)
    for chunk in chunks:
        item = chunk.strip().rstrip(".")
        if item:
            items.append(item)
    if not items:
        return None
    return intro, items


def _fix_line_start_bullet(line: str) -> tuple[str, bool]:
    s = line.rstrip("\n")
    stripped = s.lstrip()
    if ASTERISK_LINE_RE.match(stripped):
        indent = s[: len(s) - len(stripped)]
        text = ASTERISK_LINE_RE.sub("", stripped, count=1).strip()
        return f"{indent}- {text}", True
    if UNICODE_BULLET_LINE_RE.match(stripped):
        indent = s[: len(s) - len(stripped)]
        text = UNICODE_BULLET_LINE_RE.sub("", stripped, count=1).strip()
        return f"{indent}- {text}", True
    m = NUMBERED_LINE_RE.match(stripped)
    if m:
        indent = s[: len(s) - len(stripped)]
        text = NUMBERED_LINE_RE.sub("", stripped, count=1).strip()
        return f"{indent}- {text}", True
    return line, False


def fix_canonical_bullet_lists(content: str, sources_split_re: re.Pattern[str]) -> tuple[str, list[str]]:
    """Convert inline/asterisk/unicode/numbered bullets to dash lists where unambiguous."""
    changes: list[str] = []
    body = sources_split_re.split(content, maxsplit=1)[0]
    tail = content[len(body) :]

    faq_match = FAQ_HEADER_RE.search(body)
    faq_line = body[: faq_match.start()].count("\n") if faq_match else None

    out_lines: list[str] = []
    for i, line in enumerate(body.splitlines(keepends=False)):
        in_faq = _in_faq_section(faq_line, i)
        if _is_skipped_bullet_line(line, in_faq):
            out_lines.append(line)
            continue

        inline = _split_inline_asterisk_items(line)
        if inline:
            intro, items = inline
            out_lines.append(intro)
            out_lines.append("")
            for item in items:
                out_lines.append(f"- {item}.")
            changes.append(f"inline_asterisk: converted {len(items)} item(s) to dash list")
            continue

        fixed, changed = _fix_line_start_bullet(line)
        if changed:
            if ASTERISK_LINE_RE.match(line.strip()):
                kind = "asterisk_line"
            elif UNICODE_BULLET_LINE_RE.match(line.strip()):
                kind = "unicode_bullet"
            else:
                kind = "numbered_line"
            changes.append(f"{kind}: {line.strip()[:60]!r} -> {fixed.strip()[:60]!r}")
            out_lines.append(fixed)
        else:
            out_lines.append(line)

    new_body = "\n".join(out_lines)
    if body.endswith("\n") and not new_body.endswith("\n"):
        new_body += "\n"
    return new_body + tail, changes


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
