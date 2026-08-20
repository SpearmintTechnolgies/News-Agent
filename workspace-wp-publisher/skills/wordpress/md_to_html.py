#!/usr/bin/env python3
"""Convert article Markdown to HTML without pandoc.

WordPress cannot render raw Markdown. publish.sh used to `cp` the .md file
into the HTML path when pandoc was missing, which dumped `# ##` and `[links]()`
as one blob on the site.
"""
from __future__ import annotations

import argparse
import re
import sys


def md_to_html(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip() + "\n"
    try:
        import markdown

        return markdown.markdown(
            text,
            extensions=["extra", "sane_lists", "smarty"],
            output_format="html5",
        )
    except ImportError:
        return _fallback_md_to_html(text)


def _inline(s: str) -> str:
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", s)
    return s


def _fallback_md_to_html(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_inline(line[4:].strip())}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:].strip())}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:].strip())}</h1>")
        elif line.strip() in ("---", "***"):
            out.append("<hr/>")
        elif re.match(r"^[-*]\s+", line):
            items = []
            bullet_re = re.compile(r"^[-*]\s+")
            while i < len(lines) and bullet_re.match(lines[i]):
                item_txt = bullet_re.sub("", lines[i]).strip()
                items.append(f"<li>{_inline(item_txt)}</li>")
                i += 1
            out.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue
        elif re.match(r"^\d+\.\s+", line):
            items = []
            num_re = re.compile(r"^\d+\.\s+")
            while i < len(lines) and num_re.match(lines[i]):
                item_txt = num_re.sub("", lines[i]).strip()
                items.append(f"<li>{_inline(item_txt)}</li>")
                i += 1
            out.append("<ol>\n" + "\n".join(items) + "\n</ol>")
            continue
        else:
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#{1,3}\s|[-*]\s|\d+\.\s|---)", lines[i]):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{_inline(' '.join(p.strip() for p in para))}</p>")
            continue
        i += 1
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser(description="Markdown to HTML (pandoc-free)")
    p.add_argument("input")
    p.add_argument("-o", "--output", help="Output HTML path (default: stdout)")
    args = p.parse_args()
    with open(args.input, encoding="utf-8") as f:
        html = md_to_html(f.read())
    if not html.strip():
        print("MD_TO_HTML_FAIL: empty output", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[INFO]  Markdown→HTML: {args.output} ({len(html)} chars)")
    else:
        sys.stdout.write(html)
    return 0


if __name__ == "__main__":
    sys.exit(main())
