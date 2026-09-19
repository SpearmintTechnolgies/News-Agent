"""Deterministic CanonicalArticle assembler and SEO normalization. No new facts."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from newsagent_v2.article.contract import (
    ARTICLE_CATEGORIES,
    ENTITY_TYPES,
    stamp_article_schema_version,
)
from newsagent_v2.article.expand import expand_provider_article
from newsagent_v2.article.qa.headline import HEADLINE_MAX_WORDS, HEADLINE_MIN_WORDS
from newsagent_v2.article.qa.seo import META_MAX, META_MIN, SEO_TITLE_MAX, SEO_TITLE_MIN, SLUG_RE
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.canonical import CANONICAL_ARTICLE_FIELDS
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.controlled.paragraph import ParagraphValidation
from newsagent_v2.article.writer.controlled.proposition import (
    proposition_frame_from_claim,
    realize_proposition_frame,
)
from newsagent_v2.article.writer.controlled.realization import editorial_is_invalid

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TITLE_CASE = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b")


def _clip_words(text: str, maximum: int) -> str:
    words = str(text or "").split()
    if len(words) <= maximum:
        return " ".join(words).strip()
    clipped = " ".join(words[:maximum]).rstrip(".,:;!?")
    return clipped


def _from_requirements(req: dict[str, str]) -> str:
    parts = [
        str(req.get("primary_actor") or "").strip(),
        str(req.get("primary_action") or "").strip(),
        str(req.get("primary_object") or "").strip(),
    ]
    text = " ".join(part for part in parts if part)
    timing = str(req.get("timing") or "").strip()
    if timing and timing.lower() not in text.lower():
        text = f"{text} {timing}".strip()
    return text


def _copies_source_title(text: str, article_input: dict[str, Any]) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    for row in article_input.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()
        if title and SequenceMatcher(None, lowered, title.lower()).ratio() >= 0.72:
            return True
    rep = str(article_input.get("representative_title") or "").strip()
    if rep and SequenceMatcher(None, lowered, rep.lower()).ratio() >= 0.72:
        return True
    return False


def _headline_from_plan(
    plan: ArticlePlan,
    ledgers: EvidenceLedgers,
    fallback: str,
    article_input: dict[str, Any],
) -> str:
    rendered = str(fallback or "").strip()
    if editorial_is_invalid(rendered, field="headline") or _copies_source_title(rendered, article_input):
        rendered = ""
    if (
        rendered
        and HEADLINE_MIN_WORDS <= word_count(rendered) <= HEADLINE_MAX_WORDS
        and not editorial_is_invalid(rendered, field="headline")
    ):
        return rendered.rstrip(".")
    claim = ledgers.claim_by_id().get(plan.headline_claim_ids[0]) if plan.headline_claim_ids else None
    if claim is not None:
        realized = realize_proposition_frame(proposition_frame_from_claim(claim)).rstrip(".")
        if not editorial_is_invalid(realized, field="headline") and not _copies_source_title(realized, article_input):
            return _clip_words(realized, HEADLINE_MAX_WORDS)
    semantic = _from_requirements(plan.headline_requirements)
    return _clip_words(semantic or rendered, HEADLINE_MAX_WORDS).rstrip(".")


def _dek_from_plan(
    plan: ArticlePlan,
    ledgers: EvidenceLedgers,
    fallback: str,
    article_input: dict[str, Any],
) -> str:
    rendered = str(fallback or "").strip()
    if editorial_is_invalid(rendered, field="dek") or _copies_source_title(rendered, article_input):
        rendered = ""
    if rendered and 8 <= word_count(rendered) <= 40 and not editorial_is_invalid(rendered, field="dek"):
        return rendered.rstrip(".")
    claim = ledgers.claim_by_id().get(plan.dek_claim_ids[0]) if plan.dek_claim_ids else None
    if claim is not None:
        realized = realize_proposition_frame(proposition_frame_from_claim(claim)).rstrip(".")
        if not editorial_is_invalid(realized, field="dek") and not _copies_source_title(realized, article_input):
            return _clip_words(realized, 40)
    semantic = _from_requirements(plan.dek_requirements)
    return _clip_words(semantic or rendered, 40).rstrip(".")


def _slug_from_text(text: str) -> str:
    slug = _NON_ALNUM.sub("-", str(text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return slug[:80] or "story"


def _entities_from_claims(claims: list[LedgerClaim]) -> list[dict[str, str]]:
    names: list[str] = []
    seen: set[str] = set()
    for claim in claims:
        for match in _TITLE_CASE.finditer(claim.text):
            name = match.group(1).strip()
            key = name.lower()
            if key in seen or len(name) < 3:
                continue
            seen.add(key)
            names.append(name)
            if len(names) >= 8:
                break
        if len(names) >= 8:
            break
    entities: list[dict[str, str]] = []
    for name in names:
        etype = "org" if any(token in name.lower() for token in ("inc", "payments", "senate", "bank")) else "other"
        if etype not in ENTITY_TYPES:
            etype = "other"
        entities.append({"name": name, "type": etype})
    return entities


def _keywords(headline: str, body: str) -> list[str]:
    found: list[str] = []
    body_l = body.lower()
    for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", headline):
        lower = token.lower()
        if lower in {"the", "and", "for", "with"} or lower in found:
            continue
        if lower in body_l:
            found.append(token)
        if len(found) >= 6:
            break
    return found


def _valid_entities(rows: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return out
    for item in rows:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        etype = item.get("type")
        if not name or etype not in ENTITY_TYPES:
            continue
        out.append({"name": name, "type": str(etype)})
    return out


def assemble_canonical_article(
    *,
    plan: ArticlePlan,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    validated: list[ParagraphValidation],
    headline_text: str = "",
    dek_text: str = "",
    seo_title: str = "",
    meta_description: str = "",
    slug: str = "",
    entities: list[dict[str, Any]] | None = None,
    keywords: list[str] | None = None,
) -> dict[str, Any]:
    """Only retained/safe surviving units enter the CanonicalArticle. Never quarantined text."""
    ok_rows = [row for row in validated if row.assemblable]
    used_claim_ids: list[str] = []
    used_quote_ids: list[str] = []
    sections: list[dict[str, Any]] = []
    maps: list[dict[str, Any]] = []
    for index, row in enumerate(ok_rows):
        para_plan = next((item for item in plan.paragraph_plans if item.paragraph_id == row.paragraph_id), None)
        purpose = para_plan.editorial_purpose if para_plan else ""
        claim_ids = list(row.mapped_claim_ids)
        if not claim_ids and para_plan:
            claim_ids = list(para_plan.required_claim_ids)
        for claim_id in claim_ids:
            if claim_id not in used_claim_ids:
                used_claim_ids.append(claim_id)
        for quote_id in row.mapped_quote_ids:
            if quote_id not in used_quote_ids:
                used_quote_ids.append(quote_id)
        sections.append(
            {
                "id": f"s{index + 1}",
                "section_id": f"s{index + 1}",
                "purpose": purpose,
                "paragraphs": [{"text": row.text, "claim_ids": claim_ids}],
            }
        )
        maps.append({"paragraph_index": index, "claim_ids": claim_ids})

    used_claims = [ledgers.claim_by_id()[cid] for cid in used_claim_ids if cid in ledgers.claim_by_id()]
    quotes = [row for row in ledgers.quotes if row.quote_id in set(used_quote_ids)]
    headline = _headline_from_plan(plan, ledgers, headline_text, article_input)
    dek = _dek_from_plan(plan, ledgers, dek_text, article_input)
    body = "\n\n".join(row.text for row in ok_rows if row.text.strip())
    title = str(seo_title or "").strip() or headline
    meta = str(meta_description or "").strip() or dek
    raw_slug = str(slug or "").strip()
    entity_rows = _valid_entities(entities) or _entities_from_claims(used_claims)
    keyword_rows = [str(item).strip() for item in (keywords or []) if str(item).strip()] or _keywords(headline, body)
    article = {
        "event_id": plan.event_id,
        "headline": headline,
        "dek": dek,
        "article_body": body,
        "category": plan.category if plan.category in ARTICLE_CATEGORIES else "other",
        "seo_title": title[:SEO_TITLE_MAX],
        "meta_description": meta,
        "slug": raw_slug if SLUG_RE.match(raw_slug) else _slug_from_text(headline),
        "entities": entity_rows,
        "keywords": keyword_rows,
        "claims": [
            {
                "claim_id": row.claim_id,
                "id": row.claim_id,
                "text": row.text,
                "claim_type": row.claim_type,
                "evidence_ids": list(row.evidence_ids),
            }
            for row in used_claims
        ],
        "quotes": [
            {
                "quote_id": row.quote_id,
                "text": row.text,
                "kind": "direct",
                "attribution": row.speaker,
                "evidence_ids": [row.evidence_id],
            }
            for row in quotes
        ],
        "article_sections": sections,
        "paragraph_maps": maps,
        "evidence_used": [],
    }
    stamp_article_schema_version(article)
    expand_provider_article(article, article_input)
    materialize_article(article)
    article["article_body"] = body
    return article


def normalize_seo_fields(article: dict[str, Any]) -> dict[str, Any]:
    """Correct SEO mechanics from existing headline/dek/body. No new claims."""
    out = dict(article)
    headline = str(out.get("headline") or "").strip()
    dek = str(out.get("dek") or "").strip()
    body = str(out.get("article_body") or "").strip()
    seo_title = str(out.get("seo_title") or "").strip() or headline
    if len(seo_title) > SEO_TITLE_MAX:
        seo_title = seo_title[:SEO_TITLE_MAX].rstrip()
    if len(seo_title) < SEO_TITLE_MIN:
        seo_title = (headline or seo_title)[:SEO_TITLE_MAX]
    meta = str(out.get("meta_description") or "").strip() or dek
    if len(meta) > META_MAX:
        meta = meta[:META_MAX].rstrip()
    if len(meta) < META_MIN:
        filler_source = dek or body
        meta = filler_source[:META_MAX]
    slug = str(out.get("slug") or "")
    if not SLUG_RE.match(slug):
        slug = _slug_from_text(headline or slug)
    category = out.get("category")
    if category not in ARTICLE_CATEGORIES:
        category = "other"
    out["seo_title"] = seo_title
    out["meta_description"] = meta
    out["slug"] = slug
    out["category"] = category
    return out


def canonical_field_report(article: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in CANONICAL_ARTICLE_FIELDS if name not in article]
    return {
        "ok": not missing,
        "missing": missing,
        "schema_version": article.get("schema_version"),
    }
