"""Structured article produced by the V6 writer."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

_WS = re.compile(r"\s+")


def _clean(text: Any) -> str:
    return _WS.sub(" ", str(text or "")).strip()


def _ids(value: Any, prefix: str) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        token = str(item).strip().upper()
        if re.fullmatch(rf"{prefix}\d+", token) and token not in out:
            out.append(token)
    return out


def words(text: str) -> int:
    return len(text.split())


@dataclass
class Paragraph:
    text: str
    facts: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, raw: Any) -> "Paragraph":
        if isinstance(raw, str):
            return cls(text=_clean(raw))
        raw = raw if isinstance(raw, dict) else {}
        return cls(text=_clean(raw.get("text")), facts=_ids(raw.get("facts"), "F"), quotes=_ids(raw.get("quotes"), "Q"))


@dataclass
class Section:
    heading: str
    paragraphs: list[Paragraph] = field(default_factory=list)


@dataclass
class FAQItem:
    question: str
    answer: str
    facts: list[str] = field(default_factory=list)


@dataclass
class SEO:
    focus_keyword: str = ""
    meta_title: str = ""
    meta_description: str = ""
    slug: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = ""


@dataclass
class Article:
    headline: str
    dek: str
    sections: list[Section] = field(default_factory=list)
    conclusion: list[Paragraph] = field(default_factory=list)
    faq: list[FAQItem] = field(default_factory=list)
    seo: SEO = field(default_factory=SEO)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Article":
        sections = []
        for raw in data.get("sections") or []:
            if not isinstance(raw, dict):
                continue
            paras = [Paragraph.parse(p) for p in raw.get("paragraphs") or []]
            paras = [p for p in paras if p.text]
            if paras:
                sections.append(Section(heading=_clean(raw.get("heading")), paragraphs=paras))
        conclusion_raw = data.get("conclusion")
        if isinstance(conclusion_raw, dict):
            conclusion_raw = conclusion_raw.get("paragraphs")
        conclusion = [p for p in (Paragraph.parse(x) for x in conclusion_raw or []) if p.text]
        faq = []
        for raw in data.get("faq") or []:
            if isinstance(raw, dict) and _clean(raw.get("question")) and _clean(raw.get("answer")):
                faq.append(FAQItem(_clean(raw["question"]), _clean(raw["answer"]), _ids(raw.get("facts"), "F")))
        seo_raw = data.get("seo") if isinstance(data.get("seo"), dict) else {}
        seo = SEO(
            focus_keyword=_clean(seo_raw.get("focus_keyword")),
            meta_title=_clean(seo_raw.get("meta_title")),
            meta_description=_clean(seo_raw.get("meta_description")),
            slug=_clean(seo_raw.get("slug")).lower(),
            tags=[_clean(t) for t in seo_raw.get("tags") or [] if _clean(t)],
            category=_clean(seo_raw.get("category")),
        )
        return cls(
            headline=_clean(data.get("headline")),
            dek=_clean(data.get("dek")),
            sections=sections,
            conclusion=conclusion,
            faq=faq,
            seo=seo,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Article":
        return cls.from_json(data)

    # --- views ---------------------------------------------------------------
    def body_paragraphs(self) -> list[Paragraph]:
        return [p for s in self.sections for p in s.paragraphs]

    def all_paragraphs(self) -> list[tuple[str, Paragraph]]:
        """(location, paragraph) for every prose unit, closing sections included."""
        out: list[tuple[str, Paragraph]] = []
        for si, section in enumerate(self.sections):
            for pi, para in enumerate(section.paragraphs):
                out.append((f"section{si + 1}.p{pi + 1}", para))
        for pi, para in enumerate(self.conclusion):
            out.append((f"conclusion.p{pi + 1}", para))
        for qi, item in enumerate(self.faq):
            out.append((f"faq{qi + 1}.answer", Paragraph(item.answer, item.facts)))
        return out

    @property
    def body_words(self) -> int:
        return sum(words(p.text) for p in self.body_paragraphs())

    @property
    def total_words(self) -> int:
        return self.body_words + sum(words(p.text) for p in self.conclusion) + sum(
            words(f.question) + words(f.answer) for f in self.faq
        )

    def cited_facts(self) -> set[str]:
        return {fid for _, p in self.all_paragraphs() for fid in p.facts}

    def to_markdown(self) -> str:
        parts: list[str] = []
        for index, section in enumerate(self.sections):
            if section.heading and index > 0:
                parts.append(f"## {section.heading}")
            parts.extend(p.text for p in section.paragraphs)
        if self.conclusion:
            parts.append("## Conclusion")
            parts.extend(p.text for p in self.conclusion)
        if self.faq:
            parts.append("## Frequently Asked Questions")
            for item in self.faq:
                parts.append(f"### {item.question}")
                parts.append(item.answer)
        return "\n\n".join(parts)
