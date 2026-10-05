"""Send EXACT frozen CanonicalArticle bodies to Telegram. No LLM. No rewrite."""

from __future__ import annotations

import hashlib
from typing import Any

from newsagent_v2.textutil import word_count
from newsagent_v2.telegram.cards import approval_keyboard, html_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.contract import TELEGRAM_MESSAGE_LIMIT


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def article_type_label(raw: str | None) -> str:
    t = str(raw or "").strip()
    mapping = {
        "FULL_ARTICLE": "FULL ARTICLE",
        "FULL ARTICLE": "FULL ARTICLE",
        "STANDARD_ARTICLE": "STANDARD ARTICLE",
        "STANDARD_BRIEF": "STANDARD ARTICLE",
        "STANDARD ARTICLE": "STANDARD ARTICLE",
        "LIMITED_BRIEF": "LIMITED BRIEF",
        "LIMITED_DEPTH_BRIEF": "LIMITED BRIEF",
        "LIMITED BRIEF": "LIMITED BRIEF",
    }
    return mapping.get(t, t or "ARTICLE")


def split_on_paragraphs(text: str, *, max_chars: int) -> list[str]:
    """Split text into chunks <= max_chars on paragraph boundaries when possible."""
    body = str(text or "")
    if len(body) <= max_chars:
        return [body] if body else [""]
    paragraphs = body.split("\n\n")
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = para if not current else f"{current}\n\n{para}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(para) <= max_chars:
            current = para
            continue
        # Hard-split oversized paragraph on newlines, then spaces.
        lines = para.split("\n")
        buf = ""
        for line in lines:
            cand2 = line if not buf else f"{buf}\n{line}"
            if len(cand2) <= max_chars:
                buf = cand2
                continue
            if buf:
                chunks.append(buf)
            if len(line) <= max_chars:
                buf = line
                continue
            start = 0
            while start < len(line):
                chunks.append(line[start : start + max_chars])
                start += max_chars
            buf = ""
        current = buf
    if current:
        chunks.append(current)
    return chunks or [""]


def build_full_article_parts(
    *,
    rank: int,
    total: int,
    headline: str,
    body: str,
    article_type: str | None,
    words: int | None = None,
) -> list[str]:
    """
    Build plain-text parts for Telegram (escaped later).

    Final part always includes Type/Words footer.
    """
    label = article_type_label(article_type)
    wc = int(words) if words is not None else word_count(body)
    header = f"📄 FULL ARTICLE — {rank}/{total}\n\n{headline.strip()}\n\n"
    footer = f"\n\nType: {label}\nWords: {wc}"
    # Reserve room for HTML escaping inflation (~nothing for ASCII) + footer on last part.
    # Escape after split; keep plain-text budget conservative.
    budget = TELEGRAM_MESSAGE_LIMIT - 80
    body_budget_first = budget - len(header)
    body_budget_cont = budget - len(f"📄 FULL ARTICLE — {rank}/{total} (continued)\n\n")
    body_budget_last_extra = len(footer)

    if len(header) + len(body) + len(footer) <= budget:
        return [f"{header}{body}{footer}"]

    # Split body so last chunk has room for footer.
    parts_body = split_on_paragraphs(body, max_chars=max(200, body_budget_cont - body_budget_last_extra))
    # Rebalance: ensure last part + footer fits; if not, re-split last.
    out: list[str] = []
    for i, chunk in enumerate(parts_body):
        is_last = i == len(parts_body) - 1
        if i == 0 and not is_last:
            text = f"{header}{chunk}"
            if len(text) > budget:
                # Oversize first — shrink via paragraph split with tighter budget.
                sub = split_on_paragraphs(chunk, max_chars=max(200, body_budget_first))
                for j, sub_chunk in enumerate(sub):
                    if j == 0:
                        out.append(f"{header}{sub_chunk}")
                    else:
                        out.append(f"📄 FULL ARTICLE — {rank}/{total} (continued)\n\n{sub_chunk}")
                continue
            out.append(text)
        elif is_last:
            prefix = header if i == 0 else f"📄 FULL ARTICLE — {rank}/{total} (continued)\n\n"
            text = f"{prefix}{chunk}{footer}"
            if len(text) <= budget:
                out.append(text)
            else:
                # Move overflow paragraphs to prior continued messages.
                room = budget - len(prefix) - len(footer)
                sub = split_on_paragraphs(chunk, max_chars=max(200, room))
                for j, sub_chunk in enumerate(sub):
                    last = j == len(sub) - 1
                    pfx = prefix if j == 0 else f"📄 FULL ARTICLE — {rank}/{total} (continued)\n\n"
                    if last:
                        out.append(f"{pfx}{sub_chunk}{footer}")
                    else:
                        out.append(f"{pfx}{sub_chunk}")
        else:
            out.append(f"📄 FULL ARTICLE — {rank}/{total} (continued)\n\n{chunk}")
    return out


def send_full_frozen_article(
    *,
    client: TelegramTestClient,
    chat_id: str,
    batch_id: str,
    event_id: str,
    rank: int,
    total: int,
    headline: str,
    body: str,
    article_type: str | None,
    expected_hash: str | None = None,
    words: int | None = None,
) -> dict[str, Any]:
    """
    Send exact frozen article body. Verifies hash when expected_hash provided.
    APPROVE/REJECT keyboard only on the final part.
    """
    body_text = str(body or "")
    actual_hash = _sha256_text(body_text)
    if expected_hash and actual_hash != str(expected_hash):
        return {
            "ok": False,
            "event_id": event_id,
            "reason": "article_hash_mismatch",
            "expected_hash": expected_hash,
            "actual_hash": actual_hash,
            "kimi_calls": 0,
            "vertex_calls": 0,
            "wordpress_calls": 0,
        }

    parts = build_full_article_parts(
        rank=rank,
        total=total,
        headline=headline,
        body=body_text,
        article_type=article_type,
        words=words,
    )
    sends: list[dict[str, Any]] = []
    final_message_id = None
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1
        markup = approval_keyboard(batch_id, event_id) if is_last else None
        result = client.send_message(
            chat_id=chat_id,
            text=html_caption(part),
            reply_markup=markup,
        )
        sends.append(result)
        if not result.get("ok"):
            return {
                "ok": False,
                "event_id": event_id,
                "reason": "telegram_send_failed",
                "part_index": i,
                "sends": sends,
                "article_hash": actual_hash,
                "kimi_calls": 0,
                "vertex_calls": 0,
                "wordpress_calls": 0,
            }
        if is_last:
            final_message_id = result.get("message_id")

    return {
        "ok": True,
        "event_id": event_id,
        "parts": len(parts),
        "telegram_message_id": final_message_id,
        "article_hash": actual_hash,
        "headline": headline,
        "body_words": words if words is not None else word_count(body_text),
        "article_type": article_type_label(article_type),
        "last_part_plain": parts[-1] if parts else "",
        "sends": sends,
        "kimi_calls": 0,
        "vertex_calls": 0,
        "wordpress_calls": 0,
    }
