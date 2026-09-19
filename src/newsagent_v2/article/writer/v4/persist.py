"""V4 attempt persistence. Never stores secrets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|password|app_password|(?<!prompt_)(?<!completion_)(?<!total_)secret|(?<![a-z_])token(?![a-z_]))",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(r"\b(?:sk-|gsk_|Bearer\s+)[A-Za-z0-9._\-]{8,}\b")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _redact(item)
        return out
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE_RE.sub("[REDACTED]", value)
    return value


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(_redact(payload), indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def persist_v4_attempt(
    *,
    attempts_root: Path,
    event_id: str,
    attempt: dict[str, Any],
    evidence_packet: dict[str, Any] | None = None,
    writer_request_diagnostic: dict[str, Any] | None = None,
    native: dict[str, Any] | None = None,
    article_pre_verification: dict[str, Any] | None = None,
    assertions: dict[str, Any] | None = None,
    repair_log: dict[str, Any] | None = None,
    article: dict[str, Any] | None = None,
    qa: dict[str, Any] | None = None,
) -> Path:
    dest = Path(attempts_root) / str(event_id)
    dest.mkdir(parents=True, exist_ok=True)
    _write_json(dest / "attempt.json", attempt)
    if evidence_packet is not None:
        _write_json(dest / "evidence_packet.json", evidence_packet)
    if writer_request_diagnostic is not None:
        _write_json(dest / "writer_request_diagnostic.json", writer_request_diagnostic)
    if native is not None:
        _write_json(dest / "native.json", native)
    if article_pre_verification is not None:
        _write_json(dest / "article_pre_verification.json", article_pre_verification)
    if assertions is not None:
        _write_json(dest / "assertions.json", assertions)
    if repair_log is not None:
        _write_json(dest / "repair_log.json", repair_log)
    if article is not None:
        _write_json(dest / "article.json", article)
        body = str(article.get("article_body") or "")
        (dest / "article_body.txt").write_text(body + ("\n" if body else ""), encoding="utf-8")
    if qa is not None:
        _write_json(dest / "qa.json", qa)
    # Reporting-only cost snapshot from already-persisted diagnostic usage.
    try:
        from newsagent_v2.article.writer.v4.article_cost_telemetry import (
            build_article_cost_record,
            persist_article_cost_json,
        )

        batch_id = str(attempt.get("batch_id") or attempt.get("batch_run_id") or "")
        if not batch_id:
            batch_id = (
                dest.parent.parent.name
                if dest.parent.name == "attempts"
                else dest.parent.name
            )
        diag = writer_request_diagnostic if isinstance(writer_request_diagnostic, dict) else {}
        record = build_article_cost_record(
            batch_id=batch_id,
            event_id=str(event_id),
            article=article if isinstance(article, dict) else {},
            attempt=attempt,
            diagnostic=diag,
            publishable=bool(attempt.get("ok")) if "ok" in attempt else None,
        )
        persist_article_cost_json(dest, record)
    except Exception:
        # Telemetry must never break persistence.
        pass
    return dest
