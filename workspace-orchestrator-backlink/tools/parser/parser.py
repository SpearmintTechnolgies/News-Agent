#!/usr/bin/env python3
"""parser.py — extract main text, links, forms, and placement signals from HTML."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

try:
    import trafilatura
except ImportError:  # pragma: no cover - tested via mock in unit tests
    trafilatura = None  # type: ignore[assignment]

GUEST_POST_PATTERNS = (
    re.compile(r"write\s+for\s+us", re.I),
    re.compile(r"guest\s+post", re.I),
    re.compile(r"submit\s+(a\s+)?(guest\s+)?post", re.I),
    re.compile(r"contribute", re.I),
    re.compile(r"become\s+a\s+contributor", re.I),
)

COMMENT_PATTERNS = (
    re.compile(r"leave\s+a\s+comment", re.I),
    re.compile(r"post\s+a\s+comment", re.I),
    re.compile(r"comment\s+form", re.I),
)


@dataclass
class FormField:
    name: str
    field_type: str
    required: bool


@dataclass
class FormInfo:
    action: str
    method: str
    fields: list[FormField] = field(default_factory=list)


@dataclass
class ParseResult:
    title: str
    text: str
    links: list[str]
    forms: list[FormInfo]
    signals: dict[str, bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "text": self.text,
            "links": self.links,
            "forms": [
                {
                    "action": form.action,
                    "method": form.method,
                    "fields": [asdict(f) for f in form.fields],
                }
                for form in self.forms
            ],
            "signals": self.signals,
        }


def _extract_title(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def _extract_links(soup: BeautifulSoup, base_url: str | None) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        if absolute in seen:
            continue
        seen.add(absolute)
        links.append(absolute)
    return links


def _extract_forms(soup: BeautifulSoup, base_url: str | None) -> list[FormInfo]:
    forms: list[FormInfo] = []
    for form_tag in soup.find_all("form"):
        action = form_tag.get("action") or ""
        if action and base_url:
            action = urljoin(base_url, action)
        method = (form_tag.get("method") or "get").lower()
        fields: list[FormField] = []
        for input_tag in form_tag.find_all(["input", "textarea"]):
            name = input_tag.get("name") or input_tag.get("id") or ""
            if not name:
                continue
            fields.append(
                FormField(
                    name=name,
                    field_type=(input_tag.get("type") or input_tag.name or "text").lower(),
                    required=input_tag.has_attr("required"),
                )
            )
        forms.append(FormInfo(action=action, method=method, fields=fields))
    return forms


def _detect_signals(text: str, forms: list[FormInfo]) -> dict[str, bool]:
    haystack = text.lower()
    has_guest_post = any(p.search(haystack) for p in GUEST_POST_PATTERNS)
    has_comment_form = any(p.search(haystack) for p in COMMENT_PATTERNS)
    has_textarea_form = any(
        any(f.field_type in {"textarea", "text"} for f in form.fields) for form in forms
    )
    return {
        "guest_post_language": has_guest_post,
        "comment_language": has_comment_form,
        "has_form": bool(forms),
        "has_textarea_form": has_textarea_form,
    }


def parse(html: str, *, base_url: str | None = None) -> ParseResult:
    soup = BeautifulSoup(html, "lxml")
    title = _extract_title(soup)

    text = ""
    if trafilatura is not None:
        extracted = trafilatura.extract(html, url=base_url, include_comments=False)
        text = extracted or ""

    if not text:
        text = soup.get_text("\n", strip=True)

    forms = _extract_forms(soup, base_url)
    links = _extract_links(soup, base_url)
    signals = _detect_signals(f"{title}\n{text}", forms)

    return ParseResult(
        title=title,
        text=text,
        links=links,
        forms=forms,
        signals=signals,
    )
