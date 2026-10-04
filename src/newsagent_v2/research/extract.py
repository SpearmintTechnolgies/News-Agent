"""Main-content extraction (trafilatura) + chrome stripping."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import trafilatura

from newsagent_v2.research.chrome import clean_paragraphs

for _noisy in ("trafilatura", "htmldate", "courlan", "justext"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

PAYWALL_RE = re.compile(
    r"(subscribe to (continue|read)|subscribers? only|this (article|story) is for (subscribers|members)|"
    r"already a subscriber|become a (subscriber|member)|unlock this (article|story)|"
    r"create a free account to (continue|read))",
    re.IGNORECASE,
)


@dataclass
class Extracted:
    paragraphs: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    title: str = ""
    published_at: str = ""
    author: str = ""
    sitename: str = ""
    description: str = ""
    paywalled: bool = False

    @property
    def word_count(self) -> int:
        return sum(len(p.split()) for p in self.paragraphs)


def extract(html: str, url: str) -> Extracted:
    if not html:
        return Extracted()
    common = dict(
        url=url,
        include_comments=False,
        include_tables=False,
        include_images=False,
        favor_precision=True,
        deduplicate=True,
    )
    # Link-preserving markdown loses whitespace around anchors, so text comes
    # from the plain pass and only link targets come from the markdown pass.
    plain = trafilatura.extract(html, output_format="markdown", include_links=False, **common) or ""
    linked = trafilatura.extract(html, output_format="markdown", include_links=True, **common) or ""
    paragraphs, _ = clean_paragraphs([line for line in plain.splitlines() if line.strip()])
    _, links = clean_paragraphs([line for line in linked.splitlines() if line.strip()])
    meta = trafilatura.extract_metadata(html, default_url=url)
    out = Extracted(
        paragraphs=paragraphs,
        links=_absolute_links(links, url),
        title=(getattr(meta, "title", "") or "") if meta else "",
        published_at=(getattr(meta, "date", "") or "") if meta else "",
        author=(getattr(meta, "author", "") or "") if meta else "",
        sitename=(getattr(meta, "sitename", "") or "") if meta else "",
        description=(getattr(meta, "description", "") or "") if meta else "",
    )
    out.paywalled = out.word_count < 150 and bool(PAYWALL_RE.search(html))
    return out


def _absolute_links(links: list[str], base: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link.startswith(("mailto:", "javascript:", "#")):
            continue
        absolute = urljoin(base, link)
        if not absolute.startswith("http") or absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
    return out
