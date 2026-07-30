#!/usr/bin/env python3
"""
autofix_article.py — Deterministic, zero-LLM repair of MECHANICAL article failures.

Run this BEFORE deciding a check_article.py FAIL needs an LLM revision. It fixes the
cheap, unambiguous failures that otherwise each cost a full (expensive) writer revision:

  1. no_em_dash         — replace em-dashes (—) with a comma/period
  2. anchor_no_tweets   — strip x.com / twitter.com /status/ links from the BODY
                          (unwrap the markdown link to its visible label text)
  3. word_count_footer  — recompute the body word count and rewrite the
                          `[Word Count: N]` footer (or append it if missing)
  4. heading_inline_hash — strip duplicate inline `#` runs inside heading lines
  5. bare_source_line    — remove standalone `Source | Source` attribution lines
  6. canonical_bullet_lists — convert inline ` * ` / `* item` / `•` / `1.` to `- item`

It does NOT touch anything ambiguous (word band, H2/H3/FAQ counts, META limits,
topic coherence, banned phrases) — those still go to the LLM revision path.

The body word count is computed EXACTLY the way check_article.py computes it
(strip pty/thinking noise, then body-before-Sources), so the footer this writes
is guaranteed to match the checker.

Usage:
  python3 autofix_article.py --article /path/to/raw.md
  python3 autofix_article.py --article /path/to/raw.md --no-footer   # skip footer (post-sync)

Prints one `AUTOFIX:` line per change applied (or `AUTOFIX: none`).
Exit 0 always when the file is readable/writable (it is best-effort cleanup).
Exit 1 only on unreadable/empty file.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

_ARTICLE_DIR = os.path.dirname(os.path.abspath(__file__))
if _ARTICLE_DIR not in sys.path:
    sys.path.insert(0, _ARTICLE_DIR)

import article_hygiene as hyg  # noqa: E402
import validate_anchor_links as val  # noqa: E402

_ORCH_PIPELINE = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/skills/pipeline"
)
if _ORCH_PIPELINE not in sys.path:
    sys.path.insert(0, _ORCH_PIPELINE)

import sync_article_from_raw as sync  # noqa: E402

WORD_COUNT_FOOTER_RE = re.compile(
    r"^\[Word Count:\s*\d+\s*\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# em-dash, optionally padded by spaces, becomes ", " (collapses doubled commas below)
EM_DASH_RE = re.compile(r"\s*\u2014\s*")


def fix_em_dashes(content: str) -> tuple[str, int]:
    n = len(re.findall(r"\u2014", content))
    if not n:
        return content, 0
    fixed = EM_DASH_RE.sub(", ", content)
    # tidy artifacts like ", ," or " ,"
    fixed = re.sub(r",\s*,", ",", fixed)
    fixed = re.sub(r"\s+,", ",", fixed)
    return fixed, n


def strip_body_tweet_links(content: str) -> tuple[str, int]:
    """Unwrap [label](x.com/.../status/123) -> label, but only in the body
    (before the Sources footer). Sources block is left untouched."""
    parts = val.SOURCES_SPLIT_RE.split(content, maxsplit=1)
    body = parts[0]
    tail = content[len(body):]  # includes the Sources delimiter + everything after

    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        label, url = m.group(1), m.group(2)
        if val.is_tweet_url(url):
            count += 1
            return label or url
        return m.group(0)

    new_body = val.LINK_RE.sub(repl, body)
    if count == 0:
        return content, 0
    return new_body + tail, count


def recompute_footer(content: str) -> tuple[str, bool]:
    """Set the [Word Count: N] footer to the checker's body count.
    Returns (new_content, changed)."""
    body_words = len(sync.body_for_word_count(sync.strip_pty_noise(content)).split())
    new_line = f"[Word Count: {body_words}]"

    existing = WORD_COUNT_FOOTER_RE.search(content)
    if existing:
        if existing.group(0).strip() == new_line:
            return content, False
        return WORD_COUNT_FOOTER_RE.sub(new_line, content, count=1), True

    # No footer present — append it as the final line.
    sep = "" if content.endswith("\n") else "\n"
    return content + f"{sep}\n{new_line}\n", True


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic mechanical article auto-fix")
    parser.add_argument("--article", required=True, help="Path to article markdown")
    parser.add_argument(
        "--no-footer",
        action="store_true",
        help="Skip the [Word Count] footer rewrite (use on post-sync final.md)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.article):
        print(f"AUTOFIX_ERROR: file not found: {args.article}", file=sys.stderr)
        return 1

    with open(args.article, encoding="utf-8", errors="replace") as f:
        original = f.read()
    if not original.strip():
        print("AUTOFIX_ERROR: article is empty", file=sys.stderr)
        return 1

    content = original
    changes: list[str] = []

    content, em = fix_em_dashes(content)
    if em:
        changes.append(f"em_dash: replaced {em} em-dash(es) with comma")

    content, tw = strip_body_tweet_links(content)
    if tw:
        changes.append(f"anchor_no_tweets: unwrapped {tw} tweet link(s) in body")

    content, hyg_changes = hyg.fix_content_hygiene(content, val.SOURCES_SPLIT_RE)
    for c in hyg_changes:
        if c.startswith("heading_inline_hash"):
            changes.append(f"heading_no_inline_hash: {c.split(': ', 1)[-1]}")
        elif c.startswith("bare_source_line"):
            changes.append(f"no_bare_source_line: {c.split(': ', 1)[-1]}")

    content, bullet_changes = hyg.fix_canonical_bullet_lists(content, val.SOURCES_SPLIT_RE)
    for c in bullet_changes:
        changes.append(f"canonical_bullet_lists: {c}")

    if not args.no_footer:
        content, footer_changed = recompute_footer(content)
        if footer_changed:
            changes.append("word_count_footer: synced [Word Count] to body count")

    if content != original:
        with open(args.article, "w", encoding="utf-8") as f:
            f.write(content)

    if changes:
        for c in changes:
            print(f"AUTOFIX: {c}")
    else:
        print("AUTOFIX: none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
