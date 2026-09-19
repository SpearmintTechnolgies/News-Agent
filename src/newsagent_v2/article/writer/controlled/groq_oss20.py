"""Groq GPT-OSS 20B constrained ProseRenderer. One generation. No fallback."""

from __future__ import annotations

import json
from time import perf_counter
from typing import Any

from newsagent_v2.article.prompt import groq_schema_keyword_paths
from newsagent_v2.article.writer.controlled.plan import ArticlePlan
from newsagent_v2.article.writer.controlled.renderer import (
    RenderedParagraph,
    RendererResult,
    controlled_renderer_messages,
)
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.controlled.groq_structured import (
    GroqStructuredOutputError,
    assert_groq_root_object_schema,
    decode_strict_root_object,
    with_root_object_system_message,
)
from newsagent_v2.article.writer.schema import groq_controlled_v3_json_schema
from newsagent_v2.providers.groq_article import ARTICLE_MAX_COMPLETION_TOKENS, ARTICLE_TIMEOUT_SECONDS
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    GROQ_CHAT_COMPLETIONS_URL,
    GroqHttpError,
    estimate_prompt_tokens_from_bytes,
    extract_usage,
    parse_message_content,
    post_chat_completion,
    safe_chat_request_diagnostics,
)

PROVIDER_NAME = "groq"
MODEL_NAME = "openai/gpt-oss-20b"
QWEN_38_MODEL_NAME = "qwen/qwen3.8-27b"
JSON_SCHEMA_NAME = "controlled_writer_v3_prose"
# Groq qwen/qwen3.8-27b on_demand ITPM = 7000. Prior live 413 Requested = 8234.
QWEN_TOTAL_REQUEST_BUDGET = 6500
QWEN_MIN_COMPLETION_TOKENS = 1800
QWEN_PREFERRED_COMPLETION_TOKENS = 2200
QWEN_INPUT_TARGET = 4200
QWEN_INPUT_HARD_STOP = 4500


class GroqTokenBudgetError(GroqStructuredOutputError):
    """Refuses HTTP when estimated input + completion would exceed the Qwen ITPM budget."""

    def __init__(self, message: str, preflight: dict[str, Any]) -> None:
        super().__init__(message)
        self.preflight = preflight


def _est_tokens(text: str) -> int:
    return estimate_prompt_tokens_from_bytes(len((text or "").encode("utf-8")))


