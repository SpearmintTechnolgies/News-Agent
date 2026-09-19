"""CLI entry for one V4 architecture experiment."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as KIMI_KEY_ENV
from newsagent_v2.article.writer.v4.experiment import (
    configure_v4_kimi_primary,
    resume_v4_image_telegram,
    run_v4_experiment,
)
from newsagent_v2.article.writer.v4.writer import V4_KIMI_MODEL, assert_v4_writer_is_free
from newsagent_v2.control.__main__ import _load_environ
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, TelegramConfigError, load_telegram_config
from newsagent_v2.telegram.live_http import build_live_transport

REPO = Path(__file__).resolve().parents[4]


def _print_result(result: dict) -> int:
    status = str(result.get("status") or "")
    provider = str(result.get("provider") or "KIMI").upper()
    if result.get("ok") and status == "SUCCESS":
        print(
            json.dumps(
                {
                    "V4_KIMI_LIVE": "SUCCESS",
                    "provider": provider,
                    "event": result.get("event_id"),
                    "headline": result.get("headline"),
                    "sources": result.get("independent_sources"),
                    "unique_propositions": result.get("unique_propositions"),
                    "evidence_capacity": result.get("evidence_capacity"),
                    "article_type": result.get("article_type"),
                    "native_words": result.get("native_words") or result.get("initial_words"),
                    "final_words": result.get("final_words") or result.get("words"),
                    "grounding": result.get("grounding"),
                    "copyright": result.get("copyright"),
                    "mechanics": "PASS",
                    "security": "PASS",
                    "critical_count": result.get("critical_count", 0),
                    "warning_count": result.get("warning_count", 0),
                    "warning_codes": result.get("warning_codes") or [],
                    "qa_publishable": result.get("qa_publishable", True),
                    "article_hash": result.get("article_hash"),
                    "image": result.get("image"),
                    "Telegram": result.get("Telegram"),
                    "state": result.get("state"),
                    "Groq_calls": result.get("groq_calls", 0),
                    "Paid_private_Qwen_calls": result.get("paid_private_qwen_calls", 0),
                    "kimi_calls": result.get("kimi_calls"),
                    "model": result.get("model"),
                },
                indent=2,
                default=str,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "V4_KIMI_LIVE": result.get("status") or "NO SAFE ARTICLE",
                "provider": provider,
                "failures": result.get("failures") or [],
                "image_failure": result.get("image_failure"),
                "headline": result.get("headline"),
                "event": result.get("event_id"),
                "article_hash": result.get("article_hash"),
                "kimi_calls": result.get("kimi_calls", 0),
                "Groq_calls": result.get("groq_calls", 0),
                "Paid_private_Qwen_calls": result.get("paid_private_qwen_calls", 0),
                "status": result.get("status"),
                "attempts_root": result.get("attempts_root"),
                "batch_id": result.get("batch_id"),
            },
            indent=2,
            default=str,
        )
    )
    return 1


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    resume_dir = None
    if args and args[0] == "--resume-attempt":
        resume_dir = Path(args[1])
    load_dotenv(REPO / ".env")
    environ = configure_v4_kimi_primary(_load_environ())
    environ.setdefault("ARTICLE_MIN_WORDS", "120")
    environ.setdefault("NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS", "120")
    os.environ.setdefault("ARTICLE_MIN_WORDS", "120")
    os.environ.setdefault("NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS", "120")
    assert_v4_writer_is_free(V4_KIMI_MODEL, environ=environ)
    missing = [
        name
        for name in (TOKEN_ENV, CHAT_ENV, KIMI_KEY_ENV)
        if not str(environ.get(name) or "").strip()
    ]
    if missing:
        print("STOP. Missing environment variable(s): " + ", ".join(missing))
        return 2
    try:
        config = load_telegram_config(
            {TOKEN_ENV: environ[TOKEN_ENV], CHAT_ENV: environ[CHAT_ENV]}
        )
    except TelegramConfigError as exc:
        print(f"STOP. Config error: {exc}")
        return 2
    transport = build_live_transport(config)
    client = TelegramTestClient(config, transport=transport, live_send_enabled=True, timeout_seconds=90)
    store = ApprovalStore()
    if resume_dir is not None:
        article = json.loads((resume_dir / "article.json").read_text(encoding="utf-8"))
        qa = json.loads((resume_dir / "qa.json").read_text(encoding="utf-8"))
        attempt = json.loads((resume_dir / "attempt.json").read_text(encoding="utf-8"))
        packet_path = resume_dir / "evidence_packet.json"
        article_input = None
        if packet_path.exists():
            article_input = {"event_id": attempt.get("event_id"), "evidence": []}
        result = resume_v4_image_telegram(
            event_id=str(attempt.get("event_id") or article.get("event_id")),
            article=article,
            qa=qa,
            article_input=article_input,
            environ=environ,
            store=store,
            telegram_config=config,
            telegram_client=client,
            writer_model=str(attempt.get("writer_model") or V4_KIMI_MODEL),
            writer_calls=int(attempt.get("writer_calls") or 1),
            repair_calls=int(attempt.get("repair_calls") or 0),
        )
        return _print_result(result)
    result = run_v4_experiment(
        environ=environ,
        store=store,
        telegram_config=config,
        telegram_client=client,
    )
    return _print_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
