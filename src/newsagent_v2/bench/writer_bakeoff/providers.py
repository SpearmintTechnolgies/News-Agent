"""Bake-off provider calls. Live HTTP only when explicitly requested by the runner."""

from __future__ import annotations

import json
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.writer.prompts import article_first_messages, ledger_first_messages
from newsagent_v2.article.prompt import groq_schema_keyword_paths
from newsagent_v2.article.writer.schema import (
    gemini_article_first_schema,
    gemini_ledger_first_schema,
    groq_article_first_json_schema,
    groq_ledger_first_json_schema,
)
from newsagent_v2.article.writer.validate import validate_gemini_generate_content_body
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GEMINI_36_ARTICLE_FIRST,
    CANDIDATE_GEMINI_ARTICLE_FIRST,
    CANDIDATE_GROQ_ARTICLE_FIRST,
    GEMINI_NEXT_TEXT_MODEL,
    GEMINI_TEXT_MODEL,
    GROQ_LLAMA_33_MODEL,
    GROQ_QWEN_38_MODEL,
    GROQ_MODEL,
    MODE_ARTICLE_FIRST,
    PROVIDER_GEMINI,
    PROVIDER_GROQ,
)
from newsagent_v2.bench.writer_bakeoff.prompts import groq_article_first_schema_ok
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS
from newsagent_v2.providers.groq_editorial import (
    DEFAULT_REASONING_EFFORT,
    safe_chat_request_diagnostics,
)


def planned_candidates(*, gemini_configured: bool = True, groq_configured: bool = True) -> list[dict[str, Any]]:
    del gemini_configured, groq_configured
    return [
        {
            "candidate_id": CANDIDATE_GEMINI_ARTICLE_FIRST,
            "provider": PROVIDER_GEMINI,
            "model": GEMINI_TEXT_MODEL,
            "mode": MODE_ARTICLE_FIRST,
            "live_calls": 1,
            "source": "new_call_if_live",
            "repair_calls": 0,
        },
        {
            "candidate_id": CANDIDATE_GROQ_ARTICLE_FIRST,
            "provider": PROVIDER_GROQ,
            "model": GROQ_MODEL,
            "mode": MODE_ARTICLE_FIRST,
            "live_calls": 1,
            "source": "new_call_if_live",
            "repair_calls": 0,
        },
    ]


def estimated_live_calls(*, gemini_configured: bool = True, groq_configured: bool = True) -> int:
    del gemini_configured, groq_configured
    return 2


def next_writer_candidate() -> dict[str, Any]:
    return {
        "candidate_id": CANDIDATE_GEMINI_36_ARTICLE_FIRST,
        "provider": PROVIDER_GEMINI,
        "model": GEMINI_NEXT_TEXT_MODEL,
        "mode": MODE_ARTICLE_FIRST,
        "live_calls": 1,
        "source": "new_call_if_live",
        "repair_calls": 0,
        "selection_reason": (
            "Groq testing is closed. OpenRouter is not configured for the V2 article writer. "
            "NEWSAGENT_V2_GEMINI_API_KEY is configured. Gemini 2.5 Flash returned HTTP 404 naming gemini-3.6-flash."
        ),
    }


def replay_groq_structured(fixture: dict[str, Any]) -> dict[str, Any]:
    from newsagent_v2.bench.writer_bakeoff.contract import CANDIDATE_GROQ_STRUCTURED, MODE_STRUCTURED

    article = fixture["baseline_article"]
    article_input = fixture["article_input"]
    qa = run_article_qa(article, article_input, article_mode="normal")
    editorial = fixture["baseline_editorial"]
    return score_result(
        provider=PROVIDER_GROQ,
        model=str(editorial.get("model") or GROQ_MODEL),
        mode=MODE_STRUCTURED,
        qa=qa,
        article=article,
        article_input=article_input,
        http_status=editorial.get("http_status"),
        latency_ms=editorial.get("latency_ms"),
        retries=int(editorial.get("retries") or 0),
        prompt_tokens=editorial.get("prompt_tokens"),
        completion_tokens=editorial.get("completion_tokens"),
        total_tokens=editorial.get("total_tokens"),
        estimated_list_price_usd=editorial.get("estimated_list_price_usd"),
        provider_reported_cost_usd=editorial.get("provider_reported_cost_usd"),
        candidate_id=CANDIDATE_GROQ_STRUCTURED,
        replay=True,
    )