def estimate_qwen_request_components(body: dict[str, Any]) -> dict[str, int]:
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    system = str((messages[0] or {}).get("content") or "") if messages else ""
    user = str((messages[1] or {}).get("content") or "") if len(messages) > 1 else ""
    try:
        payload = json.loads(user) if user else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    schema = ((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}
    paras = payload.get("paragraph_plans") if isinstance(payload.get("paragraph_plans"), list) else []
    frames: list[Any] = []
    quotes: list[Any] = []
    for para in paras:
        if not isinstance(para, dict):
            continue
        frames.extend(para.get("proposition_frames") or [])
        quotes.extend(para.get("allowed_quotes") or [])
    meta = {
        key: payload.get(key)
        for key in (
            "event_id",
            "headline_requirements",
            "dek_requirements",
            "category",
            "seo_inputs",
            "requirements",
            "renderer_fact_ids",
        )
        if key in payload
    }
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    parts = {
        "system_prompt": _est_tokens(system),
        "task_instructions": _est_tokens(str(payload.get("task") or "")),
        "json_schema": _est_tokens(json.dumps(schema, **compact)),
        "article_plan_metadata": _est_tokens(json.dumps(meta, **compact)),
        "paragraph_plans": _est_tokens(json.dumps(paras, **compact)),
        "proposition_frames": _est_tokens(json.dumps(frames, **compact)),
        "quote_metadata": _est_tokens(json.dumps(quotes, **compact)),
        "user_json": _est_tokens(user),
    }
    return dict(sorted(parts.items(), key=lambda item: (-item[1], item[0])))


def apply_qwen_completion_budget(body: dict[str, Any]) -> dict[str, Any]:
    """Cap Qwen max_completion_tokens so estimated_input + completion <= 6500."""
    diag = safe_chat_request_diagnostics(body)
    estimated_input = int(diag.get("estimated_admission_tokens") or 0)
    existing = int(body.get("max_completion_tokens") or ARTICLE_MAX_COMPLETION_TOKENS)
    components = estimate_qwen_request_components(body)
    largest = {name: tokens for name, tokens in list(components.items())[:5]}
    safe_after_prompt = QWEN_TOTAL_REQUEST_BUDGET - estimated_input
    max_completion = min(existing, QWEN_PREFERRED_COMPLETION_TOKENS, max(0, safe_after_prompt))
    total = estimated_input + max_completion
    preflight = {
        "estimated_input_tokens": estimated_input,
        "estimated_prompt_tokens": int(diag.get("estimated_prompt_tokens") or 0),
        "estimated_serialized_tokens": int(diag.get("estimated_serialized_tokens") or 0),
        "max_completion_tokens": max_completion,
        "estimated_total_requested_tokens": total,
        "existing_limit": existing,
        "budget_cap": QWEN_TOTAL_REQUEST_BUDGET,
        "min_completion_required": QWEN_MIN_COMPLETION_TOKENS,
        "input_target": QWEN_INPUT_TARGET,
        "input_hard_stop": QWEN_INPUT_HARD_STOP,
        "prompt_bytes": int(diag.get("prompt_bytes") or 0),
        "serialized_bytes": int(diag.get("serialized_bytes") or 0),
        "schema_characters": int(diag.get("schema_characters") or 0),
        "components": components,
        "largest_components": largest,
        "http_allowed": True,
    }
    print(
        "QWEN_TOKEN_PREFLIGHT "
        f"estimated_input_tokens={estimated_input} "
        f"max_completion_tokens={max_completion} "
        f"estimated_total_requested_tokens={total}",
        flush=True,
    )
    if (
        estimated_input > QWEN_INPUT_HARD_STOP
        or max_completion < QWEN_MIN_COMPLETION_TOKENS
        or total > QWEN_TOTAL_REQUEST_BUDGET
    ):
        preflight["http_allowed"] = False
        message = (
            "STOP BEFORE HTTP: Qwen token budget assertion failed. "
            f"estimated_input_tokens={estimated_input} "
            f"max_completion_tokens={max_completion} "
            f"estimated_total_requested_tokens={total} "
            f"largest_components={largest}"
        )
        preflight["assert_failed"] = message
        print(f"QWEN_TOKEN_PREFLIGHT_ASSERT_FAILED {message}", flush=True)
        raise GroqTokenBudgetError(message, preflight)
    body["max_completion_tokens"] = max_completion
    preflight["max_completion_tokens"] = max_completion
    preflight["estimated_total_requested_tokens"] = estimated_input + max_completion
    return preflight


def groq_controlled_v3_request(
    plan: ArticlePlan,
    ledgers: EvidenceLedgers,
    *,
    model: str = MODEL_NAME,
) -> dict[str, Any]:
    schema = groq_controlled_v3_json_schema()
    preflight = assert_groq_root_object_schema(schema)
    if preflight["root_schema_type"] != "object" or preflight["root_array_allowed"]:
        raise GroqStructuredOutputError("refusing provider call: root schema is not a single object")
    bad = groq_schema_keyword_paths(schema)
    if bad:
        raise RuntimeError("controlled-v3 schema contains unsupported Groq keywords")
    props = schema.get("properties") or {}
    if "claims" in props or "quotes" in props or "article_body" in props:
        raise RuntimeError("controlled-v3 schema must not ask the model for a ledger")
    body: dict[str, Any] = {
        "model": model,
        "messages": with_root_object_system_message(controlled_renderer_messages(plan, ledgers)),
        "max_completion_tokens": ARTICLE_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": JSON_SCHEMA_NAME,
                "strict": True,
                "schema": schema,
            },
        },
    }
    if str(model).startswith("openai/gpt-oss"):
        body["reasoning_effort"] = DEFAULT_REASONING_EFFORT
        body["include_reasoning"] = False
    return body


def groq_gpt_oss_20b_controlled_v3_request(plan: ArticlePlan, ledgers: EvidenceLedgers) -> dict[str, Any]:
    return groq_controlled_v3_request(plan, ledgers, model=MODEL_NAME)


def parse_controlled_v3_native(native: dict[str, Any], plan: ArticlePlan) -> RendererResult:
    if not isinstance(native, dict):
        return RendererResult(ok=False, invalid_output=True, error="native is not an object", native=None)
    rows = native.get("paragraphs")
    if not isinstance(rows, list) or not rows:
        return RendererResult(
            ok=False,
            invalid_output=True,
            error="paragraphs missing",
            native=native,
        )
    by_id: dict[str, str] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("paragraph_id") or "").strip()
        sentences = item.get("sentences")
        if isinstance(sentences, list) and sentences:
            parts = [str(row.get("text") or "").strip() for row in sentences if isinstance(row, dict)]
            text = " ".join(part for part in parts if part).strip()
        else:
            text = str(item.get("text") or "").strip()
        if pid and text:
            by_id[pid] = text
            by_id[pid.lower()] = text
    if not by_id:
        return RendererResult(
            ok=False,
            invalid_output=True,
            error="paragraphs missing",
            native=native,
        )
    from newsagent_v2.article.writer.controlled.renderer_contract import (
        CROSS_PARAGRAPH_FACT,
        UNAUTHORIZED_RELATIONSHIP,
        UNKNOWN_FACT_ID,
        validate_sentence_declarations,
    )

    declaration_issues = validate_sentence_declarations(native, plan)
    hard = [
        item
        for item in declaration_issues
        if item.get("code") in {UNKNOWN_FACT_ID, CROSS_PARAGRAPH_FACT, UNAUTHORIZED_RELATIONSHIP}
    ]
    if hard:
        return RendererResult(
            ok=False,
            invalid_output=True,
            error=str(hard[0].get("code")),
            native=native,
        )
    paragraphs: list[RenderedParagraph] = []
    for para in plan.paragraph_plans:
        text = by_id.get(para.paragraph_id) or by_id.get(para.paragraph_id.lower()) or ""
        paragraphs.append(
            RenderedParagraph(paragraph_id=para.paragraph_id, text=text, subheading=para.subheading)
        )
    entities = native.get("entities") if isinstance(native.get("entities"), list) else []
    keywords = native.get("keywords") if isinstance(native.get("keywords"), list) else []
    return RendererResult(
        ok=True,
        paragraphs=paragraphs,
        headline_text=str(native.get("headline") or "").strip(),
        dek_text=str(native.get("dek") or "").strip(),
        seo_title=str(native.get("seo_title") or "").strip(),
        meta_description=str(native.get("meta_description") or "").strip(),
        slug=str(native.get("slug") or "").strip(),
        entities=[row for row in entities if isinstance(row, dict)],
        keywords=[str(item) for item in keywords if isinstance(item, str)],
        native=native,
    )


