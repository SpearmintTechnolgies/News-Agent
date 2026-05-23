#!/usr/bin/env python3
"""Convert pandoc HTML to WordPress Gutenberg block-serialized content."""

import argparse
import re
import sys

BLOCK_RE = re.compile(
    r"<h1[^>]*>.*?</h1>|"
    r"<h2[^>]*>.*?</h2>|"
    r"<h3[^>]*>.*?</h3>|"
    r"<h4[^>]*>.*?</h4>|"
    r"<p[^>]*>.*?</p>|"
    r"<ul[^>]*>.*?</ul>|"
    r"<ol[^>]*>.*?</ol>|"
    r"<img[^>]*\/?>",
    re.IGNORECASE | re.DOTALL,
)


def _heading_block(tag: str, inner_html: str) -> str:
    level = int(tag[1])
    text = re.sub(r"<[^>]+>", "", inner_html).strip()
    return (
        f'<!-- wp:heading {{"level":{level}}} -->\n'
        f'<h{level} class="wp-block-heading">{text}</h{level}>\n'
        f"<!-- /wp:heading -->"
    )


def _paragraph_block(full_tag: str) -> str:
    return f"<!-- wp:paragraph -->\n{full_tag}\n<!-- /wp:paragraph -->"


def _list_block(full_tag: str, ordered: bool) -> str:
    kind = "wp:list" if not ordered else 'wp:list {"ordered":true}'
    return f"<!-- {kind} -->\n{full_tag}\n<!-- /wp:list -->"


def _image_block(full_tag: str) -> str:
    src_m = re.search(r'src=["\']([^"\']+)["\']', full_tag, re.I)
    alt_m = re.search(r'alt=["\']([^"\']*)["\']', full_tag, re.I)
    src = src_m.group(1) if src_m else ""
    alt = alt_m.group(1) if alt_m else ""
    if not src:
        return f"<!-- wp:html -->\n{full_tag}\n<!-- /wp:html -->"
    return (
        "<!-- wp:image {\"sizeSlug\":\"large\"} -->\n"
        f'<figure class="wp-block-image size-large">'
        f'<img src="{src}" alt="{alt}"/></figure>\n'
        "<!-- /wp:image -->"
    )


def convert_element(element: str) -> str:
    element = element.strip()
    if not element:
        return ""

    lower = element.lower()
    if lower.startswith("<h1"):
        inner = re.sub(r"^<h1[^>]*>|</h1>$", "", element, flags=re.I | re.S).strip()
        return _heading_block("h1", inner)
    if lower.startswith("<h2"):
        inner = re.sub(r"^<h2[^>]*>|</h2>$", "", element, flags=re.I | re.S).strip()
        return _heading_block("h2", inner)
    if lower.startswith("<h3"):
        inner = re.sub(r"^<h3[^>]*>|</h3>$", "", element, flags=re.I | re.S).strip()
        return _heading_block("h3", inner)
    if lower.startswith("<h4"):
        inner = re.sub(r"^<h4[^>]*>|</h4>$", "", element, flags=re.I | re.S).strip()
        return _heading_block("h4", inner)
    if lower.startswith("<p"):
        return _paragraph_block(element)
    if lower.startswith("<ul"):
        return _list_block(element, ordered=False)
    if lower.startswith("<ol"):
        return _list_block(element, ordered=True)
    if lower.startswith("<img"):
        return _image_block(element)
    return f"<!-- wp:html -->\n{element}\n<!-- /wp:html -->"


def html_to_gutenberg(html: str) -> str:
    html = re.sub(r"<!DOCTYPE[^>]*>", "", html, flags=re.I)
    html = re.sub(r"</?(html|body|head|meta|title)[^>]*>", "", html, flags=re.I)
    html = html.strip()

    if not html:
        return ""

    if "<!-- wp:" in html:
        return html

    blocks = []
    last_end = 0
    for m in BLOCK_RE.finditer(html):
        between = html[last_end : m.start()].strip()
        if between:
            blocks.append(f"<!-- wp:html -->\n{between}\n<!-- /wp:html -->")
        blocks.append(convert_element(m.group(0)))
        last_end = m.end()

    tail = html[last_end:].strip()
    if tail:
        blocks.append(f"<!-- wp:html -->\n{tail}\n<!-- /wp:html -->")

    blocks = [b for b in blocks if b.strip()]
    if not blocks:
        return f'<!-- wp:html -->\n{html}\n<!-- /wp:html -->'

    body = "\n\n".join(blocks)
    return (
        '<!-- wp:group {"layout":{"type":"constrained","contentSize":"720px"}} -->\n'
        f'<div class="wp-block-group">\n{body}\n</div>\n'
        "<!-- /wp:group -->"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert HTML to Gutenberg blocks")
    parser.add_argument("input", help="Input HTML file path")
    parser.add_argument("-o", "--output", help="Output file (default: overwrite input)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        html = f.read()

    result = html_to_gutenberg(html)
    out_path = args.output or args.input
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)

    block_count = result.count("<!-- wp:")
    print(f"[INFO]  Gutenberg conversion: {block_count} block(s) written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
