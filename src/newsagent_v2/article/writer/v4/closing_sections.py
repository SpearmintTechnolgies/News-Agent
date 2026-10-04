"""Grounded Conclusion / What Happens Next + FAQ appendices for V4 articles.

Uses only authorized ledger propositions already collected. Never invents
answers, forecasts, or filler. Skips sections when evidence cannot support them.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from newsagent_v2.article.qa.textutil import split_sentences, word_count, words

CONCLUSION_HEADING = "Conclusion / What Happens Next"
FAQ_HEADING = "FAQs"

_FUTURE_MARKERS = (
    "will ",
    " plan",
    "plans ",
    "planned ",
    "propose",
    "proposed",
    "expect",
    "expected",
    "pending",
    "await",
    "awaiting",
    "contingent",
    "after approval",
    "next ",
    "upcoming",
    "scheduled",
    "to begin",
    "begin after",
    "may ",
    "could ",
    "conditional",
)

_CONCLUSION_PATTERNS = (
    re.compile(r"(?im)(?:^|\n)\s*(?:#{1,3}\s*|\*\*)?conclusion(?:\s*/\s*what happens next)?\b"),
    re.compile(r"(?im)(?:^|\n)\s*(?:#{1,3}\s*|\*\*)?what happens next\b"),
)
_FAQ_PATTERNS = (
    re.compile(r"(?im)(?:^|\n)\s*(?:#{1,3}\s*|\*\*)?faqs?\b"),
    re.compile(r"(?im)(?:^|\n)\s*(?:#{1,3}\s*|\*\*)?frequently asked questions\b"),
    re.compile(r"(?im)(?:^|\n)\s*\*\*\s*Q\s*:"),
    re.compile(r"(?im)(?:^|\n)\s*Q\s*:\s*\S"),
)


def detect_closing_sections(body: str) -> dict[str, bool]:
    """Semantic detection of existing Conclusion / FAQ blocks (markdown or bold)."""
    text = str(body or "")
    return {
        "conclusion": any(pat.search(text) for pat in _CONCLUSION_PATTERNS),
        "faq": any(pat.search(text) for pat in _FAQ_PATTERNS),
    }


def body_has_closing_sections(body: str) -> bool:
    detected = detect_closing_sections(body)
    return bool(detected["conclusion"] and detected["faq"])


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def _overlap(left: str, right: str) -> float:
    a = set(words(left))
    b = set(words(right))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _proposition_rows(authorized_propositions: Sequence[Any] | None) -> list[dict[str, Any]]:
    from newsagent_v2.article.writer.v4.packet import _is_boilerplate_proposition

    rows: list[dict[str, Any]] = []
    if not authorized_propositions:
        return rows
    for item in authorized_propositions:
        if isinstance(item, dict):
            prop = str(
                item.get("proposition")
                or item.get("text")
                or item.get("claim")
                or ""
            ).strip()
            if not prop or _is_boilerplate_proposition(prop):
                continue
            kind = str(item.get("kind") or "fact").strip().lower()
            if kind == "connective":
                continue
            rows.append(
                {
                    "id": str(item.get("id") or item.get("prop_id") or item.get("proposition_id") or ""),
                    "proposition": prop,
                    "attribution": str(item.get("attribution") or "").strip(),
                    "subject": str(item.get("subject") or "").strip(),
                    "modal": str(item.get("modal") or item.get("modality") or "").strip(),
                    "entities": [str(x) for x in (item.get("entities") or []) if str(x).strip()],
                }
            )
        else:
            kind = str(getattr(item, "kind", "fact") or "fact").strip().lower()
            if kind == "connective":
                continue
            prop = str(getattr(item, "proposition", None) or getattr(item, "text", None) or "").strip()
            if not prop or _is_boilerplate_proposition(prop):
                continue
            rows.append(
                {
                    "id": str(getattr(item, "id", "") or getattr(item, "prop_id", "") or getattr(item, "proposition_id", "") or ""),
                    "proposition": prop,
                    "attribution": str(getattr(item, "attribution", "") or "").strip(),
                    "subject": str(getattr(item, "subject", "") or "").strip(),
                    "modal": str(getattr(item, "modal", "") or getattr(item, "modality", "") or "").strip(),
                    "entities": [str(x) for x in (getattr(item, "entities", None) or []) if str(x).strip()],
                }
            )
    return rows


def _already_covered(proposition: str, body: str) -> bool:
    if _overlap(proposition, body) >= 0.72:
        return True
    for sentence in split_sentences(body):
        if _overlap(proposition, sentence) >= 0.78:
            return True
    return False


def _is_future_looking(row: dict[str, Any]) -> bool:
    blob = _norm(" ".join([row.get("modal") or "", row.get("proposition") or ""]))
    return any(marker in blob for marker in _FUTURE_MARKERS)


def _faq_question(row: dict[str, Any]) -> str:
    attribution = row.get("attribution") or ""
    subject = row.get("subject") or ""
    entities = row.get("entities") or []
    if attribution:
        return f"What did {attribution} report?"
    if subject:
        return f"What is known about {subject}?"
    if entities:
        return f"What was reported about {entities[0]}?"
    return "What did reporting establish?"


def build_grounded_conclusion(rows: list[dict[str, Any]], body: str) -> str | None:
    """Return a short grounded conclusion paragraph, or None if unsupported."""
    unused = [row for row in rows if not _already_covered(row["proposition"], body)]
    pool = unused or rows
    future = [row for row in pool if _is_future_looking(row)]
    chosen: list[str] = []
    for row in future[:2]:
        chosen.append(row["proposition"].rstrip(".") + ".")
    if not chosen:
        for row in pool[:2]:
            text = row["proposition"].rstrip(".") + "."
            if text not in chosen:
                chosen.append(text)
            if len(chosen) >= 2:
                break
    if not chosen:
        return None
    return " ".join(chosen)


def build_grounded_faqs(rows: list[dict[str, Any]], body: str, *, min_faqs: int = 2, max_faqs: int = 4) -> list[tuple[str, str]]:
    """Build 2–4 FAQs whose answers are authorized propositions only."""
    faqs: list[tuple[str, str]] = []
    seen_answers: set[str] = set()
    # Prefer facts not already fully restated in the body.
    ordered = [row for row in rows if not _already_covered(row["proposition"], body)] + [
        row for row in rows if _already_covered(row["proposition"], body)
    ]
    for row in ordered:
        answer = row["proposition"].rstrip(".") + "."
        key = _norm(answer)
        if key in seen_answers:
            continue
        if len(answer.split()) < 4:
            continue
        question = _faq_question(row)
        # Avoid near-duplicate questions
        if any(_overlap(question, q) >= 0.9 for q, _ in faqs):
            # Fall back to a more specific question using a slice of the answer.
            subject = row.get("subject") or (row.get("entities") or [None])[0]
            if subject:
                question = f"What was reported regarding {subject}?"
            else:
                question = f"What else did sources establish?"
            if any(_overlap(question, q) >= 0.9 for q, _ in faqs):
                continue
        faqs.append((question, answer))
        seen_answers.add(key)
        if len(faqs) >= max_faqs:
            break
    if len(faqs) < min_faqs:
        return []
    return faqs


def format_closing_sections(*, conclusion: str | None, faqs: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    if conclusion:
        parts.append(f"## {CONCLUSION_HEADING}\n\n{conclusion}")
    if faqs:
        lines = [f"## {FAQ_HEADING}", ""]
        for question, answer in faqs:
            lines.append(f"**Q:** {question}")
            lines.append(f"**A:** {answer}")
            lines.append("")
        parts.append("\n".join(lines).rstrip())
    return "\n\n".join(parts).strip()


def append_grounded_closing_sections(
    article_body: str,
    *,
    authorized_propositions: Sequence[Any] | None,
    article_type: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Append grounded closing sections when appropriate.

    Idempotent: detects existing Conclusion / FAQ semantically and only adds
    the missing section(s). Never duplicates either block.
    """
    body = str(article_body or "").strip()
    meta: dict[str, Any] = {
        "appended": False,
        "conclusion": False,
        "faq_count": 0,
        "skipped_reason": None,
        "had_conclusion": False,
        "had_faq": False,
    }
    kind = str(article_type or "").upper()
    if kind in {"LIMITED_DEPTH_BRIEF", "LIMITED"}:
        meta["skipped_reason"] = "limited_article_type"
        return body, meta
    if not body:
        meta["skipped_reason"] = "empty_body"
        return body, meta

    detected = detect_closing_sections(body)
    meta["had_conclusion"] = bool(detected["conclusion"])
    meta["had_faq"] = bool(detected["faq"])
    need_conclusion = not detected["conclusion"]
    need_faq = not detected["faq"]
    if not need_conclusion and not need_faq:
        meta["skipped_reason"] = "already_present"
        return body, meta

    rows = _proposition_rows(authorized_propositions)
    if len(rows) < 2:
        meta["skipped_reason"] = "insufficient_authorized_facts"
        return body, meta

    conclusion = build_grounded_conclusion(rows, body) if need_conclusion else None
    faqs = build_grounded_faqs(rows, body) if need_faq else []
    if need_conclusion and not conclusion and need_faq and not faqs:
        meta["skipped_reason"] = "no_grounded_closing_material"
        return body, meta
    if need_conclusion and not conclusion and not need_faq:
        meta["skipped_reason"] = "no_grounded_conclusion_material"
        return body, meta
    if need_faq and not faqs and not need_conclusion:
        meta["skipped_reason"] = "no_grounded_faq_material"
        return body, meta
    if not conclusion and not faqs:
        meta["skipped_reason"] = "no_grounded_closing_material"
        return body, meta

    block = format_closing_sections(conclusion=conclusion, faqs=faqs)
    if not block:
        meta["skipped_reason"] = "empty_block"
        return body, meta

    new_body = f"{body.rstrip()}\n\n{block}".strip()
    # Guard: never leave duplicate section headings after append.
    if need_conclusion and detect_closing_sections(body)["conclusion"]:
        raise AssertionError("conclusion append attempted despite detection")
    meta.update(
        {
            "appended": True,
            "conclusion": bool(conclusion),
            "faq_count": len(faqs),
            "added_words": word_count(block),
        }
    )
    return new_body, meta
