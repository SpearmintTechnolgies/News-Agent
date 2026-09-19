"""Orchestrate V2 Telegram TEST delivery. Default path is mock/offline."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.telegram.client import TelegramLiveDisabledError, TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig, mask_token
from newsagent_v2.telegram.contract import (
    SEND_TYPE_MESSAGE,
    SEND_TYPE_PHOTO,
    TELEGRAM_MODE_TEST,
    TelegramOutbound,
    TelegramSendResult,
)
from newsagent_v2.telegram.telemetry import build_telegram_telemetry, redact_telegram_keys
from newsagent_v2.telegram.validate import TelegramValidationError, validate_outbound

DEFAULT_RUNS_ROOT = (
    Path(__file__).resolve().parents[3] / "output" / "telegram_runs"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def persist_telegram_run(
    run_dir: Path,
    *,
    request_metadata: dict[str, Any],
    formatted_payload: dict[str, Any],
    send_result: dict[str, Any],
    telemetry: dict[str, Any],
    secrets: list[str] | tuple[str, ...] = (),
) -> dict[str, str]:
    run_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "request_metadata": run_dir / "request_metadata.json",
        "formatted_payload": run_dir / "formatted_payload.json",
        "send_result": run_dir / "send_result.json",
        "telemetry": run_dir / "telemetry.json",
    }
    for name, path in paths.items():
        payload = {
            "request_metadata": request_metadata,
            "formatted_payload": formatted_payload,
            "send_result": send_result,
            "telemetry": telemetry,
        }[name]
        write_json_utf8(path, redact_telegram_keys(payload, secrets))
    return {name: str(path) for name, path in paths.items()}


def _telemetry_send_type(send_type: str | None) -> str | None:
    if send_type == SEND_TYPE_MESSAGE:
        return "message"
    if send_type == SEND_TYPE_PHOTO:
        return "photo"
    return send_type


def deliver_test_message(
    payload: TelegramOutbound,
    config: TelegramConfig,
    *,
    transport: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    persist: bool = False,
    persist_root: Path | None = None,
    now: datetime | None = None,
    repo_root: Path | None = None,
    live_test: bool = False,
) -> dict[str, Any]:
    started = perf_counter()
    timestamp = now or _utc_now()
    run_id = _run_id(timestamp)
    secrets = [config.bot_token]
    kwargs = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root

    live = bool(live_test)
    mock = not live
    offline = not live
    result_shell: dict[str, Any] = {
        "run_id": run_id,
        "mock": mock,
        "offline": offline,
        "live_test": live,
        "success": False,
    }

    def _finish(send_result: TelegramSendResult, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        latency_ms = int(round((perf_counter() - started) * 1000))
        send_result.latency_ms = latency_ms
        telemetry = build_telegram_telemetry(
            run_id=run_id,
            event_id=payload.event_id,
            telegram_mode=TELEGRAM_MODE_TEST,
            send_type=_telemetry_send_type(send_result.send_type),
            chat_id=send_result.chat_id,
            message_id=send_result.message_id,
            success=send_result.success,
            http_status=send_result.http_status,
            retry_count=send_result.retry_count,
            latency_ms=latency_ms,
            failure_reason=send_result.failure_reason,
            telegram_error_code=send_result.telegram_error_code,
            telegram_description=send_result.telegram_description,
            caption_length=send_result.caption_length,
            image_attached=send_result.image_attached,
            timestamp_utc=timestamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            mock=mock,
            offline=offline,
            secrets=secrets,
            token_fingerprint=mask_token(config.bot_token),
        )
        extra = extra or {}
        artifact_paths = None
        if persist:
            root = persist_root or DEFAULT_RUNS_ROOT
            artifact_paths = persist_telegram_run(
                root / run_id,
                request_metadata=extra.get("request_metadata") or {
                    "mock": mock,
                    "offline": offline,
                    "token_included": False,
                },
                formatted_payload=extra.get("formatted_payload") or {},
                send_result=send_result.as_dict(),
                telemetry=telemetry,
                secrets=secrets,
            )
        result_shell.update(
            {
                "success": send_result.success,
                "send_result": send_result.as_dict(),
                "telemetry": telemetry,
                "artifact_paths": artifact_paths,
                "validation_error": extra.get("validation_error"),
            }
        )
        result_shell.update({k: v for k, v in extra.items() if k not in result_shell})
        return result_shell

    try:
        checked = validate_outbound(payload, config, **kwargs)
    except TelegramValidationError as exc:
        send_result = TelegramSendResult(
            success=False,
            chat_id=config.test_chat_id,
            failure_reason=f"{exc.code}: {exc.message}",
            http_called=False,
            mock=mock,
            offline=offline,
        )
        return _finish(send_result, {"validation_error": {"code": exc.code, "message": exc.message}})

    formatted = checked["formatted"]
    text = str(formatted["text"])
    chat_id = str(checked["chat_id"])
    send_photo = bool(checked["send_photo"])
    send_type = SEND_TYPE_PHOTO if send_photo else SEND_TYPE_MESSAGE

    if live and transport is None:
        from time import sleep as real_sleep

        from newsagent_v2.telegram.live_http import build_live_transport

        transport = build_live_transport(config)
        if sleep is None:
            sleep = real_sleep

    client = TelegramTestClient(
        config,
        transport=transport,
        sleep=sleep,
        live_send_enabled=live,
    )
    metadata = client.public_request_metadata(
        send_type,
        {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "text_or_caption": text,
            "image_attached": send_photo,
        },
    )
    extra = {
        "request_metadata": metadata,
        "formatted_payload": {
            "mock": mock,
            "offline": offline,
            "send_type": send_type,
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "length": formatted["length"],
            "truncated": formatted["truncated"],
            "image_attached": send_photo,
            "image_path_omitted": True,
        },
    }

    try:
        if send_photo:
            image_path = Path(str(checked["image_path"]))
            http_result = client.send_photo(
                chat_id=chat_id,
                photo_name=image_path.name,
                photo_bytes=image_path.read_bytes(),
                caption=text,
            )
        else:
            http_result = client.send_message(chat_id=chat_id, text=text)
    except TelegramLiveDisabledError as exc:
        send_result = TelegramSendResult(
            success=False,
            chat_id=chat_id,
            send_type=send_type,
            caption_length=len(text),
            image_attached=send_photo,
            failure_reason=str(exc),
            http_called=False,
            mock=mock,
            offline=offline,
        )
        return _finish(send_result, extra)

    send_result = TelegramSendResult(
        success=bool(http_result.get("ok")),
        message_id=http_result.get("message_id") if isinstance(http_result.get("message_id"), int) else None,
        chat_id=chat_id,
        http_status=http_result.get("status_code") if isinstance(http_result.get("status_code"), int) else None,
        retry_count=int(http_result.get("retry_count") or 0),
        failure_reason=None if http_result.get("ok") else str(http_result.get("error") or "send failed"),
        telegram_error_code=(
            http_result.get("telegram_error_code")
            if isinstance(http_result.get("telegram_error_code"), int)
            else None
        ),
        telegram_description=(
            http_result.get("telegram_description")
            if isinstance(http_result.get("telegram_description"), str)
            else None
        ),
        send_type=send_type,
        caption_length=len(text),
        image_attached=send_photo,
        http_called=True,
        mock=mock,
        offline=offline,
    )
    return _finish(send_result, extra)
