#!/usr/bin/env python3
"""
Validate article H3 / FAQ / H2 counts against global borders and section order.

Usage:
  python3 validate_article_structure.py /tmp/crypto-article.md
"""

from __future__ import annotations

import re
import sys

H3_MIN, H3_MAX = 3, 6
FAQ_MIN, FAQ_MAX = 3, 6
H2_MIN, H2_MAX = 2, 4

SOURCES_SPLIT_RE = re.compile(
    r"\n(?:\*\*Sources:\*\*|^Sources:)\s*\n",
    re.IGNORECASE | re.MULTILINE,
)
FAQ_HEADER_RE = re.compile(r"^##\s+FAQs?\s*$", re.IGNORECASE | re.MULTILINE)
CONCLUSION_HEADER_RE = re.compile(r"^##\s+Conclusion\s*$", re.IGNORECASE | re.MULTILINE)


def split_parts(content: str) -> tuple[str, str, str]:
    """Return (body_before_faq, faq_section, after_sources)."""
    sources_parts = SOURCES_SPLIT_RE.split(content, maxsplit=1)
    main = sources_parts[0]
    after = sources_parts[1] if len(sources_parts) > 1 else ""

    m = FAQ_HEADER_RE.search(main)
    if not m:
        return main, "", after

    body = main[: m.start()].rstrip()
    faq = main[m.start() :].rstrip()
    return body, faq, after


def count_h3(body: str) -> int:
    return len(re.findall(r"^###\s+", body, re.MULTILINE))


def count_h2_body(body: str) -> int:
    """H2 sections excluding Conclusion and FAQs."""
    count = 0
    for line in body.splitlines():
        if re.match(r"^##\s+", line):
            title = line[3:].strip().lower()
            if title.startswith("conclusion"):
                continue
            count += 1
    return count


def count_faqs(faq_section: str) -> int:
    if not faq_section:
        return 0
    return len(re.findall(r"^\*{0,2}\d+\.\s+", faq_section, re.MULTILINE))


def check_section_order(main_before_sources: str) -> list[str]:
    """FAQs must follow Conclusion; both must exist."""
    errors: list[str] = []
    concl = CONCLUSION_HEADER_RE.search(main_before_sources)
    faq = FAQ_HEADER_RE.search(main_before_sources)

    if not concl:
        errors.append("missing ## Conclusion section")
    if not faq:
        errors.append("missing ## FAQs section")
    if concl and faq and faq.start() < concl.start():
        errors.append("## FAQs must appear after ## Conclusion")
    return errors


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: validate_article_structure.py <article.md>",
            file=sys.stderr,
        )
        return 1

    with open(sys.argv[1], encoding="utf-8", errors="replace") as f:
        content = f.read()

    sources_parts = SOURCES_SPLIT_RE.split(content, maxsplit=1)
    main = sources_parts[0]

    body, faq, _after = split_parts(content)
    h3 = count_h3(body)
    h2 = count_h2_body(body)
    faq_n = count_faqs(faq)

    errors: list[str] = []
    errors.extend(check_section_order(main))

    if not (H3_MIN <= h3 <= H3_MAX):
        errors.append(f"expected {H3_MIN}-{H3_MAX} H3 subsections (###), found {h3}")
    if not (FAQ_MIN <= faq_n <= FAQ_MAX):
        errors.append(f"expected {FAQ_MIN}-{FAQ_MAX} FAQ items, found {faq_n}")
    if not (H2_MIN <= h2 <= H2_MAX):
        errors.append(f"expected {H2_MIN}-{H2_MAX} H2 body sections, found {h2}")

    if errors:
        print("ARTICLE_INVALID: " + "; ".join(errors))
        return 1

    print(f"STRUCTURE_VALID: {h3} H3, {faq_n} FAQ, {h2} H2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
