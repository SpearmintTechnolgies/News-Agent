"""V2 Telegram control listener. Default: no live network."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article.writer.bedrock_mantle import KEY_ENV as BEDROCK_KEY_ENV
from newsagent_v2.article.writer.qwen_vllm import BASE_ENV as QWEN_BASE_ENV, KEY_ENV as QWEN_KEY_ENV
from newsagent_v2.image.providers.cloudflare import ACCOUNT_ENV, TOKEN_ENV as CF_TOKEN_ENV
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import (
    CHAT_ENV,
    TOKEN_ENV,
    TelegramConfigError,
    load_telegram_config,
)
from newsagent_v2.telegram.contract import LIVE_SEND_ENABLED
from newsagent_v2.telegram.live_http import build_live_transport
from newsagent_v2.telegram.listener import run_listen_loop

LISTEN_COMMAND = "python -m newsagent_v2.control --listen --live"
GROQ_ENV = "GROQ_API_KEY"
REPO_ROOT = Path(__file__).resolve().parents[3]
POLL_TIMEOUT_SECONDS = 30
HTTP_TIMEOUT_SECONDS = 90

HELP = f"""
NewsAgent V2 Telegram /make listener.

Default: NO live Telegram polling, NO LLM, NO image, NO WordPress.

Start with:

  {LISTEN_COMMAND}

Required env (do not paste values):
  {TOKEN_ENV}
  {CHAT_ENV}
  {GROQ_ENV}
  {ACCOUNT_ENV}
  {CF_TOKEN_ENV}

WordPress publishing is disabled for this first live test.
APPROVE records approval only and does not create a public post.

