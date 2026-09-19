"""Offline V4 provider selection + bounded infrastructure failover tests.

No live provider calls. No Kimi. No paid private Qwen.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_FALLBACK_MODEL,
    ENV_FALLBACK_PROVIDER,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    INFRA_MODEL_UNAVAILABLE,
    INFRA_OTHER,
    INFRA_RATE_LIMIT,
    INFRA_TIMEOUT,
    KNOWN_GROQ_QWEN_OTPM_LIMIT,
    V4_MAX_COMPLETION_TOKENS,
    classify_provider_error,
    resolve_v4_provider_specs,
    sanitize_provider_log_blob,
    transport_request_contains_no_secrets,
)
from newsagent_v2.article.writer.v4.writer import (
    FailoverV4Writer,
    V4NaturalProseWriter,
    V4WriterResult,
    build_v4_writer,
    parse_v4_native,
)


def _packet(event_id: str = "event-test") -> WriterEvidencePacket:
    return WriterEvidencePacket(
        event_id=event_id,
        story_topic="Test topic",
        authorized_facts=(
            AuthorizedFact(
                id="C01",
                proposition="Alpha Corp disclosed a $10 million filing on Tuesday.",
                numbers=("$10 million",),
                entities=("Alpha Corp",),
            ),
        ),
    )


def _native_payload() -> dict[str, Any]:
    body = (
        "Alpha Corp disclosed a $10 million filing on Tuesday. "
        "The company said the disclosure covers the planned service arrangement. "
        "No other material terms were announced in the filing. "
    )
    # Pad lightly to look like a short article without implying live QA.
    body = (body * 8).strip()
    return {
        "headline": "Alpha Corp Discloses $10 Million Filing",
        "dek": "Alpha Corp disclosed a $10 million filing on Tuesday.",
        "article_body": body,
        "seo_title": "Alpha Corp Discloses $10 Million Filing",
        "meta_description": "Alpha Corp disclosed a $10 million filing on Tuesday.",
        "slug": "alpha-corp-10m-filing",
    }


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _ScriptedTransport:
    provider_name = "scripted"
    model = "scripted-model"
    api_key = "test-key"

    def __init__(self, results: list[V4WriterResult | dict[str, Any] | Exception]):
        self._results = list(results)
        self.calls = 0
        self.last_messages: list[dict[str, str]] | None = None
        self.last_bodies: list[dict[str, Any]] = []

    def complete(self, *, messages, body_extra=None, max_completion_tokens=3500, temperature=0.3):
        from newsagent_v2.article.writer.v4.provider import ChatCompletionResult

        del max_completion_tokens, temperature
        self.calls += 1
        self.last_messages = list(messages)
        body = {"messages": messages, "model": self.model}
        if body_extra:
            body.update(body_extra)
        self.last_bodies.append(body)
        if not self._results:
            return ChatCompletionResult(ok=False, error="exhausted", error_type=INFRA_OTHER, provider=self.provider_name, model=self.model)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, V4WriterResult):
            if item.ok and item.native:
                return ChatCompletionResult(
                    ok=True,
                    content=item.native.as_dict(),
                    payload={"choices": [{"message": {"content": json.dumps(item.native.as_dict())}}]},
                    provider=self.provider_name,
                    model=self.model,
                )
            return ChatCompletionResult(
                ok=False,
                error=item.error or "fail",
                error_type=item.error_type or INFRA_OTHER,
                status_code=429 if item.error_type == INFRA_RATE_LIMIT else 500,
                provider=self.provider_name,
                model=self.model,
            )
        # dict script: either success payload or error descriptor
        if item.get("ok"):
            content = item.get("content") or _native_payload()
            return ChatCompletionResult(
                ok=True,
                content=content,
                payload={"choices": [{"message": {"content": json.dumps(content)}}]},
                provider=self.provider_name,
                model=self.model,
                usage=dict(item.get("usage") or {}),
            )
        return ChatCompletionResult(
            ok=False,
            error=str(item.get("error") or "fail"),
            error_type=str(item.get("error_type") or INFRA_OTHER),
            status_code=item.get("status_code"),
            provider=self.provider_name,
            model=self.model,
            retry_after=item.get("retry_after"),
        )


class TestV4ProviderFailover(unittest.TestCase):
    def test_otpm_max_completion_tokens_safe(self) -> None:
        self.assertLess(V4_MAX_COMPLETION_TOKENS, KNOWN_GROQ_QWEN_OTPM_LIMIT)
        self.assertGreaterEqual(V4_MAX_COMPLETION_TOKENS, 500)
        # Guard the live failure mode: Requested 1076 > Limit 1000.
        self.assertLessEqual(V4_MAX_COMPLETION_TOKENS, 900)

    def test_provider_selection_config_driven(self) -> None:
        env = {
            ENV_PROVIDER: "groq",
            ENV_MODEL: "qwen/qwen3.8-27b",
            ENV_FALLBACK_PROVIDER: "groq",
            ENV_FALLBACK_MODEL: "openai/gpt-oss-20b",
            ENV_MAX_PROVIDER_ATTEMPTS: "2",
            "GROQ_API_KEY": "test-not-real",
        }
        specs = resolve_v4_provider_specs(env)
        self.assertEqual(specs["primary"].provider, "groq")
        self.assertEqual(specs["primary"].model, "qwen/qwen3.8-27b")
        self.assertEqual(specs["fallback"].provider, "groq")
        self.assertEqual(specs["fallback"].model, "openai/gpt-oss-20b")
        writer = build_v4_writer(environ=env, enable_failover=True, http_post=lambda *a, **k: None)
        self.assertIsInstance(writer, FailoverV4Writer)
        self.assertEqual(writer.primary.model, "qwen/qwen3.8-27b")
        self.assertEqual(writer.fallback.model, "openai/gpt-oss-20b")

    def test_provider_interface_independence(self) -> None:
        # Pipeline consumes .render(packet) only â€” no provider-specific clients in compile.
        import inspect
        from newsagent_v2.article.writer.v4 import compile as compile_mod

        src = inspect.getsource(compile_mod)
        self.assertNotIn("GROQ_CHAT_COMPLETIONS_URL", src)
        self.assertNotIn("moonshot", src.lower())
        self.assertNotIn("qwen_vllm", src.lower())
        self.assertNotIn("post_chat_completion", src)

        transport = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        writer = V4NaturalProseWriter(transport=transport, http_post=lambda *a, **k: None)
        result = writer.render(_packet("evt-iface"))
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.native)

    def test_rate_limit_failover(self) -> None:
        # Non-Groq primary â†’ Groq fallback remains OTPM-safe to attempt.
        primary_t = _ScriptedTransport(
            [{"ok": False, "error": "OTPM rate limit", "error_type": INFRA_RATE_LIMIT, "status_code": 429}]
        )
        primary_t.provider_name = "qwen_vllm"
        fallback_t = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        primary = V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None)
        primary.provider = "qwen_vllm"
        primary.model = "qwen/qwen3.8-27b"
        fallback = V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None)
        fallback.provider = "groq"
        fallback.model = "openai/gpt-oss-20b"
        writer = FailoverV4Writer(primary=primary, fallback=fallback, max_provider_attempts=2)
        packet = _packet("evt-rl")
        result = writer.render(packet)
        self.assertTrue(result.ok)
        self.assertEqual(result.provider_attempts, 2)
        self.assertEqual(primary_t.calls, 1)
        self.assertEqual(fallback_t.calls, 1)
        self.assertTrue(writer.last_failover_trace[0]["failover_eligible"])

    def test_same_org_groq_otpm_skips_failover(self) -> None:
        primary_t = _ScriptedTransport(
            [{"ok": False, "error": "OTPM", "error_type": INFRA_RATE_LIMIT, "status_code": 429}]
        )
        fallback_t = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        primary = V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None)
        primary.provider = "groq"
        primary.model = "qwen/qwen3.8-27b"
        fallback = V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None)
        fallback.provider = "groq"
        fallback.model = "openai/gpt-oss-20b"
        writer = FailoverV4Writer(primary=primary, fallback=fallback, max_provider_attempts=2)
        result = writer.render(_packet("evt-otpm"))
        self.assertFalse(result.ok)
        self.assertEqual(fallback_t.calls, 0)
        self.assertFalse(writer.last_failover_trace[0]["failover_eligible"])
        self.assertIn("same_org_groq_failover_skipped", result.error or "")

    def test_timeout_failover(self) -> None:
        primary_t = _ScriptedTransport(
            [{"ok": False, "error": "timed out", "error_type": INFRA_TIMEOUT, "status_code": 408}]
        )
        fallback_t = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        writer = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
        )
        result = writer.render(_packet("evt-to"))
        self.assertTrue(result.ok)
        self.assertEqual(result.provider_attempts, 2)

    def test_model_unavailable_failover(self) -> None:
        primary_t = _ScriptedTransport(
            [
                {
                    "ok": False,
                    "error": "model_not_found",
                    "error_type": INFRA_MODEL_UNAVAILABLE,
                    "status_code": 404,
                }
            ]
        )
        fallback_t = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        writer = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
        )
        result = writer.render(_packet("evt-mu"))
        self.assertTrue(result.ok)
        self.assertEqual(classify_provider_error("model_not_found", status_code=404), INFRA_MODEL_UNAVAILABLE)

    def test_content_failure_no_failover(self) -> None:
        primary_t = _ScriptedTransport(
            [{"ok": False, "error": "invalid json body", "error_type": INFRA_OTHER, "status_code": 200}]
        )
        fallback_t = _ScriptedTransport([{"ok": True, "content": _native_payload()}])
        writer = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
        )
        # Simulate non-provider content-ish failure: provider_error False path via invalid_output
        # Primary returns OTHER with provider_error True but OTHER not in INFRA_FAILOVER_TYPES.
        result = writer.render(_packet("evt-content"))
        self.assertFalse(result.ok)
        self.assertEqual(fallback_t.calls, 0)
        self.assertEqual(result.provider_attempts, 1)

    def test_bounded_attempts(self) -> None:
        primary_t = _ScriptedTransport(
            [{"ok": False, "error": "rate", "error_type": INFRA_RATE_LIMIT, "status_code": 429}]
        )
        fallback_t = _ScriptedTransport(
            [{"ok": False, "error": "rate2", "error_type": INFRA_RATE_LIMIT, "status_code": 429}]
        )
        writer = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
            max_provider_attempts=2,
        )
        result = writer.render(_packet("evt-bound"))
        self.assertFalse(result.ok)
        self.assertEqual(primary_t.calls + fallback_t.calls, 2)
        self.assertEqual(result.provider_attempts, 2)
        # max clamped
        writer2 = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
            max_provider_attempts=99,
        )
        self.assertEqual(writer2.max_provider_attempts, 2)

    def test_same_candidate_preserved(self) -> None:
        seen: list[str] = []

        class TrackingTransport(_ScriptedTransport):
            def complete(self, *, messages, body_extra=None, max_completion_tokens=3500, temperature=0.3):
                # Extract event_id from packet embedded in user message.
                user = messages[1]["content"]
                payload = json.loads(user)
                seen.append(payload["evidence_packet"]["event_id"])
                return super().complete(
                    messages=messages,
                    body_extra=body_extra,
                    max_completion_tokens=max_completion_tokens,
                    temperature=temperature,
                )

        primary_t = TrackingTransport(
            [{"ok": False, "error": "timeout", "error_type": INFRA_TIMEOUT, "status_code": 408}]
        )
        fallback_t = TrackingTransport([{"ok": True, "content": _native_payload()}])
        writer = FailoverV4Writer(
            primary=V4NaturalProseWriter(transport=primary_t, http_post=lambda *a, **k: None),
            fallback=V4NaturalProseWriter(transport=fallback_t, http_post=lambda *a, **k: None),
        )
        packet = _packet("candidate-042")
        result = writer.render(packet)
        self.assertTrue(result.ok)
        self.assertEqual(seen, ["candidate-042", "candidate-042"])
        self.assertEqual(writer.last_failover_trace[0]["same_candidate_packet_event_id"], "candidate-042")

    def test_no_secret_logging(self) -> None:
        secret = "TEST_LIVE_SECRET_VALUE"
        blob = sanitize_provider_log_blob(f"Authorization: Bearer {secret} api_key={secret}")
        self.assertNotIn(secret, blob)
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        self.assertTrue(transport_request_contains_no_secrets(body, (secret,)))
        # Paid/Kimi stay idle without allow flags
        specs = resolve_v4_provider_specs(
            {
                ENV_PROVIDER: "qwen_vllm",
                ENV_ALLOW_PAID_QWEN: "0",
                ENV_ALLOW_KIMI: "0",
            }
        )
        self.assertFalse(specs["primary"].allowed)
        self.assertFalse(specs["allow_kimi"])


if __name__ == "__main__":
    unittest.main()