def groq_article_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    bad = groq_article_first_schema_ok()
    if bad:
        raise RuntimeError("article-first schema contains unsupported Groq keywords")
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    return {
        "model": GROQ_MODEL,
        "messages": article_first_messages(batch_id="writer-bakeoff", story=story),
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "include_reasoning": False,
        "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "article_first_v1",
                "strict": True,
                "schema": groq_article_first_json_schema(),
            },
        },
    }


def groq_llama_33_article_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    """Article-first JSON for Llama 3.3. Omits gpt-oss reasoning controls."""
    bad = groq_article_first_schema_ok()
    if bad:
        raise RuntimeError("article-first schema contains unsupported Groq keywords")
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    return {
        "model": GROQ_LLAMA_33_MODEL,
        "messages": article_first_messages(batch_id="writer-bakeoff", story=story),
        "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "article_first_v1",
                "strict": True,
                "schema": groq_article_first_json_schema(),
            },
        },
    }


def groq_qwen_38_ledger_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    """Ledger-first JSON for Groq-hosted Qwen 3.8. Omits gpt-oss reasoning and claim schema."""
    bad = groq_schema_keyword_paths(groq_ledger_first_json_schema())
    if bad:
        raise RuntimeError("ledger-first schema contains unsupported Groq keywords")
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    return {
        "model": GROQ_QWEN_38_MODEL,
        "messages": ledger_first_messages(batch_id="writer-bakeoff", story=story),
        "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "ledger_first_v1",
                "strict": True,
                "schema": groq_ledger_first_json_schema(),
            },
        },
    }


def groq_qwen_38_article_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    """Article-first JSON for Groq-hosted Qwen 3.8. Omits gpt-oss reasoning controls."""
    bad = groq_article_first_schema_ok()
    if bad:
        raise RuntimeError("article-first schema contains unsupported Groq keywords")
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    return {
        "model": GROQ_QWEN_38_MODEL,
        "messages": article_first_messages(batch_id="writer-bakeoff", story=story),
        "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "article_first_v1",
                "strict": True,
                "schema": groq_article_first_json_schema(),
            },
        },
    }


def gemini_article_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    messages = article_first_messages(batch_id="writer-bakeoff", story=story)
    instruction = messages[0]["content"]
    user = messages[1]["content"]
    return {
        "contents": [{"role": "user", "parts": [{"text": instruction + "\n\n" + user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": gemini_article_first_schema(),
            "maxOutputTokens": BATCH_MAX_COMPLETION_TOKENS,
        },
    }


def gemini_ledger_first_request(fixture: dict[str, Any]) -> dict[str, Any]:
    story = {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }
    messages = ledger_first_messages(batch_id="writer-bakeoff", story=story)
    instruction = messages[0]["content"]
    user = messages[1]["content"]
    return {
        "contents": [{"role": "user", "parts": [{"text": instruction + "\n\n" + user}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": gemini_ledger_first_schema(),
            "maxOutputTokens": BATCH_MAX_COMPLETION_TOKENS,
        },
    }


def gemini_text_request(fixture: dict[str, Any], *, mode: str) -> dict[str, Any]:
    del mode
    return gemini_article_first_request(fixture)


def request_diagnostics_for_plan(fixture: dict[str, Any], *, gemini_configured: bool = True) -> list[dict[str, Any]]:
    del gemini_configured
    groq_first = groq_article_first_request(fixture)
    gemini_body = gemini_article_first_request(fixture)
    serialized = json.dumps(gemini_body, ensure_ascii=False)
    return [
        {
            "candidate_id": CANDIDATE_GEMINI_ARTICLE_FIRST,
            "diagnostics": {
                "model": GEMINI_TEXT_MODEL,
                "max_completion_tokens": BATCH_MAX_COMPLETION_TOKENS,
                "serialized_bytes": len(serialized.encode("utf-8")),
                "estimated_admission_tokens": (len(serialized.encode("utf-8")) + 3) // 4,
                "schema_validation": validate_gemini_generate_content_body(gemini_body),
            },
            "output_cap": BATCH_MAX_COMPLETION_TOKENS,
        },
        {
            "candidate_id": CANDIDATE_GROQ_ARTICLE_FIRST,
            "diagnostics": safe_chat_request_diagnostics(groq_first),
            "output_cap": BATCH_MAX_COMPLETION_TOKENS,
        },
    ]
