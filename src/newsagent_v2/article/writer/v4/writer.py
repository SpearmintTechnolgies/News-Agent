"""V4 natural prose writer. Provider-agnostic; selection via env/config only."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Mapping

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import (
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    INFRA_AUTH,
    INFRA_FAILOVER_TYPES,
    INFRA_OTHER,
    INFRA_RATE_LIMIT,
    PROVIDER_KIMI,
    V4_MAX_COMPLETION_TOKENS,
    ChatTransport,
    GroqChatTransport,
    build_transport,
    classify_provider_error,
    reasoning_effort_for_model,
    resolve_v4_provider_specs,
    same_org_groq_failover_is_otpm_safe,
    sanitize_provider_log_blob,
)
from newsagent_v2.providers.groq_editorial import (
    parse_message_content,
    resolve_groq_api_key,
)

# Defaults when env is unset. Live Kimi validation sets provider via env.
V4_WRITER_PROVIDER = "groq"
V4_WRITER_MODEL = "qwen/qwen3.8-27b"
V4_FALLBACK_MODEL = "openai/gpt-oss-20b"
V4_KIMI_MODEL = "moonshotai.kimi-k2.5"
# Still blocked by default; ALLOW_KIMI / ALLOW_PAID_QWEN unlock explicitly.
FORBIDDEN_WRITERS = frozenset(
    {
        "moonshotai.kimi-k2.5",
        "kimi",
        "vllm-local/qwen3.8-27b",
        "paid_qwen",
    }
)

V4_SYSTEM_PROMPT = """You are a professional crypto/news journalist for CoinNetwork.
Write this as a complete professional news article, not a summary or brief.
Use developed but concise newsroom prose with varied sentence structure and natural transitions.
Fully realize the supplied authorized facts.
You may use stylistic and grammatical connective language that introduces no new factual proposition.
Do not compress the story merely to minimize verification risk.
Do not pad with unsupported background or generic market commentary.
Aim for approximately 300 BODY words when the evidence supports that depth.
Target band: 250–400 BODY words (article_body only — not JSON wrapper tokens).
Prefer approximately 290–330 BODY words when evidence supports it.
RULES:
- use only authorized facts
- preserve numbers exactly
- preserve attribution and modality/uncertainty EXACTLY as in evidence (plan/proposed/expected/may/could/conditional must NOT become completed/active/definite/will)
- CRITICAL: When related facts contain dates, percentages, thresholds, deadlines, or conditional activation criteria, do NOT synthesize them into a new combined assertion. Use separate directly-groundable statements.
- For temporal/numeric claims: prefer atomic statements that mirror evidence structure; avoid combining date+number+condition into one invented sentence
- do not invent motives, causality, predictions, market reaction, or background
- do not invent comparisons, importance claims, or unsupported temporal relationships
- paraphrase independently; avoid copying source phrasing or long source-like phrases
- quotes only from authorized quotes, reproduced exactly
- prefer complete developed sentences over telegraphic fragments
- When authorized facts support it, end the article_body with a short grounded "Conclusion / What Happens Next" (only outcomes or next steps stated in evidence — no speculation) and 2–4 useful FAQs whose answers restate authorized facts only; omit either block if evidence cannot support it without filler or invention
- do not mention evidence IDs, prompts, system, environment, or configuration
OUTPUT: exactly one JSON object with keys:
headline, dek, article_body, seo_title, meta_description, slug
Do NOT include fact_ids_used, relationships, paragraph plans, or proof metadata.
""".strip()

V4_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "headline",
        "dek",
        "article_body",
        "seo_title",
        "meta_description",
        "slug",
    ],
    "properties": {
        "headline": {"type": "string"},
        "dek": {"type": "string"},
        "article_body": {"type": "string"},
        "seo_title": {"type": "string"},
        "meta_description": {"type": "string"},
        "slug": {"type": "string"},
    },
}


@dataclass
class V4NativeArticle:
    headline: str = ""
    dek: str = ""
    article_body: str = ""
    seo_title: str = ""
    meta_description: str = ""
    slug: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "dek": self.dek,
            "article_body": self.article_body,
            "seo_title": self.seo_title,
            "meta_description": self.meta_description,
            "slug": self.slug,
        }

    @property
    def word_count(self) -> int:
        return word_count(self.article_body)


@dataclass
class V4WriterResult:
    ok: bool
    native: V4NativeArticle | None = None
    error: str | None = None
    provider_error: bool = False
    invalid_output: bool = False
    provider: str = V4_WRITER_PROVIDER
    model: str = V4_WRITER_MODEL
    latency_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    request_diagnostic: dict[str, Any] = field(default_factory=dict)
    raw_payload: dict[str, Any] | None = None
    error_type: str | None = None
    provider_attempts: int = 1


def _env_truthy(environ: Mapping[str, str] | None, key: str) -> bool:
    if environ is not None:
        raw = str(environ.get(key) or "").strip().lower()
    else:
        raw = str(os.environ.get(key) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def assert_v4_writer_is_free(
    model: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> None:
    """Reject Kimi/paid Qwen unless the matching allow-env is explicitly set."""
    lowered = str(model or "").strip().lower()
    allow_kimi = _env_truthy(environ, ENV_ALLOW_KIMI)
    allow_paid = _env_truthy(environ, ENV_ALLOW_PAID_QWEN)
    if "kimi" in lowered or "moonshotai" in lowered:
        if allow_kimi:
            return
        raise RuntimeError(
            f"V4 experiment forbids writer model {model!r} "
            f"(set {ENV_ALLOW_KIMI}=true to authorize Kimi)"
        )
    if "vllm-local" in lowered or "paid_qwen" in lowered:
        if allow_paid:
            return
        raise RuntimeError(
            f"V4 experiment forbids writer model {model!r} "
            f"(set {ENV_ALLOW_PAID_QWEN}=true to authorize paid Qwen)"
        )
    for banned in FORBIDDEN_WRITERS:
        if banned in lowered:
            raise RuntimeError(f"V4 experiment forbids writer model {model!r}")


def _generation_packet_dict(packet: WriterEvidencePacket) -> dict[str, Any]:
    """Semantic facts only — never raw source prose / extracted_text / prior body."""
    return {
        "event_id": packet.event_id,
        "story_topic": packet.story_topic,
        "authorized_facts": [
            {
                "id": row.id,
                "proposition": row.proposition,
                "attribution": row.attribution,
                "numbers": list(row.numbers),
                "polarity": row.polarity,
                "modal": row.modal,
                "modality": row.modality,
                "entities": list(row.entities),
                "subject": row.subject,
                "predicate": row.predicate,
                "object": row.object,
                "time": row.time,
                "location": row.location,
            }
            for row in packet.authorized_facts
        ],
        "authorized_quotes": [row.as_dict() for row in packet.authorized_quotes],
        "authorized_entities": list(packet.authorized_entities),
        "source_context": {
            "source_names": list((packet.source_context or {}).get("source_names") or []),
            "publication_timestamps": list(
                (packet.source_context or {}).get("publication_timestamps") or []
            ),
        },
        "forbidden": list(packet.forbidden),
    }


def build_v4_writer_messages(
    packet: WriterEvidencePacket,
    *,
    regeneration: bool = False,
    article_type: str | None = None,
    word_range: tuple[int, int] | None = None,
) -> list[dict[str, str]]:
    prefer = word_range or (290, 330)
    hard = (250, 400) if (article_type or "").upper() in {"", "FULL_ARTICLE"} else prefer
    if article_type and article_type.upper() == "LIMITED_DEPTH_BRIEF":
        instruction = (
            "Write a LIMITED_DEPTH_BRIEF from authorized_facts only. "
            f"Use only what the evidence safely supports (about {prefer[0]}–{prefer[1]} BODY words). "
            "Do not invent filler to lengthen the article."
        )
    elif article_type and article_type.upper() == "STANDARD_BRIEF":
        instruction = (
            "Write a STANDARD_BRIEF from authorized_facts only. "
            f"Target about {prefer[0]}–{prefer[1]} BODY words."
        )
    elif regeneration:
        instruction = (
            "Regenerate a COMPLETE fresh newsroom article from authorized_facts only. "
            "Do not reuse any prior draft. "
            f"Target {prefer[0]}–{prefer[1]} BODY words "
            f"(hard band {hard[0]}–{hard[1]} BODY words). Develop authorized facts naturally. "
            "No unsupported significance, comparison, predictions, or invented causality. "
            "Where authorized facts support it, include a grounded Conclusion / What Happens Next "
            "and 2–4 FAQs with answers restating those facts only."
        )
    else:
        instruction = (
            "Write the article now using only authorized_facts and authorized_quotes. "
            "Where authorized facts support it, include a grounded Conclusion / What Happens Next "
            "and 2–4 FAQs with answers restating those facts only — no filler or speculation."
        )
    system = V4_SYSTEM_PROMPT
    if regeneration:
        system = (
            V4_SYSTEM_PROMPT
            + "\nThis is a FRESH regeneration. Prior article text is unavailable and must not be invented."
        )
    evidence = _generation_packet_dict(packet)
    user = {
        "instruction": instruction,
        "evidence_packet": evidence,
        "output_schema": list(V4_JSON_SCHEMA["required"]),
        "body_word_target": {
            "min": hard[0],
            "prefer_min": prefer[0],
            "prefer_max": prefer[1],
            "max": hard[1],
        },
        "article_type": article_type or "FULL_ARTICLE",
        "regeneration": bool(regeneration),
    }
    banned = {
        "extracted_text",
        "source_article_body",
        "source_sentences",
        "previous_article",
        "article_body",
        "matched_source_fragment",
        "offending_sentence",
    }
    assert not (banned & set(evidence.keys())), "generation packet must stay semantic-only"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def parse_v4_native(payload: Any) -> V4NativeArticle:
    if isinstance(payload, str):
        text = payload.strip()
        fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                payload = json.loads(text[start : end + 1])
            else:
                raise
    if not isinstance(payload, dict):
        raise ValueError("native is not an object")
    # Reject V3 proof fields if accidentally present — ignore them, do not require them.
    native = V4NativeArticle(
        headline=str(payload.get("headline") or "").strip(),
        dek=str(payload.get("dek") or "").strip(),
        article_body=str(payload.get("article_body") or "").strip(),
        seo_title=str(payload.get("seo_title") or "").strip(),
        meta_description=str(payload.get("meta_description") or "").strip(),
        slug=str(payload.get("slug") or "").strip(),
        raw=dict(payload),
    )
    if not native.article_body or not native.headline:
        raise ValueError("headline and article_body are required")
    return native


def _slugify(text: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9\s-]", "", text or "").strip().lower()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:80].strip("-") or "article"


class V4NaturalProseWriter:
    """Provider-agnostic prose writer. Provider selected via transport/config only."""

    renderer_name = "v4_natural_prose_writer"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        environ: dict[str, str] | None = None,
        http_post: Callable[..., Any] | None = None,
        model: str | None = None,
        max_calls: int = 3,
        timeout_seconds: int = 120,
        transport: ChatTransport | None = None,
        provider_name: str | None = None,
        event_id: str | None = None,
    ) -> None:
        self.environ = environ
        self.http_post = http_post
        self.max_calls = max(1, int(max_calls))
        self.timeout_seconds = timeout_seconds
        self.generation_calls = 0
        self.last_result: V4WriterResult | None = None
        self.event_id = event_id
        if transport is not None:
            self.transport = transport
            self.provider = getattr(transport, "provider_name", provider_name or DEFAULT_PROVIDER)
            self.model = getattr(transport, "model", model or DEFAULT_MODEL)
            transport_key = getattr(transport, "api_key", None)
            if self.provider == PROVIDER_KIMI:
                self.api_key = api_key or transport_key
            else:
                self.api_key = api_key or transport_key or resolve_groq_api_key(environ)
            assert_v4_writer_is_free(self.model, environ=environ)
        else:
            # Backward-compatible default: Groq free route.
            self.api_key = api_key or resolve_groq_api_key(environ)
            self.model = model or V4_WRITER_MODEL
            self.provider = provider_name or V4_WRITER_PROVIDER
            assert_v4_writer_is_free(self.model, environ=environ)
            self.transport = GroqChatTransport(
                api_key=self.api_key or "",
                model=self.model,
                http_post=http_post,
                timeout_seconds=timeout_seconds,
            )
        # Prefer transport key for non-Groq providers (e.g. Bedrock Mantle).
        if not self.api_key:
            self.api_key = getattr(self.transport, "api_key", None) or api_key
        # Propagate event_id to Kimi transport for budget tracking
        if self.provider == PROVIDER_KIMI and self.event_id:
            if hasattr(self.transport, "event_id"):
                object.__setattr__(self.transport, "event_id", self.event_id)

    def _configured(self) -> bool:
        if self.http_post is not None:
            return True
        transport = getattr(self, "transport", None)
        if transport is not None and getattr(transport, "provider_name", "") == PROVIDER_KIMI:
            return bool(getattr(transport, "allowed", False)) and bool(
                str(getattr(transport, "api_key", None) or self.api_key or "").strip()
            )
        key = getattr(self.transport, "api_key", None) or self.api_key
        return bool(str(key or "").strip()) and str(key) not in {"blocked", ""}

    def render(
        self,
        packet: WriterEvidencePacket,
        *,
        regeneration: bool = False,
    ) -> V4WriterResult:
        if self.generation_calls >= self.max_calls:
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error="V4 writer call budget exceeded",
                provider=self.provider,
                model=self.model,
                error_type=INFRA_OTHER,
            )
            self.last_result = result
            return result
        if not self._configured():
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error=f"{self.provider} credentials missing",
                provider=self.provider,
                model=self.model,
                error_type=INFRA_AUTH,
            )
            self.last_result = result
            return result
        messages = build_v4_writer_messages(packet, regeneration=regeneration)
        diagnostic = {
            "provider": self.provider,
            "model": self.model,
            "message_roles": [row.get("role") for row in messages],
            "packet_fact_count": len(packet.authorized_facts),
            "packet_quote_count": len(packet.authorized_quotes),
            "has_fact_ids_used_requirement": False,
            "has_relationship_requirement": False,
            "body_word_target_present": "250" in messages[0]["content"] and "400" in messages[0]["content"],
            "reasoning_effort": reasoning_effort_for_model(self.model),
            "max_completion_tokens": V4_MAX_COMPLETION_TOKENS,
            "regeneration": bool(regeneration),
        }
        body_extra: dict[str, Any] = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "v4_natural_article",
                    "strict": True,
                    "schema": V4_JSON_SCHEMA,
                },
            },
        }
        self.generation_calls += 1
        started = perf_counter()

        # Add stage marker for Kimi budget tracking
        if self.provider == PROVIDER_KIMI and body_extra:
            body_extra = {**body_extra, "_stage": "initial_writer"}

        response = self.transport.complete(
            messages=messages,
            body_extra=body_extra,
            max_completion_tokens=V4_MAX_COMPLETION_TOKENS,
            temperature=0.3,
            event_id=self.event_id,
        )
        # Soft retry without json_schema if model rejects schema mode.
        if (not response.ok) and (
            "json_schema" in str(response.error or "").lower()
            or "response_format" in str(response.error or "").lower()
            or "failed to generate json" in str(response.error or "").lower()
        ):
            soft_messages = list(messages) + [
                {"role": "user", "content": "Return ONLY the JSON object. No markdown."}
            ]
            response = self.transport.complete(
                messages=soft_messages,
                body_extra={"_stage": "schema_fallback"},
                max_completion_tokens=V4_MAX_COMPLETION_TOKENS,
                temperature=0.3,
                event_id=self.event_id,
            )
            diagnostic = {**diagnostic, "soft_json_fallback": True}
        latency_ms = int((perf_counter() - started) * 1000)
        diagnostic = {
            **diagnostic,
            "finish_reason": response.finish_reason,
            "retry_after": response.retry_after,
            "request_shape": dict(response.request_shape or {}),
            "usage": dict(response.usage or {}),
        }
        if not response.ok:
            err = sanitize_provider_log_blob(str(response.error or "provider_error"))
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error=f"provider_error status={response.status_code} {err}"[:500],
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                request_diagnostic=diagnostic,
                error_type=response.error_type
                or classify_provider_error(err, status_code=response.status_code),
                usage=dict(response.usage or {}),
            )
            self.last_result = result
            return result
        try:
            content = response.content
            if content is None and isinstance(response.payload, dict):
                content = parse_message_content(response.payload)
            native = parse_v4_native(content)
            if not native.slug:
                native.slug = _slugify(native.headline)
            if not native.seo_title:
                native.seo_title = native.headline[:70]
            if not native.meta_description:
                native.meta_description = (native.dek or native.article_body)[:155]
            result = V4WriterResult(
                ok=True,
                native=native,
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                usage=dict(response.usage or {}),
                request_diagnostic=diagnostic,
                raw_payload={"content_type": type(content).__name__, "finish_reason": response.finish_reason},
            )
            self.last_result = result
            return result
        except Exception as exc:  # noqa: BLE001 — convert to structured failure
            result = V4WriterResult(
                ok=False,
                error=sanitize_provider_log_blob(str(exc))[:400],
                provider_error=False,
                invalid_output=True,
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                request_diagnostic=diagnostic,
                error_type=INFRA_OTHER,
            )
            self.last_result = result
            return result


class FailoverV4Writer:
    """
    Bounded provider failover for infrastructure errors only.

    Content failures (grounding/copyright/mechanics/security/editorial) never failover.
    """

    renderer_name = "v4_failover_prose_writer"

    def __init__(
        self,
        *,
        primary: V4NaturalProseWriter,
        fallback: V4NaturalProseWriter | None = None,
        max_provider_attempts: int = 2,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.max_provider_attempts = max(1, min(2, int(max_provider_attempts)))
        self.generation_calls = 0
        self.last_result: V4WriterResult | None = None
        self.last_failover_trace: list[dict[str, Any]] = []

    @property
    def model(self) -> str:
        return self.primary.model

    @property
    def provider(self) -> str:
        return self.primary.provider

    @property
    def api_key(self) -> str | None:
        return getattr(self.primary, "api_key", None)

    @property
    def http_post(self) -> Callable[..., Any] | None:
        return getattr(self.primary, "http_post", None)

    @property
    def timeout_seconds(self) -> int:
        return int(getattr(self.primary, "timeout_seconds", 120) or 120)

    @property
    def transport(self) -> ChatTransport:
        return self.primary.transport

    def render(self, packet: WriterEvidencePacket, *, regeneration: bool = False) -> V4WriterResult:
        self.last_failover_trace = []
        writers: list[V4NaturalProseWriter] = [self.primary]
        if self.fallback is not None and self.max_provider_attempts >= 2:
            writers.append(self.fallback)
        writers = writers[: self.max_provider_attempts]
        last: V4WriterResult | None = None
        for idx, writer in enumerate(writers):
            self.generation_calls += 1
            result = writer.render(packet, regeneration=regeneration)
            last = result
            attempt = {
                "attempt": idx + 1,
                "provider": writer.provider,
                "model": writer.model,
                "ok": result.ok,
                "error_type": result.error_type,
                "error": (result.error or "")[:200],
                "retry_after": (result.request_diagnostic or {}).get("retry_after"),
                "failover_eligible": bool(
                    (not result.ok)
                    and result.provider_error
                    and (result.error_type in INFRA_FAILOVER_TYPES)
                    and same_org_groq_failover_is_otpm_safe(
                        primary_provider=self.primary.provider,
                        fallback_provider=writer.provider
                        if idx == 0 and self.fallback is not None
                        else (self.fallback.provider if self.fallback else writer.provider),
                        error_type=result.error_type,
                    )
                ),
                "same_candidate_packet_event_id": packet.event_id,
            }
            # Recompute eligibility against the *next* writer (fallback).
            if (
                (not result.ok)
                and result.provider_error
                and result.error_type in INFRA_FAILOVER_TYPES
                and self.fallback is not None
                and idx == 0
            ):
                attempt["failover_eligible"] = same_org_groq_failover_is_otpm_safe(
                    primary_provider=self.primary.provider,
                    fallback_provider=self.fallback.provider,
                    error_type=result.error_type,
                )
            self.last_failover_trace.append(attempt)
            if result.ok:
                result.provider_attempts = idx + 1
                self.last_result = result
                return result
            if not result.provider_error or result.error_type not in INFRA_FAILOVER_TYPES:
                result.provider_attempts = idx + 1
                self.last_result = result
                return result
            if idx == 0 and self.fallback is not None and not attempt["failover_eligible"]:
                # Same-org Groq OTPM: do not burn fallback.
                if result.error_type == INFRA_RATE_LIMIT:
                    result.error = (
                        f"{result.error or 'RATE_LIMITED'}; "
                        f"retry_after={(result.request_diagnostic or {}).get('retry_after')}; "
                        "same_org_groq_failover_skipped"
                    )[:500]
                result.provider_attempts = idx + 1
                self.last_result = result
                return result
            if idx >= len(writers) - 1:
                result.provider_attempts = idx + 1
                self.last_result = result
                return result
        assert last is not None
        self.last_result = last
        return last


def build_v4_writer(
    *,
    api_key: str | None = None,
    environ: dict[str, str] | None = None,
    http_post: Callable[..., Any] | None = None,
    enable_failover: bool = True,
    max_calls: int | None = None,
    event_id: str | None = None,
) -> V4NaturalProseWriter | FailoverV4Writer:
    env = dict(environ or {})
    specs = resolve_v4_provider_specs(env)
    primary_spec = specs["primary"]
    fallback_spec = specs["fallback"]
    max_attempts = int(specs.get("max_provider_attempts") or 2)
    # Reduced from 24 to safer defensive secondary limit
    calls = max(1, int(max_calls if max_calls is not None else 3))
    assert_v4_writer_is_free(primary_spec.model, environ=env)

    primary_transport = build_transport(
        provider=primary_spec.provider,
        model=primary_spec.model,
        environ=env,
        http_post=http_post,
        allow_paid_qwen=bool(specs.get("allow_paid_qwen")),
        allow_kimi=bool(specs.get("allow_kimi")),
        timeout_seconds=180 if primary_spec.provider == PROVIDER_KIMI else 120,
    )
    # Propagate event_id to Kimi transport
    if primary_spec.provider == PROVIDER_KIMI and event_id:
        if hasattr(primary_transport, "event_id"):
            object.__setattr__(primary_transport, "event_id", event_id)

    if api_key and primary_spec.provider == "groq":
        primary_transport = GroqChatTransport(
            api_key=api_key,
            model=primary_spec.model,
            http_post=http_post,
            timeout_seconds=120,
        )
    primary = V4NaturalProseWriter(
        transport=primary_transport,
        api_key=api_key,
        environ=env,
        http_post=http_post,
        max_calls=calls,
        timeout_seconds=180 if primary_spec.provider == PROVIDER_KIMI else 120,
        event_id=event_id,
    )

    # Controlled Kimi validation: never silently fall back to Groq.
    if primary_spec.provider == PROVIDER_KIMI:
        return primary
    if not enable_failover or max_attempts < 2:
        return primary
    # Same-provider model failover (e.g. Groq qwen → Groq gpt-oss) is allowed.
    if fallback_spec.provider == primary_spec.provider and fallback_spec.model == primary_spec.model:
        return primary
    if not fallback_spec.allowed:
        return primary

    fallback_transport = build_transport(
        provider=fallback_spec.provider,
        model=fallback_spec.model,
        environ=env,
        http_post=http_post,
        allow_paid_qwen=bool(specs.get("allow_paid_qwen")),
        allow_kimi=bool(specs.get("allow_kimi")),
    )
    fallback = V4NaturalProseWriter(
        transport=fallback_transport,
        api_key=api_key if fallback_spec.provider == "groq" else None,
        environ=env,
        http_post=http_post,
        max_calls=calls,
        event_id=event_id,
    )
    return FailoverV4Writer(
        primary=primary,
        fallback=fallback,
        max_provider_attempts=max_attempts,
    )


class ScriptedV4Writer:
    """Offline test double. Never calls a provider."""

    renderer_name = "scripted_v4_writer"

    def __init__(
        self,
        native: dict[str, Any] | None = None,
        *,
        error: str | None = None,
        expansion_text: str | None = None,
        regeneration_payload: dict[str, Any] | None = None,
        semantic_rewrite_text: str | None = None,
        clean_room: dict[str, Any] | None = None,
        clean_room_error: str | None = None,
    ) -> None:
        self.native_payload = native
        self.error = error
        self.expansion_text = expansion_text
        self.regeneration_payload = regeneration_payload
        self.semantic_rewrite_text = semantic_rewrite_text
        self.clean_room_payload = clean_room
        self.clean_room_error = clean_room_error
        self.generation_calls = 0
        self.regeneration_calls = 0
        self.expansion_calls = 0
        self.semantic_rewrite_calls = 0
        self.clean_room_calls = 0
        self.last_render_regeneration = False
        self.last_semantic_rewrite_props: list[str] | None = None
        self.clean_room_request_messages: list[dict[str, str]] | None = None
        self.model = "scripted"
        self.last_result: V4WriterResult | None = None

    def render(self, packet: WriterEvidencePacket, *, regeneration: bool = False) -> V4WriterResult:
        del packet
        self.generation_calls += 1
        self.last_render_regeneration = bool(regeneration)
        if regeneration:
            self.regeneration_calls += 1
        if self.error and not regeneration:
            result = V4WriterResult(ok=False, error=self.error, invalid_output=True, model=self.model)
            self.last_result = result
            return result
        payload = self.native_payload or {}
        if regeneration and self.regeneration_payload is not None:
            payload = self.regeneration_payload
        if regeneration and self.regeneration_payload is None and not payload:
            result = V4WriterResult(
                ok=False,
                error="regeneration_scripted_empty",
                invalid_output=True,
                model=self.model,
            )
            self.last_result = result
            return result
        native = parse_v4_native(payload)
        result = V4WriterResult(ok=True, native=native, model=self.model, provider="scripted")
        self.last_result = result
        return result

    def expand(self, *, native: V4NativeArticle, unused: Any, current_words: int) -> str:
        del native, unused, current_words
        self.expansion_calls += 1
        return str(self.expansion_text or "")

    def semantic_rewrite(self, proposition_texts: list[str]) -> str:
        self.semantic_rewrite_calls += 1
        self.last_semantic_rewrite_props = list(proposition_texts)
        return str(self.semantic_rewrite_text or "")

    def clean_room_render(self, packet: WriterEvidencePacket) -> V4NativeArticle | dict[str, Any]:
        from newsagent_v2.article.writer.v4.copyright_recovery import build_clean_room_messages

        self.clean_room_calls += 1
        self.clean_room_request_messages = build_clean_room_messages(packet)
        if self.clean_room_error:
            return {"error": self.clean_room_error}
        if self.clean_room_payload is None:
            return {"error": "clean_room_scripted_empty"}
        return parse_v4_native(self.clean_room_payload)
