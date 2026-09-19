"""
Expand compact Groq article JSON into local article-output-v1.

Resolves evidence_id → frozen evidence unit → url/source.
Does not invent mappings. No LLM.
"""

from __future__ import annotations

from typing import Any

from newsagent_v2.article.contract import stamp_article_schema_version
from newsagent_v2.article.prompt_evidence import compact_story_evidence


def _short_evidence_id(evidence_id: str) -> str:
    text = str(evidence_id or "").strip()
    if "-e" in text:
        return "e" + text.rsplit("-e", 1)[-1]
    return ""


def story_evidence_units(article_input: dict[str, Any] | None) -> list[dict[str, Any]]:
    pack = article_input if isinstance(article_input, dict) else {}
    units = pack.get("evidence_units")
    if isinstance(units, list) and units:
        return [row for row in units if isinstance(row, dict)]
    compact = compact_story_evidence(
        {
            "event_id": pack.get("event_id"),
            "article_input": pack,
        }
    )
    found = compact.get("evidence_units")
    return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []


def evidence_id_index(article_input: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for unit in story_evidence_units(article_input):
        evidence_id = str(unit.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        index[evidence_id] = unit
        short = _short_evidence_id(evidence_id)
        if short and short not in index:
            index[short] = unit
    return index


def foreign_evidence_ids(
    article_input: dict[str, Any] | None,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> set[str]:
    local = set(evidence_id_index(article_input))
    found: set[str] = set()
    event_id = (article_input or {}).get("event_id")
    for other in other_article_inputs or []:
        if not isinstance(other, dict):
            continue
        if other.get("event_id") == event_id:
            continue
        for evidence_id in evidence_id_index(other):
            if evidence_id not in local:
                found.add(evidence_id)
    return found


def _ref_from_unit(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": unit.get("url"),
        "source": unit.get("source"),
    }


def resolve_evidence_ids(
    evidence_ids: Any,
    article_input: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Map compact IDs to url/source refs. Unknown IDs are omitted, not invented."""
    if not isinstance(evidence_ids, list):
        return []
    index = evidence_id_index(article_input)
    refs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in evidence_ids:
        evidence_id = str(raw).strip()
        if not evidence_id:
            continue
        unit = index.get(evidence_id)
        if unit is None:
            continue
        url = unit.get("url")
        key = str(url or evidence_id)
        if key in seen:
            continue
        seen.add(key)
        refs.append(_ref_from_unit(unit))
    return refs


def _claim_id(claim: dict[str, Any]) -> str:
    return str(claim.get("claim_id") or claim.get("id") or "").strip()


def _unique_refs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        if not isinstance(url, str) or not url or url in seen:
            continue
        seen.add(url)
        ref = {"url": url}
        source = row.get("source")
        if isinstance(source, str) and source.strip():
            ref["source"] = source
        out.append(ref)
    return out


def _expand_ref_list(
    row: dict[str, Any],
    article_input: dict[str, Any] | None,
) -> None:
    ids = row.get("evidence_ids")
    existing = row.get("evidence_refs")
    has_urls = (
        isinstance(existing, list)
        and any(isinstance(item, dict) and item.get("url") for item in existing)
    )
    if isinstance(ids, list) and ids and not has_urls:
        row["evidence_refs"] = resolve_evidence_ids(ids, article_input)
    elif not isinstance(existing, list):
        row["evidence_refs"] = []


def expand_provider_article(
    article: Any,
    article_input: dict[str, Any] | None = None,
    *,
    other_article_inputs: list[dict[str, Any]] | None = None,
) -> Any:
    """Mutate compact provider JSON into local article-output-v1 fields."""
    del other_article_inputs
    if not isinstance(article, dict):
        return article
    pack = article_input if isinstance(article_input, dict) else {}

    for claim in article.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        claim_id = _claim_id(claim)
        if claim_id and not claim.get("claim_id"):
            claim["claim_id"] = claim_id
        if not claim.get("claim_type"):
            claim["claim_type"] = "fact"
        _expand_ref_list(claim, pack)

    for quote in article.get("quotes") or []:
        if not isinstance(quote, dict):
            continue
        _expand_ref_list(quote, pack)

    if not isinstance(article.get("evidence_used"), list):
        collected: list[dict[str, Any]] = []
        for group in (article.get("claims"), article.get("quotes")):
            if not isinstance(group, list):
                continue
            for row in group:
                if not isinstance(row, dict):
                    continue
                refs = row.get("evidence_refs")
                if isinstance(refs, list):
                    collected.extend(item for item in refs if isinstance(item, dict))
        article["evidence_used"] = _unique_refs(collected)

    if article.get("generation_notes") is None:
        article["generation_notes"] = ""
    elif "generation_notes" not in article:
        article["generation_notes"] = ""

    for section in article.get("article_sections") or []:
        if not isinstance(section, dict):
            continue
        if not section.get("section_id") and section.get("id"):
            section["section_id"] = str(section.get("id") or "")
        if "purpose" not in section:
            section["purpose"] = ""

    stamp_article_schema_version(article)
    return article