class GroqGptOss20bProseRenderer:
    """Exactly one Chat Completions call. Automatic HTTP retries = 0."""

    renderer_name = "groq_gpt_oss_20b"
    provider_name = PROVIDER_NAME
    model_name = MODEL_NAME

    def __init__(
        self,
        *,
        api_key: str,
        http_post: Any = None,
        timeout_seconds: int = ARTICLE_TIMEOUT_SECONDS,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.http_post = http_post
        self.timeout_seconds = timeout_seconds
        self.model_name = model or MODEL_NAME
        self.renderer_name = (
            "groq_qwen_38_controlled_v331"
            if self.model_name == QWEN_38_MODEL_NAME
            else "groq_gpt_oss_20b"
        )
        self.generation_calls = 0
        self.http_status: int | None = None
        self.latency_ms: int | None = None
        self.usage: dict[str, Any] = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        self.raw_payload: dict[str, Any] | None = None
        self.native: dict[str, Any] | None = None
        self.request_body: dict[str, Any] | None = None
        self.error: str | None = None
        self.schema_preflight: dict[str, Any] | None = None
        self.token_preflight: dict[str, Any] | None = None

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        if self.generation_calls >= 1:
            return RendererResult(
                ok=False,
                provider_error=True,
                error="generation budget exhausted",
            )
        try:
            self.request_body = groq_controlled_v3_request(plan, ledgers, model=self.model_name)
            self.schema_preflight = assert_groq_root_object_schema(
                ((self.request_body.get("response_format") or {}).get("json_schema") or {}).get("schema")
            )
            if self.model_name == QWEN_38_MODEL_NAME:
                self.token_preflight = apply_qwen_completion_budget(self.request_body)
        except GroqTokenBudgetError as exc:
            self.error = str(exc)[:800]
            self.token_preflight = exc.preflight
            return RendererResult(
                ok=False,
                provider_error=True,
                budget_exceeded=True,
                error=self.error,
            )
        except GroqStructuredOutputError as exc:
            self.error = str(exc)[:500]
            return RendererResult(ok=False, provider_error=True, error=self.error)
        self.generation_calls += 1
        started = perf_counter()
        try:
            result = post_chat_completion(
                self.request_body,
                api_key=self.api_key,
                url=GROQ_CHAT_COMPLETIONS_URL,
                timeout_seconds=self.timeout_seconds,
                http_post=self.http_post,
                sleep=lambda _seconds: None,
                max_attempts=1,
            )
        except Exception as exc:
            self.latency_ms = int((perf_counter() - started) * 1000)
            self.error = type(exc).__name__
            return RendererResult(ok=False, provider_error=True, error=self.error)
        self.latency_ms = int((perf_counter() - started) * 1000)
        self.http_status = result.get("status_code") if isinstance(result.get("status_code"), int) else None
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else None
        self.raw_payload = payload
        if payload:
            self.usage = extract_usage(payload)
        if not result.get("ok") or self.http_status != 200 or payload is None:
            reason = str(result.get("error") or f"HTTP {self.http_status}")
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict) and error.get("message"):
                reason = str(error["message"])[:500]
            # failed_generation is provider-rejected JSON. Do not parse it into native.
            self.error = reason
            self.native = None
            return RendererResult(ok=False, provider_error=True, error=reason)
        try:
            native = parse_message_content(payload)
            native = decode_strict_root_object(native)
        except (GroqHttpError, GroqStructuredOutputError) as exc:
            self.error = str(exc)[:500]
            return RendererResult(ok=False, invalid_output=True, error=self.error)
        parsed = parse_controlled_v3_native(native, plan)
        self.native = parsed.native
        if not parsed.ok:
            self.error = parsed.error
        return parsed
