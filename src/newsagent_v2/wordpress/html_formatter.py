"""CMS-ready HTML formatter for WordPress articles.

Deterministic HTML generation:
- Centered reading-column wrapper with scoped CSS (~720-820px)
- TOC with H2 anchor links (FAQs/Sources once; no per-FAQ H3 dump)
- H2/H3 structure from article
- Evidence-based source links (no invented URLs)
- Internal "Read Also" links via Master Index (published) or WP REST publish-only
- No LLM, no URL invention
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from .config import WordPressConfig
from newsagent_v2.publication.master_index import MasterIndexStore
from newsagent_v2.write.article import strip_ai_dashes
from .editorial_structure import structure_article_body

Transport = Callable[..., Any]


@dataclass
class FormattedArticle:
    """CMS-ready formatted article."""
    html_content: str
    toc: str
    headings: list[dict[str, Any]]  # TOC H2 entries: [{level, text, anchor, id}, ...]
    source_links: list[dict[str, Any]]  # Evidence-based sources used
    internal_links: list[dict[str, Any]]  # WP internal "Read Also" links


class ArticleHtmlFormatter:
    """Format article body into CMS-ready HTML."""

    # Patterns for parsing article structure
    HEADING_PATTERN = re.compile(r'^(#{2,3})\s+(.+)$', re.MULTILINE)
    PARAGRAPH_SPLIT = re.compile(r'\n\n+')

    # Scoped reading-column CSS — affects only .na-article-reading-column, not Divi/global layout.
    READING_COLUMN_STYLE = (
        "<style>"
        ".na-article-reading-column{"
        "max-width:780px;width:100%;margin:0 auto;box-sizing:border-box;"
        "line-height:1.65;font-size:1.05em;}"
        ".na-article-reading-column p{margin:0 0 0.8em;}"
        ".na-article-reading-column h2,.na-article-reading-column h3{"
        "line-height:1.3;margin:1.1em 0 0.4em;}"
        ".na-article-reading-column .article-toc{"
        "margin:0.35em 0 0.65em;padding:0.6em 0.85em;}"
        ".na-article-reading-column p + .article-toc{"
        "margin-top:0.15em;}"
        ".na-article-reading-column .article-toc + h2{margin-top:0.5em;}"
        ".na-article-reading-column .article-toc .toc-list{"
        "list-style:disc;margin:0.3em 0 0 1.1em;padding:0;}"
        ".na-article-reading-column .article-toc .toc-item{"
        "margin:0.15em 0;padding:0;}"
        ".na-article-reading-column img,"
        ".na-article-reading-column table,"
        ".na-article-reading-column pre{"
        "max-width:100%;height:auto;overflow-x:auto;}"
        "@media (max-width:900px){"
        ".na-article-reading-column{max-width:100%;padding:0 0.5rem;}}"
        "</style>"
    )

    def __init__(
        self,
        config: WordPressConfig,
        transport: Transport,
        master_index: MasterIndexStore | None = None,
    ) -> None:
        self.config = config
        self.transport = transport
        self.master_index = master_index

    def _wp_api(self, endpoint: str) -> str:
        """Build WordPress REST API URL."""
        return urljoin(self.config.base_url + "/", f"wp-json/wp/v2/{endpoint}")

    def _auth(self) -> tuple[str, str]:
        """Return auth tuple."""
        return (self.config.username, self.config.app_password)

    def _slugify_anchor(self, text: str) -> str:
        """Convert heading text to HTML anchor ID."""
        cleaned = re.sub(r'[^\w\s-]', '', text.lower())
        cleaned = re.sub(r'[\s]+', '-', cleaned)
        return cleaned[:50] or "section"

    def _extract_headings(self, content: str) -> list[dict[str, Any]]:
        """Extract H2/H3 headings from markdown-style article."""
        headings = []
        seen_anchors: set[str] = set()

        for match in self.HEADING_PATTERN.finditer(content):
            hashes = match.group(1)
            text = match.group(2).strip()
            level = len(hashes)  # 2 for H2, 3 for H3

            anchor = self._slugify_anchor(text)
            original_anchor = anchor
            counter = 1
            while anchor in seen_anchors:
                anchor = f"{original_anchor}-{counter}"
                counter += 1
            seen_anchors.add(anchor)

            headings.append({
                "level": level,
                "text": text,
                "anchor": anchor,
                "id": f"h-{anchor}",
            })

        return headings

    def _build_toc(self, headings: list[dict[str, Any]]) -> str:
        """Build Table of Contents HTML.

        Main TOC lists H2 sections only (FAQs and Sources once each).
        FAQ question H3s stay inside the FAQs section and are not dumped here.
        """
        toc_headings = [h for h in headings if int(h.get("level") or 0) == 2]
        if not toc_headings:
            return ""

        lines = [
            '<nav class="article-toc" aria-label="Table of Contents">',
            '<h2>Table of Contents</h2>',
            '<ul class="toc-list">',
        ]
        for h in toc_headings:
            lines.append(
                f'<li class="toc-item toc-h2">'
                f'<a href="#{h["anchor"]}">{html.escape(h["text"])}</a>'
                f'</li>'
            )
        lines.append('</ul>')
        lines.append('</nav>')
        return "\n".join(lines)

    def _transform_headings_to_html(self, content: str, headings: list[dict[str, Any]]) -> str:
        """Transform markdown headings to HTML H2/H3 with anchors."""
        result = content
        for h in headings:
            md_pattern = rf'^{"#" * h["level"]}\s+{re.escape(h["text"])}$'
            html_heading = f'<h{h["level"]} id="{h["anchor"]}">{html.escape(h["text"])}</h{h["level"]}>'
            result = re.sub(md_pattern, html_heading, result, flags=re.MULTILINE)
        return result

    def _build_source_links_section(
        self,
        evidence: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Build source links section from evidence data.

        Uses only URLs already present in evidence - no invention.
        """
        if not evidence:
            return "", []

        used_sources: list[dict[str, Any]] = []
        lines = ['<section class="article-sources">', '<h2>Sources</h2>', '<ul>']

        for item in evidence:
            if not isinstance(item, dict):
                continue

            url = item.get("url", "").strip()
            source = item.get("source", "").strip() or item.get("source_name", "").strip()
            title = item.get("title", "").strip()

            if not url or not url.startswith(("http://", "https://")):
                continue

            if any(s.get("url") == url for s in used_sources):
                continue

            display_text = title or source or "Source"
            used_sources.append({
                "url": url,
                "source": source,
                "title": title,
            })

            lines.append(
                f'<li><a href="{html.escape(url)}" target="_blank" rel="noopener">'
                f'{html.escape(display_text)}</a></li>'
            )

        lines.append('</ul>')
        lines.append('</section>')

        if used_sources:
            return "\n".join(lines), used_sources
        return "", []

    def _discover_internal_links(
        self,
        topic: str,
        max_links: int = 3,
        entities: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Discover internal "Read Also" links via Master Index or WP REST.

        Published only. No LLM, no URL invention. Empty MI => skip cleanly.
        """
        if not topic:
            return []

        if self.master_index is not None:
            # wordpress-source rows come from status=publish sync only.
            host = (urlparse(self.config.base_url).hostname or "").lower()
            candidates = self.master_index.search_wordpress(
                topic, entities=entities, limit=max_links, host=host
            )
            return [
                {
                    "id": row.wp_post_id,
                    "url": row.canonical_url,
                    "title": (row.title or "").strip(),
                }
                for row in candidates
                if row.canonical_url
                and str(row.canonical_url).startswith(("http://", "https://"))
                and (row.title or "").strip()
            ]

        search_term = topic.replace(" ", "+")[:50]
        resp = self.transport(
            "GET",
            self._wp_api(
                f"posts?search={search_term}&status=publish&per_page={max_links + 2}"
                f"&_fields=id,title,link,status"
            ),
            auth=self._auth(),
        )

        if not resp.get("ok"):
            return []

        posts = resp.get("payload", [])
        if not isinstance(posts, list):
            return []

        links = []
        for post in posts:
            if not isinstance(post, dict):
                continue
            status = str(post.get("status") or "publish").lower()
            if status and status != "publish":
                continue
            post_id = post.get("id")
            post_url = post.get("link")
            title_obj = post.get("title", {})
            post_title = (
                title_obj.get("rendered") if isinstance(title_obj, dict) else str(title_obj)
            )
            if post_url and post_title and str(post_url).startswith(("http://", "https://")):
                links.append({
                    "id": post_id,
                    "url": post_url,
                    "title": post_title,
                })
            if len(links) >= max_links:
                break

        return links

    def _build_internal_links_section(
        self,
        links: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]]]:
        """Build "Read Also" internal links section."""
        if not links:
            return "", []

        lines = ['<section class="article-read-also">', '<h2>Read Also</h2>', '<ul>']
        for link in links:
            lines.append(
                f'<li><a href="{html.escape(link["url"])}">'
                f'{html.escape(link["title"])}</a></li>'
            )
        lines.append('</ul>')
        lines.append('</section>')

        return "\n".join(lines), links

    _MD_LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()]+)\)")

    @classmethod
    def _inline(cls, text: str) -> str:
        """Escape text, then render markdown links ``[anchor](https://...)`` as anchors."""
        return cls._MD_LINK_RE.sub(r'<a href="\2">\1</a>', html.escape(text))

    def _paragraphs_to_html(self, content: str) -> str:
        """Convert plain paragraphs to HTML paragraphs."""
        blocks = self.PARAGRAPH_SPLIT.split(content.strip())
        html_blocks = []
        escape = self._inline

        for block in blocks:
            block = block.strip()
            if not block:
                continue

            if block.startswith("<"):
                html_blocks.append(block)
                continue

            if block.startswith(("<h2", "<H2", "<h3", "<H3")):
                html_blocks.append(block)
                continue

            if block.startswith(">"):
                quote_text = block[1:].strip()
                html_blocks.append(f'<blockquote>{escape(quote_text)}</blockquote>')
            elif block.startswith(("- ", "* ", "1. ")):
                items = []
                for line in block.split("\n"):
                    line = line.strip()
                    if line.startswith(("- ", "* ")):
                        items.append(f'<li>{escape(line[2:])}</li>')
                    elif re.match(r'^\d+\.\s', line):
                        items.append(f'<li>{escape(re.sub(r"^\d+\.\s", "", line))}</li>')
                if items:
                    html_blocks.append(f'<ul>\n{"\n".join(items)}\n</ul>')
                else:
                    html_blocks.append(f'<p>{escape(block)}</p>')
            else:
                html_blocks.append(f'<p>{escape(block)}</p>')

        return "\n\n".join(html_blocks)

    def _wrap_reading_column(self, inner_html: str) -> str:
        """Wrap article HTML in a centered ~720-820px reading column (scoped CSS only)."""
        if not inner_html or not inner_html.strip():
            return inner_html
        return (
            f"{self.READING_COLUMN_STYLE}\n"
            f'<div class="na-article-reading-column newsagent-article">\n'
            f"{inner_html.strip()}\n"
            f"</div>"
        )

    def format_article(
        self,
        article_body: str,
        evidence: list[dict[str, Any]] | None,
        topic: str = "",
        entities: list[str] | None = None,
        include_toc: bool = True,
        include_sources: bool = True,
        include_read_also: bool = True,
        max_read_also: int = 3,
        preserve_structure: bool = False,
        read_also_links: list[dict[str, Any]] | None = None,
    ) -> FormattedArticle:
        """Format article body into CMS-ready HTML.

        Structural pipeline (no fact invention):
        1. Normalize bold closing markers / FAQ questions into markdown H2/H3
        2. Split long prose into short paragraphs and titled H2 sections
        3. Ensure Conclusion / FAQs H2s and FAQ question H3s
        4. Build TOC from H2 headings; assemble intro -> TOC -> body -> Sources
        5. Wrap in scoped reading-column container
        """
        if not article_body:
            return FormattedArticle(
                html_content="",
                toc="",
                headings=[],
                source_links=[],
                internal_links=[],
            )
        article_body = strip_ai_dashes(article_body)

        if preserve_structure:
            # Already sectioned markdown (lede, ## sections, ## Conclusion, ## FAQ with ### questions).
            first_heading = re.search(r"^## ", article_body, flags=re.MULTILINE)
            split_at = first_heading.start() if first_heading else len(article_body)
            intro_md, body_md = article_body[:split_at].strip(), article_body[split_at:].strip()
        else:
            intro_md, body_md = structure_article_body(article_body)

        all_headings = self._extract_headings(body_md)
        intro_html = self._paragraphs_to_html(intro_md) if intro_md else ""
        body_with_headings = self._transform_headings_to_html(body_md, all_headings)
        body_html = self._paragraphs_to_html(body_with_headings)

        sources_html = ""
        source_links: list[dict[str, Any]] = []
        if include_sources and evidence:
            sources_html, source_links = self._build_source_links_section(evidence)
            if sources_html:
                sources_anchor = self._slugify_anchor("Sources")
                if not any(h.get("text") == "Sources" for h in all_headings):
                    all_headings.append(
                        {
                            "level": 2,
                            "text": "Sources",
                            "anchor": sources_anchor,
                            "id": f"h-{sources_anchor}",
                        }
                    )
                sources_html = re.sub(
                    r"<h2(\b[^>]*)>\s*Sources\s*</h2>",
                    f'<h2 id="{sources_anchor}">Sources</h2>',
                    sources_html,
                    count=1,
                    flags=re.IGNORECASE,
                )

        toc_html = self._build_toc(all_headings) if include_toc and all_headings else ""
        toc_headings = [h for h in all_headings if int(h.get("level") or 0) == 2]

        read_also_html = ""
        internal_links: list[dict[str, Any]] = []
        if read_also_links is not None:
            internal_links = [link for link in read_also_links if link.get("url") and link.get("title")]
            read_also_html, _ = self._build_internal_links_section(internal_links[:max_read_also])
        elif include_read_also and topic:
            internal_links = self._discover_internal_links(
                topic, max_links=max_read_also, entities=entities
            )
            read_also_html, _ = self._build_internal_links_section(internal_links)

        parts: list[str] = []
        if intro_html:
            parts.append(intro_html)
        if toc_html:
            parts.append(toc_html)
        if body_html:
            parts.append(body_html)
        if sources_html:
            parts.append(sources_html)
        if read_also_html:
            parts.append(read_also_html)

        body_inner = "\n\n".join(parts)
        final_html = self._wrap_reading_column(body_inner) if body_inner else ""

        return FormattedArticle(
            html_content=final_html,
            toc=toc_html,
            headings=toc_headings,
            source_links=source_links,
            internal_links=internal_links,
        )
