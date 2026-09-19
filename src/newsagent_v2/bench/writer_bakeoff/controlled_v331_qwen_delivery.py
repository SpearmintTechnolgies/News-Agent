"""One V3.3.1 Groq Qwen 3.8 delivery: article → Flux → Telegram. WordPress off."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from shutil import copy2
from typing import Any

from newsagent_v2.approval.store import STATE_AWAITING_APPROVAL, STATE_GENERATED, ApprovalStore
from newsagent_v2.article.enrich import enrich_story
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.groq_oss20 import GroqGptOss20bProseRenderer
from newsagent_v2.article.writer.controlled.pipeline import compile_controlled_article
from newsagent_v2.batch.viability import resolve_candidate_scan_limit
from newsagent_v2.bench.writer_bakeoff.contract import GROQ_QWEN_38_MODEL, HARD_MIN_WORDS
from newsagent_v2.bench.writer_bakeoff.controlled_v32_gpt_oss_20b_proof import select_sufficient_story
from newsagent_v2.bench.writer_bakeoff.credentials import GROQ_KEY_ENV, load_environ
from newsagent_v2.bench.writer_bakeoff.freeze import sha256_payload
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.benchmark.telemetry import redact_secrets
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.image.artifacts import DEFAULT_RUNS_ROOT
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.brief import build_visual_brief
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH, DEFAULT_NEGATIVE_PROMPT
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.cloudflare import (
    ACCOUNT_ENV,
    DEFAULT_MODEL as FLUX_MODEL,
    TOKEN_ENV as CF_TOKEN_ENV,
    CloudflareImageProvider,
    load_cloudflare_config,
)
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.providers.groq_editorial import resolve_groq_api_key
from newsagent_v2.telegram.cards import approval_caption, approval_keyboard, html_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, load_telegram_config
from newsagent_v2.telegram.live_http import build_live_transport

REPO_ROOT = Path(__file__).resolve().parents[4]
PROOF_ROOT = REPO_ROOT / "benchmarks" / "writer_bakeoff" / "controlled_v331_qwen_delivery"
LOGO_PATH = REPO_ROOT / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _brief(article: dict[str, Any], pack: dict[str, Any]) -> Any:
    headline = str(article.get("headline") or "").strip()
    dek = str(article.get("dek") or "").strip()
    names = [
        str(row.get("name") or "").strip()
        for row in (article.get("entities") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip()
    ]
    evidence = []
    for item in pack.get("evidence") or []:
        if isinstance(item, dict):
            evidence.append(
                {
                    "evidence_id": item.get("evidence_id") or item.get("id"),
                    "source": item.get("source"),
                    "url": item.get("url"),
                }
            )
    prompt = (
        "Professional editorial-news photograph, artwork only, no writing of any kind. "
        f"Story visual: {headline}. {dek[:220]}. Photorealistic Reuters/Bloomberg 16:9, "
        "clean negative space. Do not render headlines, captions, newspapers, logos, "
        "CoinNetwork branding, or readable text."
    )
    return build_visual_brief(
        {
            "event_id": str(article.get("event_id") or ""),
            "editorial_subject": headline,
            "story_category": str(article.get("category") or "other"),
            "visual_concept": headline,
            "primary_entities": names[:4],
            "facts": [{"text": dek or headline, "kind": "fact"}] if (dek or headline) else [],
            "visual_metaphors": [
                {
                    "text": "restrained institutional news photography",
                    "kind": "visual_metaphor",
                    "not_a_factual_claim": True,
                }
            ],
            "scene": "editorial news scene matching the frozen headline",
            "composition": "wide 16:9, one clear focal scene, clean negative space",
            "mood": "serious institutional reporting",
            "lighting": "professional news feature lighting",
            "avoid": ["headline text", "captions", "logos", "CoinNetwork branding", "readable type"],
            "negative_prompt": DEFAULT_NEGATIVE_PROMPT + ", headlines, captions, logos, readable text",
            "width": CARD_WIDTH,
            "height": CARD_HEIGHT,
            "evidence": evidence,
            "provider_visual_prompt": prompt,
        }
    )


def run_delivery() -> dict[str, Any]:
    if REAL_INFERENCE_AUTHORIZED:
        return {"status": "ARTICLE FAILED", "reason": "Kimi must remain blocked"}
    env = load_environ(REPO_ROOT)
    stamp = _stamp()
    run_dir = PROOF_ROOT / "live_runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    secrets = tuple(
        str(env[name]).strip()
        for name in (GROQ_KEY_ENV, CF_TOKEN_ENV, TOKEN_ENV)
        if str(env.get(name) or "").strip()
    )
    api_key = resolve_groq_api_key(env)
    if not api_key:
        return {"status": "ARTICLE FAILED", "reason": "GROQ_API_KEY missing", "run_path": str(run_dir)}

    discovered = discover_ranked_top5()
    ranked = list(discovered.get("ranked_clusters") or [])
    selected, _selection, inspections = select_sufficient_story(
        ranked_clusters=ranked,
        stories=None,
        enrich_fn=enrich_story,
        scan_limit=resolve_candidate_scan_limit(env),
    )
    write_json_utf8(run_dir / "discovery.json", redact_secrets({"inspections": inspections}, secrets))
    if selected is None:
        return {
            "status": "ARTICLE FAILED",
            "reason": "NO_CAPACITY_SAFE_CANDIDATE",
            "run_path": str(run_dir),
        }

    story = selected["story"]
    pack = selected["assessed"]["pack"]
    renderer = GroqGptOss20bProseRenderer(api_key=api_key, model=GROQ_QWEN_38_MODEL)
    compiled = compile_controlled_article(story, renderer=renderer)
    article = compiled.article
    qa = compiled.qa or {}
    metrics = qa.get("metrics") or {}
    if article:
        write_json_utf8(run_dir / "article.json", redact_secrets(article, secrets))
        (run_dir / "article_body.txt").write_text(str(article.get("article_body") or ""), encoding="utf-8")
    if qa:
        write_json_utf8(run_dir / "qa.json", redact_secrets(qa, secrets))
    write_json_utf8(
        run_dir / "writer_telemetry.json",
        redact_secrets(
            {
                "model": renderer.model_name,
                "http_status": renderer.http_status,
                "error": renderer.error,
                "generation_calls": renderer.generation_calls,
                "usage": renderer.usage,
                "failure_class": compiled.failure_class,
                "token_preflight": renderer.token_preflight,
                "max_completion_tokens": None
                if renderer.request_body is None
                else renderer.request_body.get("max_completion_tokens"),
            },
            secrets,
        ),
    )
    if renderer.native:
        write_json_utf8(run_dir / "native.json", redact_secrets(renderer.native, secrets))

    publishable = bool(compiled.ok and qa.get("publishable") and qa.get("qa_passed"))
    words = int(metrics.get("article_word_count") or (word_count(str((article or {}).get("article_body") or "")) if article else 0))
    preflight = renderer.token_preflight or {}
    if preflight and not preflight.get("http_allowed", True):
        return {
            "status": "INPUT STILL TOO LARGE",
            "input": preflight.get("estimated_input_tokens"),
            "largest_components": preflight.get("largest_components"),
            "run_path": str(run_dir),
        }
    if not publishable or article is None:
        reason = compiled.failure_class or renderer.error or "qa_publishable=false"
        crits = [item.get("code") for item in qa.get("critical_failures") or []]
        return {
            "status": "ARTICLE FAILED",
            "reason": f"{reason} criticals={crits} words={words} http={renderer.http_status}",
            "run_path": str(run_dir),
            "headline": None if article is None else article.get("headline"),
        }

    if file_sha256(LOGO_PATH) != EXPECTED_LOGO_SHA256:
        return {"status": "IMAGE FAILED", "reason": "logo hash mismatch", "run_path": str(run_dir)}
    if not (str(env.get(ACCOUNT_ENV) or "").strip() and str(env.get(CF_TOKEN_ENV) or "").strip()):
        return {"status": "IMAGE FAILED", "reason": "Cloudflare credentials missing", "run_path": str(run_dir)}

    brief = _brief(article, pack)
    provider = CloudflareImageProvider(
        load_cloudflare_config({ACCOUNT_ENV: env[ACCOUNT_ENV], CF_TOKEN_ENV: env[CF_TOKEN_ENV]}),
        model=FLUX_MODEL,
        timeout_seconds=180,
        max_retries=0,
    )
    runs = run_provider_benchmark(provider, brief, generations=1, persist_root=DEFAULT_RUNS_ROOT, compose=False)
    run = runs[0]
    result = run.get("result") or {}
    validation = run.get("validation") or {}
    write_json_utf8(run_dir / "image_run.json", redact_secrets({"result": result, "validation": validation}, secrets))
    if not result.get("success") or not validation.get("passed"):
        return {
            "status": "IMAGE FAILED",
            "reason": str(result.get("failure_reason") or validation or "raw validation failed"),
            "run_path": str(run_dir),
        }
    raw_path = Path(str(result["raw_image_path"]))
    branded_path = run_dir / "branded.png"
    composition = compose_card(
        raw_path,
        branded_path,
        CompositionSpec(headline="", category_label=None, logo_path=LOGO_PATH, test_mode=False, logo_only=True, output_name="branded.png"),
    )
    if not (composition.get("compositor_validation") or {}).get("passed"):
        return {"status": "IMAGE FAILED", "reason": "compositor failed", "run_path": str(run_dir)}
    copy2(raw_path, run_dir / "raw.png")

    if not (str(env.get(TOKEN_ENV) or "").strip() and str(env.get(CHAT_ENV) or "").strip()):
        return {"status": "TELEGRAM FAILED", "reason": "Telegram credentials missing", "run_path": str(run_dir)}

    event_id = str(article.get("event_id") or story.get("event_id") or "event")
    batch_id = f"v331qwen-{stamp}"
    article_hash = sha256_payload(article)
    image_hash = file_sha256(branded_path)
    caption = approval_caption(rank=1, headline=str(article.get("headline") or ""), dek=str(article.get("dek") or ""), total=1)
    store = ApprovalStore()
    store.write_story(
        batch_id,
        event_id,
        {
            "event_id": event_id,
            "batch_id": batch_id,
            "state": STATE_GENERATED,
            "article": deepcopy(article),
            "article_sha256": article_hash,
            "qa_result": deepcopy(qa),
            "qa_publishable": True,
            "final_image_path": str(branded_path),
            "image_sha256": image_hash,
            "frozen_bundle_path": str(run_dir),
            "no_regeneration": True,
            "allow_wordpress": False,
            "publish_on_approve": False,
            "wordpress_disabled": True,
            "writer_model": GROQ_QWEN_38_MODEL,
        },
        secrets,
    )
    tg = load_telegram_config({TOKEN_ENV: env[TOKEN_ENV], CHAT_ENV: env[CHAT_ENV]})
    client = TelegramTestClient(tg, transport=build_live_transport(tg), live_send_enabled=True, timeout_seconds=90)
    sent = client.send_photo(
        chat_id=tg.test_chat_id,
        photo_name="branded.png",
        photo_bytes=branded_path.read_bytes(),
        caption=html_caption(caption),
        reply_markup=approval_keyboard(batch_id, event_id),
    )
    if not sent.get("ok"):
        return {
            "status": "TELEGRAM FAILED",
            "reason": str(sent.get("error") or sent.get("description") or "send_photo failed"),
            "run_path": str(run_dir),
        }
    message_id = sent.get("message_id")
    store.cas_story_state(
        batch_id,
        event_id,
        expected=STATE_GENERATED,
        new_state=STATE_AWAITING_APPROVAL,
        extra={"telegram_message_id": message_id, "telegram_send_ok": True, "caption": caption},
    )
    bundle = {
        "event_id": event_id,
        "article_sha256": article_hash,
        "image_sha256": image_hash,
        "qa_publishable": True,
        "telegram_message_id": message_id,
        "state": STATE_AWAITING_APPROVAL,
        "writer": GROQ_QWEN_38_MODEL,
        "image_model": FLUX_MODEL,
        "wordpress_disabled": True,
    }
    write_json_utf8(run_dir / "bundle.json", redact_secrets(bundle, secrets))
    return {
        "status": "SUCCESS",
        "headline": article.get("headline"),
        "words": words,
        "coverage": metrics.get("body_claim_coverage"),
        "exact_overlap": metrics.get("exact_overlap_count"),
        "max_similarity": metrics.get("max_similarity"),
        "message_id": message_id,
        "run_path": str(run_dir),
    }


def main() -> dict[str, Any]:
    return run_delivery()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
