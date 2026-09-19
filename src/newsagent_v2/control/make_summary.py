"""Truthful /make Telegram summaries. No secrets or raw provider payloads."""

from __future__ import annotations

from typing import Any

STRUCTURE_CODES = frozenset(
    {
        "paragraph_missing_claim_ids",
        "unknown_claim_id",
        "claim_missing_evidence",
        "unknown_evidence_ref",
        "foreign_evidence_ref",
        "orphan_claim",
        "quote_body_unmapped",
    }
)


def _short(value: Any, *, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return "unknown"
    return text[:limit]


def _editorial(telemetry: dict[str, Any] | None) -> dict[str, Any]:
    payload = telemetry or {}
    editorial = payload.get("editorial")
    return editorial if isinstance(editorial, dict) else {}


def _http_label(editorial: dict[str, Any]) -> str:
    status = editorial.get("http_status")
    return str(status) if status is not None else "n/a"


def _provider_code(editorial: dict[str, Any]) -> str:
    error = editorial.get("provider_error")
    if isinstance(error, dict):
        return _short(
            error.get("provider_error_code")
            or error.get("provider_error_type")
            or error.get("provider_error_message")
        )
    return "unknown"


def _has_generated(row: dict[str, Any]) -> bool:
    article = row.get("article")
    if not isinstance(article, dict):
        return False
    return bool(
        article.get("headline")
        or article.get("article_sections")
        or article.get("article_body")
    )


def _critical_codes(row: dict[str, Any]) -> list[str]:
    qa = row.get("qa_result") if isinstance(row.get("qa_result"), dict) else {}
    found: list[str] = []
    for item in qa.get("critical_failures") or []:
        if isinstance(item, dict) and item.get("code"):
            found.append(str(item["code"]))
    return found


def _word_count(row: dict[str, Any]) -> str:
    qa = row.get("qa_result") if isinstance(row.get("qa_result"), dict) else {}
    metrics = qa.get("metrics") if isinstance(qa.get("metrics"), dict) else {}
    count = metrics.get("article_word_count")
    return str(count) if count is not None else "?"


def _structure_status(row: dict[str, Any]) -> str:
    codes = set(_critical_codes(row))
    if codes & STRUCTURE_CODES:
        return "FAIL"
    if _has_generated(row):
        return "PASS"
    return "n/a"


def _cards_sent(approval_cards: list[dict[str, Any]] | None) -> bool:
    return any(
        isinstance(row, dict) and row.get("ok") is not False
        for row in (approval_cards or [])
    )


def format_make_summary(
    *,
    stories: list[dict[str, Any]] | None = None,
    telemetry: dict[str, Any] | None = None,
    viability: dict[str, Any] | None = None,
    approval_cards: list[dict[str, Any]] | None = None,
) -> str:
    rows = list(stories or [])
    tel = telemetry or {}
    editorial = _editorial(tel)
    viability_payload = viability or {}
    scanned = int(viability_payload.get("scanned_count") or 0)
    selected_n = viability_payload.get("selected_count")
    if selected_n is None:
        selected_n = len(rows)
    selected_n = int(selected_n)
    generated = [row for row in rows if _has_generated(row)]
    qa_pass = [row for row in rows if row.get("qa_publishable")]
    deliverable = [row for row in rows if row.get("deliverable")]
    http_label = _http_label(editorial)
    provider_failed = bool(editorial.get("provider_error")) or (
        editorial.get("http_status") is not None and editorial.get("http_status") != 200
    )

    if selected_n == 0:
        return (
            "⚠️ V2 TOP 1 — NO VIABLE STORY\n"
            f"Scanned: {scanned}\n"
            "Evidence sufficient: 0\n"
            "Generation: not reached\n"
            "Images: not reached"
        )

    if provider_failed and not generated:
        return (
            "❌ V2 TOP 1 — EDITORIAL FAILED\n"
            f"Viable: {selected_n}/1\n"
            f"Groq: HTTP {http_label}\n"
            f"Error: {_provider_code(editorial)}\n"
            "Generated: 0\n"
            "QA: not reached\n"
            "Images: not reached"
        )

    if selected_n <= 1 and len(rows) <= 1:
        row = rows[0] if rows else {}
        if generated and not qa_pass:
            codes = _critical_codes(row)
            code_text = ", ".join(codes[:6]) if codes else _short(row.get("skip_reason") or "qa_failed")
            return (
                "⚠️ V2 TOP 1 — ARTICLE FAILED QA\n"
                "Generated: 1/1\n"
                f"Groq: HTTP {http_label}\n"
                f"Words: {_word_count(row)}\n"
                f"Structure: {_structure_status(row)}\n"
                "QA: FAIL\n"
                f"Critical: {code_text}\n"
                "Images: not reached"
            )
        if qa_pass and not deliverable:
            image = row.get("image") if isinstance(row.get("image"), dict) else {}
            err = _short(
                image.get("reason")
                or image.get("provider_error_code")
                or row.get("skip_reason")
                or "image_failed"
            )
            return (
                "⚠️ V2 TOP 1 — IMAGE FAILED\n"
                "Article: PASS\n"
                "QA: PASS\n"
                "Image provider: Cloudflare\n"
                "Image: FAIL\n"
                f"Error: {err}"
            )
        if deliverable:
            sent = "sent" if _cards_sent(approval_cards) else "not sent"
            return (
                "✅ V2 TOP 1 READY — 1/1\n"
                "Article: PASS\n"
                "Image: PASS\n"
                f"Approval card: {sent}"
            )
        if not generated:
            return (
                "❌ V2 TOP 1 — EDITORIAL FAILED\n"
                f"Viable: {selected_n}/1\n"
                f"Groq: HTTP {http_label}\n"
                f"Error: {_short(row.get('skip_reason') or _provider_code(editorial))}\n"
                "Generated: 0\n"
                "QA: not reached\n"
                "Images: not reached"
            )

    awaiting = len(deliverable)
    total = max(len(rows), int(tel.get("expected_count") or selected_n or 1))
    if awaiting == total and awaiting:
        return f"✅ V2 READY — {awaiting}/{total} awaiting approval."
    return (
        f"⚠️ V2 PARTIAL — {awaiting}/{total} READY\n"
        f"Generated: {len(generated)}\n"
        f"QA passed: {len(qa_pass)}\n"
        f"Images ready: {awaiting}"
    )
