#!/usr/bin/env python3
"""
count_article_body_words.py — Body word count using the same logic as sync.

Usage:
  python3 count_article_body_words.py --path /path/to/article/raw.md
  python3 count_article_body_words.py --run-dir /tmp/crypto-run-<RUN_ID>
  python3 count_article_body_words.py --path raw.md --max 1200   # exit 1 if over

Prints: BODY_WORDS: <n>
"""
from __future__ import annotations

import argparse
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from sync_article_from_raw import body_for_word_count, strip_pty_noise  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Count article body words (pre-Sources).")
    parser.add_argument("--path", help="Path to article markdown file")
    parser.add_argument("--run-dir", help="Run bundle; reads article/raw.md")
    parser.add_argument(
        "--max",
        type=int,
        default=None,
        help="If set, exit 1 when body words exceed this value",
    )
    args = parser.parse_args()

    if args.run_dir:
        path = os.path.join(os.path.realpath(args.run_dir), "article", "raw.md")
    elif args.path:
        path = os.path.realpath(args.path)
    else:
        print("count_article_body_words: provide --path or --run-dir", file=sys.stderr)
        return 1

    if not os.path.isfile(path):
        print(f"count_article_body_words: file not found: {path}", file=sys.stderr)
        return 1

    with open(path, encoding="utf-8", errors="ignore") as f:
        content = strip_pty_noise(f.read())

    n = len(body_for_word_count(content).split())
    print(f"BODY_WORDS: {n}")

    if args.max is not None and n > args.max:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
