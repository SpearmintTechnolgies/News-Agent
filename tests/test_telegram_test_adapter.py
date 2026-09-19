from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.telegram.client import (
    TelegramLiveDisabledError,
    TelegramTestClient,
    refuse_live_transport,
)
from newsagent_v2.telegram.config import (
    CHAT_ENV,
    TOKEN_ENV,
    TelegramConfig,
    TelegramConfigError,
    load_telegram_config,
    mask_token,
)
from newsagent_v2.telegram.contract import (
    LIVE_SEND_ENABLED,
    SEND_TYPE_MESSAGE,
    SEND_TYPE_PHOTO,
    TelegramOutbound,
)
from newsagent_v2.telegram.delivery import deliver_test_message
from newsagent_v2.telegram.formatter import format_outbound_text
from newsagent_v2.telegram.validate import TelegramValidationError, validate_outbound
from newsagent_v2.telegram.__main__ import connectivity_payload, main as telegram_main

REPO = Path(__file__).resolve().parents[1]
PIXEL = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)
TOKEN = "1234567890:AA-test-token-value-not-real"
CHAT = "-1001234567890"


def _environ(**extra: str) -> dict[str, str]:
    env = {TOKEN_ENV: TOKEN, CHAT_ENV: CHAT}
    env.update(extra)
    return env


def _config() -> TelegramConfig:
    return load_telegram_config(_environ())


def _payload(**overrides) -> TelegramOutbound:
    data = {
        "event_id": "event-021",
        "headline": "Revolut reports customer data exposure",
        "category": "security_incident",
        "dek": "A spoofed government-domain email was used to obtain customer files.",
        "source_count": 2,
        "mode": "test",
    }
    data.update(overrides)
    return TelegramOutbound(**data)