LIVE_SEND_ENABLED default={LIVE_SEND_ENABLED}
""".strip()


def _load_environ() -> dict[str, str]:
    load_dotenv(REPO_ROOT / ".env")
    environ = {key: str(value) for key, value in os.environ.items()}
    names = (
        TOKEN_ENV,
        CHAT_ENV,
        ACCOUNT_ENV,
        CF_TOKEN_ENV,
        QWEN_KEY_ENV,
        QWEN_BASE_ENV,
        BEDROCK_KEY_ENV,
        "NEWSAGENT_V2_VERTEX_PROJECT",
        "NEWSAGENT_V2_VERTEX_LOCATION",
        "NEWSAGENT_V2_VERTEX_MODEL",
        "NEWSAGENT_V2_NANO_BANANA_MODEL",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "NEWSAGENT_V2_VERTEX_ENABLED",
    )
    if os.name == "nt":
        try:
            import winreg
        except ImportError:
            winreg = None  # type: ignore[assignment]
        if winreg is not None:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                    for name in names:
                        if str(environ.get(name, "")).strip():
                            continue
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                        except FileNotFoundError:
                            continue
                        if value:
                            environ[name] = str(value)
            except OSError:
                pass
    # Local CEO Vertex overlay: fill missing project/location/model/credentials path
    # from the known local SA file. Never embeds JSON secrets. Never copies into repo.
    from newsagent_v2.image.vertex_local_env import apply_local_vertex_environ

    return apply_local_vertex_environ(environ, persist_user_env=True)


def _missing_env_names(environ: dict[str, str]) -> list[str]:
    # Listener bootstrap still needs Telegram + legacy image/writer keys.
    # Vertex is validated separately inside the V4 final pipeline.
    names = (TOKEN_ENV, CHAT_ENV, ACCOUNT_ENV, CF_TOKEN_ENV, QWEN_KEY_ENV, QWEN_BASE_ENV, BEDROCK_KEY_ENV)
    return [name for name in names if not str(environ.get(name, "")).strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--once", action="store_true", help="Run one /make through the production pipeline")
    args = parser.parse_args(argv)
    if args.once:
        environ = _load_environ()
        missing = _missing_env_names(environ)
        if missing:
            print("STOP. Missing environment variable(s): " + ", ".join(missing))
            return 2
        try:
            config = load_telegram_config(
                {
                    TOKEN_ENV: environ[TOKEN_ENV],
                    CHAT_ENV: environ[CHAT_ENV],
                }
            )
        except TelegramConfigError as exc:
            print(f"STOP. Config error (names only): {exc}")
            return 2
        from newsagent_v2.control.live import build_live_pipeline
        from newsagent_v2.control.make import execute_make, reset_make_guard
        from newsagent_v2.article.qa.policy import (
            ARTICLE_MIN_WORDS_ENV,
            DEMO_ARTICLE_MIN_WORDS_ENV,
            resolve_article_hard_minimum_words,
        )
        from newsagent_v2.control.make_recovery import wordpress_publish_enabled

        transport = build_live_transport(config)
        client = TelegramTestClient(
            config,
            transport=transport,
            live_send_enabled=True,
            timeout_seconds=HTTP_TIMEOUT_SECONDS,
        )
        store = ApprovalStore()
        pipeline = build_live_pipeline(
            environ=environ,
            store=store,
            telegram_config=config,
            telegram_client=client,
        )
        reset_make_guard(cooldown_seconds=0)
        floor, demo = resolve_article_hard_minimum_words(environ)
        wp_on, wp_missing = wordpress_publish_enabled(environ)
        print(
            json.dumps(
                {
                    "demo_article_min_words": floor,
                    "temporary_demo_minimum_active": demo,
                    "env_keys_checked": [DEMO_ARTICLE_MIN_WORDS_ENV, ARTICLE_MIN_WORDS_ENV],
                    "wordpress_publish_enabled": wp_on,
                    "wordpress_missing": wp_missing,
                },
                indent=2,
            )
        )
        result = execute_make(
            telegram_config=config,
            client=client,
            store=store,
            pipeline=pipeline,
            environ=environ,
        )
        print(json.dumps(
            {
                "status": result.get("status") or ("SUCCESS" if result.get("ok") else "FAILED"),
                "headline": result.get("headline"),
                "words": result.get("words"),
                "coverage": result.get("coverage"),
                "exact_overlap": result.get("exact_overlap"),
                "candidate_attempts": result.get("candidate_attempts"),
                "message_id": result.get("message_id"),
                "batch_id": result.get("batch_id") or result.get("batch_run_id"),
                "bundle": result.get("bundle"),
                "state": result.get("state"),
                "writer": (result.get("telemetry") or {}).get("writer") or result.get("writer"),
                "kimi_calls": (result.get("telemetry") or {}).get("kimi_calls"),
                "failures": result.get("failures"),
                "attempts_root": (result.get("telemetry") or {}).get("attempts_root") or result.get("attempts_root"),
                "completion_text": result.get("completion_text"),
                "ok": result.get("ok"),
                "image_failure": result.get("image_failure"),
                "temporary_minimum": floor if demo else None,
                "wordpress_disabled": (result.get("telemetry") or {}).get("wordpress_disabled"),
                "wordpress_missing": (result.get("telemetry") or {}).get("wordpress_missing") or wp_missing,
                "article_hash": result.get("article_hash") or result.get("article_sha256"),
            },
            indent=2,
            default=str,
        ))
        return 0 if str(result.get("status") or "") == "SUCCESS" else 1
    if not args.listen or not args.live:
        print(HELP)
        print("Refusing to start the live Telegram listener.")
        return 2

    environ = _load_environ()
    missing = _missing_env_names(environ)
    if missing:
        print("STOP. Missing environment variable(s): " + ", ".join(missing))
        print("Do not paste secret values into chat.")
        return 2

    try:
        config = load_telegram_config(
            {
                TOKEN_ENV: environ[TOKEN_ENV],
                CHAT_ENV: environ[CHAT_ENV],
            }
        )
    except TelegramConfigError as exc:
        print(f"STOP. Config error (names only): {exc}")
        return 2

    from newsagent_v2.control.live import build_live_pipeline, resolve_wordpress_publish_fn

    transport = build_live_transport(config)
    client = TelegramTestClient(
        config,
        transport=transport,
        live_send_enabled=True,
        timeout_seconds=HTTP_TIMEOUT_SECONDS,
    )
    store = ApprovalStore()
    pipeline = build_live_pipeline(
        environ=environ,
        store=store,
        telegram_config=config,
        telegram_client=client,
    )
    wp_fn = resolve_wordpress_publish_fn(environ)
    print("NewsAgent V2 listener is running.")
    print("Target: configured Newsagent group.")
    from newsagent_v2.control.make_recovery import wordpress_publish_enabled

    wp_on, wp_missing = wordpress_publish_enabled(environ)
    print(f"WordPress publishing enabled={wp_on} missing={wp_missing}")
    print("Waiting for updates. Ctrl+C to stop.")
    try:
        run_listen_loop(
            config=config,
            client=client,
            store=store,
            pipeline=pipeline,
            wp_publish_fn=wp_fn,
            poll_timeout=POLL_TIMEOUT_SECONDS,
        )
    except KeyboardInterrupt:
        print("Listener stopped.")
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