class FakeResponse:
    def __init__(self, status_code: int, payload=None, headers=None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _success_transport(message_id: int = 42):
    def transport(*args, **kwargs):
        return FakeResponse(200, {"ok": True, "result": {"message_id": message_id}})

    return transport


class ConfigTests(unittest.TestCase):
    def test_missing_token_rejected(self) -> None:
        with self.assertRaises(TelegramConfigError) as ctx:
            load_telegram_config({CHAT_ENV: CHAT})
        self.assertIn(TOKEN_ENV, str(ctx.exception))

    def test_missing_chat_id_rejected(self) -> None:
        with self.assertRaises(TelegramConfigError) as ctx:
            load_telegram_config({TOKEN_ENV: TOKEN})
        self.assertIn(CHAT_ENV, str(ctx.exception))

    def test_empty_values_rejected(self) -> None:
        with self.assertRaises(TelegramConfigError):
            load_telegram_config({TOKEN_ENV: "   ", CHAT_ENV: CHAT})
        with self.assertRaises(TelegramConfigError):
            load_telegram_config({TOKEN_ENV: TOKEN, CHAT_ENV: ""})

    def test_no_process_env_fallback(self) -> None:
        with self.assertRaises(TelegramConfigError):
            load_telegram_config(None)

    def test_token_masking(self) -> None:
        masked = mask_token(TOKEN)
        self.assertNotEqual(masked, TOKEN)
        self.assertNotIn(TOKEN, masked)
        self.assertTrue(masked.startswith(TOKEN[:3]))

    def test_test_mode_frozen_true(self) -> None:
        self.assertTrue(_config().test_mode)
        with self.assertRaises(TelegramConfigError):
            TelegramConfig(bot_token=TOKEN, test_chat_id=CHAT, test_mode=False)


class FormatterTests(unittest.TestCase):
    def test_html_escaping(self) -> None:
        payload = _payload(headline="<b>Hi & bye</b>")
        formatted = format_outbound_text(payload)
        text = str(formatted["text"])
        self.assertIn("&lt;b&gt;Hi &amp; bye&lt;/b&gt;", text)
        self.assertIn("<strong>", text)
        self.assertNotIn("<b>Hi", text)

    def test_unicode_headline(self) -> None:
        payload = _payload(headline="Revolut ãƒ‡ãƒ¼ã‚¿æš´éœ² â€” cafÃ©")
        formatted = format_outbound_text(payload)
        self.assertIn("ãƒ‡ãƒ¼ã‚¿æš´éœ²", str(formatted["text"]))
        self.assertIn("cafÃ©", str(formatted["text"]))

    def test_long_message_truncated_to_limit(self) -> None:
        payload = _payload(dek=("word " * 3000).strip())
        formatted = format_outbound_text(payload)
        self.assertLessEqual(int(formatted["length"]), 4096)
        self.assertTrue(formatted["truncated"])


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()
        self.repo = REPO
        self.pixel = REPO / "tests" / "fixtures" / "telegram" / "pixel.png"
        self.pixel.parent.mkdir(parents=True, exist_ok=True)
        if not self.pixel.is_file():
            self.pixel.write_bytes(PIXEL)

    def test_malformed_url_rejected(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(_payload(article_url="javascript:alert(1)"), self.config)
        self.assertEqual(ctx.exception.code, "malformed_url")

    def test_localhost_url_rejected(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(_payload(article_url="http://localhost:8080/draft"), self.config)
        self.assertEqual(ctx.exception.code, "localhost_url")

    def test_windows_path_leakage_rejected(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(
                _payload(dek="See C:\\NewsAgent-V2\\notes.txt for the draft."),
                self.config,
            )
        self.assertEqual(ctx.exception.code, "windows_path_leak")

    def test_outside_project_image_path_rejected(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(
                _payload(image_path=r"C:\OtherProject\photo.png"),
                self.config,
                repo_root=self.repo,
            )
        self.assertEqual(ctx.exception.code, "image_outside_repo")

    def test_missing_image_path_falls_back_to_message_mode(self) -> None:
        checked = validate_outbound(_payload(image_path=None), self.config)
        self.assertFalse(checked["send_photo"])
        self.assertIsNone(checked["image_path"])

    def test_valid_project_local_image_path_accepted(self) -> None:
        checked = validate_outbound(
            _payload(image_path=str(self.pixel)),
            self.config,
            repo_root=self.repo,
        )
        self.assertTrue(checked["send_photo"])
        self.assertTrue(str(checked["image_path"]).endswith("pixel.png"))
        self.assertNotIn(str(self.pixel), str(checked["formatted"]["text"]))

    def test_test_mode_required(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(_payload(mode="production"), self.config)
        self.assertEqual(ctx.exception.code, "test_mode_required")

    def test_wrong_chat_id_rejected(self) -> None:
        with self.assertRaises(TelegramValidationError) as ctx:
            validate_outbound(_payload(chat_id="999999"), self.config)
        self.assertEqual(ctx.exception.code, "wrong_chat_id")


class ClientAndDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = _config()
        self.pixel = REPO / "tests" / "fixtures" / "telegram" / "pixel.png"
        self.pixel.parent.mkdir(parents=True, exist_ok=True)
        self.pixel.write_bytes(PIXEL)

    def test_mock_send_message_success(self) -> None:
        result = deliver_test_message(
            _payload(),
            self.config,
            transport=_success_transport(42),
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["send_result"]["message_id"], 42)
        self.assertEqual(result["send_result"]["send_type"], SEND_TYPE_MESSAGE)
        self.assertFalse(result["send_result"]["image_attached"])
        self.assertTrue(result["send_result"]["mock"])

    def test_mock_send_photo_success(self) -> None:
        result = deliver_test_message(
            _payload(image_path=str(self.pixel)),
            self.config,
            transport=_success_transport(99),
            repo_root=REPO,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["send_result"]["send_type"], SEND_TYPE_PHOTO)
        self.assertTrue(result["send_result"]["image_attached"])
        self.assertEqual(result["send_result"]["message_id"], 99)

    def test_429_retries_once(self) -> None:
        calls: list = []
        sleeps: list[float] = []

        def transport(*args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return FakeResponse(
                    429,
                    {"ok": False, "error_code": 429, "description": "Too Many Requests"},
                    {"Retry-After": "2"},
                )
            return FakeResponse(200, {"ok": True, "result": {"message_id": 7}})

        result = deliver_test_message(
            _payload(),
            self.config,
            transport=transport,
            sleep=sleeps.append,
        )
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["send_result"]["retry_count"], 1)
        self.assertEqual(sleeps, [2.0])

    def test_500_retries_once(self) -> None:
        calls: list = []

        def transport(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                return FakeResponse(500, {"ok": False, "error_code": 500, "description": "Bad gateway"})
            return FakeResponse(200, {"ok": True, "result": {"message_id": 8}})

        result = deliver_test_message(_payload(), self.config, transport=transport)
        self.assertTrue(result["success"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["telemetry"]["retry_count"], 1)

    def test_401_no_blind_retry(self) -> None:
        calls: list = []

        def transport(*args, **kwargs):
            calls.append(1)
            return FakeResponse(
                401,
                {"ok": False, "error_code": 401, "description": "Unauthorized"},
            )

        result = deliver_test_message(_payload(), self.config, transport=transport)
        self.assertFalse(result["success"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["send_result"]["retry_count"], 0)
        self.assertEqual(result["send_result"]["telegram_error_code"], 401)

    def test_retry_count_and_message_id_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = deliver_test_message(
                _payload(),
                self.config,
                transport=_success_transport(55),
                persist=True,
                persist_root=Path(tmp),
            )
            self.assertEqual(result["telemetry"]["message_id"], 55)
            self.assertEqual(result["telemetry"]["retry_count"], 0)
            telemetry_path = Path(result["artifact_paths"]["telemetry"])
            dumped = telemetry_path.read_text(encoding="utf-8")
            self.assertIn('"message_id": 55', dumped)
            self.assertNotIn(TOKEN, dumped)
            self.assertNotIn("Authorization", dumped)

    def test_caption_length_telemetry(self) -> None:
        result = deliver_test_message(
            _payload(),
            self.config,
            transport=_success_transport(),
        )
        length = result["telemetry"]["caption_length"]
        self.assertIsInstance(length, int)
        self.assertGreater(length, 10)
        self.assertEqual(length, result["formatted_payload"]["length"])

    def test_no_token_in_telemetry(self) -> None:
        result = deliver_test_message(
            _payload(),
            self.config,
            transport=_success_transport(),
        )
        blob = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(TOKEN, blob)
        self.assertNotIn(TOKEN, json.dumps(result["telemetry"]))

    def test_no_transport_makes_no_network_call(self) -> None:
        result = deliver_test_message(_payload(), self.config)
        self.assertFalse(result["success"])
        self.assertFalse(result["send_result"]["http_called"])
        self.assertIn("disabled", result["send_result"]["failure_reason"].lower())

    def test_refuse_live_transport(self) -> None:
        with self.assertRaises(TelegramLiveDisabledError):
            refuse_live_transport("https://api.telegram.org/bot[REDACTED]/sendMessage")
        client = TelegramTestClient(self.config)
        with self.assertRaises(TelegramLiveDisabledError):
            client.send_message(chat_id=CHAT, text="hi")

    def test_default_path_still_refuses_live_http(self) -> None:
        self.assertFalse(LIVE_SEND_ENABLED)
        result = deliver_test_message(_payload(), self.config)
        self.assertFalse(result["success"])
        self.assertFalse(result["live_test"])
        self.assertTrue(result["mock"])
        self.assertTrue(result["offline"])
        self.assertFalse(result["send_result"]["http_called"])

    def test_explicit_live_test_opt_in_with_injected_transport(self) -> None:
        result = deliver_test_message(
            _payload(),
            self.config,
            transport=_success_transport(77),
            live_test=True,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["live_test"])
        self.assertFalse(result["mock"])
        self.assertFalse(result["offline"])
        self.assertEqual(result["telemetry"]["send_type"], "message")
        self.assertEqual(result["send_result"]["message_id"], 77)
        self.assertFalse(result["send_result"]["image_attached"])


class CliOptInTests(unittest.TestCase):
    def test_cli_without_live_test_flag_refuses(self) -> None:
        code = telegram_main([])
        self.assertEqual(code, 2)

    def test_connectivity_payload_is_not_article_one(self) -> None:
        payload = connectivity_payload()
        self.assertEqual(payload.event_id, "telegram-connectivity-test")
        self.assertNotEqual(payload.event_id, "event-021")
        self.assertIsNone(payload.article_url)
        self.assertIsNone(payload.image_path)
        self.assertIn("TEST", payload.dek or "")


class IsolationTests(unittest.TestCase):
    def test_no_telegram_network_or_model_imports(self) -> None:
        root = REPO / "src" / "newsagent_v2" / "telegram"
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if path.name != "live_http.py":
                self.assertNotIn("import requests", text)
                self.assertNotIn("from requests", text)
            self.assertNotIn("wordpress", text.lower())
            self.assertNotIn("newsagent_v2.providers.groq", text)
            self.assertNotIn("NewsAgent-Local", text)
            self.assertNotIn("Aadi", text)
            self.assertNotIn("Hermes", text)
            self.assertNotIn("Anime Faceless", text)
            self.assertNotIn("GenerateImage", text)

    def test_config_reads_only_v2_env_names(self) -> None:
        source = (REPO / "src" / "newsagent_v2" / "telegram" / "config.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("NEWSAGENT_V2_TELEGRAM_BOT_TOKEN", source)
        self.assertIn("NEWSAGENT_V2_TELEGRAM_TEST_CHAT_ID", source)
        self.assertNotIn("TELEGRAM_BOT_TOKEN\n", source.replace(TOKEN_ENV, ""))

    def test_persist_root_stays_in_repo_constant(self) -> None:
        from newsagent_v2.telegram.delivery import DEFAULT_RUNS_ROOT

        self.assertTrue(str(DEFAULT_RUNS_ROOT).startswith(str(REPO)))
        self.assertIn("telegram_runs", str(DEFAULT_RUNS_ROOT))


if __name__ == "__main__":
    unittest.main()


