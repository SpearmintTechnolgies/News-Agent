"""First development vertical slice: frozen candidate → Cloudflare → logo → Telegram.

Does not run /make, writers, WordPress, or Aadi/Hermes/Anime.
Does not regenerate the article or QA.
"""

from __future__ import annotations

import json
import os
import shutil
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from newsagent_v2.approval.store import (
    STATE_AWAITING_APPROVAL,
    STATE_GENERATED,
    ApprovalStore,
)
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, SOURCE_BATCH_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.freeze import sha256_payload
from newsagent_v2.bench.writer_bakeoff.runner import verify_fixture_hashes
from newsagent_v2.benchmark.input import write_json_utf8
from newsagent_v2.image.artifacts import DEFAULT_RUNS_ROOT, utc_now
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.brief import build_visual_brief
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH, DEFAULT_NEGATIVE_PROMPT
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.cloudflare import (
    ACCOUNT_ENV,
    DEFAULT_MODEL,
    TOKEN_ENV as CF_TOKEN_ENV,
    CloudflareImageProvider,
    load_cloudflare_config,
)
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.telegram.cards import approval_caption, approval_keyboard, html_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, load_telegram_config
from newsagent_v2.telegram.contract import TELEGRAM_CAPTION_LIMIT
from newsagent_v2.telegram.formatter import WINDOWS_PATH_RE
from newsagent_v2.telegram.live_http import build_live_transport

REPO_ROOT = Path(__file__).resolve().parents[4]
CANDIDATE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID / "DEVELOPMENT_WINNER_CANDIDATE"
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
BUNDLE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID / "DEVELOPMENT_VERTICAL_SLICE"
LOGO_PATH = REPO_ROOT / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"
CLOUDFLARE_MODEL = DEFAULT_MODEL

PROVIDER_VISUAL_PROMPT = (
    "Professional editorial-news photograph, artwork only, no writing of any kind. "
    "A serious United States Senate legislative chamber interior: wooden desks, "
    "gallery lighting, American civic architecture, a procedural-vote atmosphere, "
    "without readable nameplates, banners, or bill text. Combine that civic setting "
    "with restrained digital-asset market-structure symbolism: a muted abstract "
    "network or ledger geometry, not coins. This is specifically about Senate "
    "consideration of the revised CLARITY Act crypto-regulation bill ahead of a "
    "procedural vote — a Congress-and-digital-asset-policy visual. Photorealistic "
    "Reuters/Bloomberg feature lighting, 16:9, clean negative space. Do not render "
    "headlines, captions, newspapers, fake legislation wording, logos, CoinNetwork "
    "branding, Bitcoin coins, blockchain neon, generic Capitol stock collage, or a "
    "generic finance illustration."
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_environ() -> dict[str, str]:
    load_dotenv(REPO_ROOT / ".env")
    environ = {key: str(value) for key, value in os.environ.items()}
    names = (ACCOUNT_ENV, CF_TOKEN_ENV, TOKEN_ENV, CHAT_ENV)
    if os.name != "nt":
        return environ
    try:
        import winreg
    except ImportError:
        return environ
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
        return environ
    return environ


def _env_present(environ: dict[str, str], name: str) -> bool:
    return bool(str(environ.get(name, "")).strip())


def _snapshot_candidate() -> dict[str, str]:
    names = (
        "manifest.json",
        "article.json",
        "article_body.txt",
        "qa.json",
        "score.json",
        "resolver.json",
        "evidence_mapping.json",
        "corrections.json",
        "telemetry.json",
        "normalized.json",
    )
    return {name: file_sha256(CANDIDATE / name) for name in names if (CANDIDATE / name).is_file()}


def verify_candidate() -> dict[str, Any]:
    failures: list[str] = []
    if not CANDIDATE.is_dir():
        return {"passed": False, "failures": ["candidate_missing"], "details": {}}

    required = (
        "manifest.json",
        "article.json",
        "article_body.txt",
        "qa.json",
        "score.json",
        "resolver.json",
    )
    missing = [name for name in required if not (CANDIDATE / name).is_file()]
    if missing:
        failures.append(f"missing_files:{missing}")

    manifest = _load_json(CANDIDATE / "manifest.json") if (CANDIDATE / "manifest.json").is_file() else {}
    article = _load_json(CANDIDATE / "article.json") if (CANDIDATE / "article.json").is_file() else {}
    qa = _load_json(CANDIDATE / "qa.json") if (CANDIDATE / "qa.json").is_file() else {}
    score = _load_json(CANDIDATE / "score.json") if (CANDIDATE / "score.json").is_file() else {}
    body_file = (CANDIDATE / "article_body.txt").read_text(encoding="utf-8") if (CANDIDATE / "article_body.txt").is_file() else ""
    body = str(article.get("article_body") or "")
    metrics = qa.get("metrics") or {}
    words = word_count(body)
    article_hash = sha256_payload(article) if article else None
    article_file_hash = file_sha256(CANDIDATE / "article.json") if (CANDIDATE / "article.json").is_file() else None
    qa_file_hash = file_sha256(CANDIDATE / "qa.json") if (CANDIDATE / "qa.json").is_file() else None

    if manifest.get("development_corrected_candidate") is not True:
        failures.append("development_corrected_candidate")
    if manifest.get("autonomous_writer_pass") is not False:
        failures.append("autonomous_writer_pass")
    if manifest.get("qa_publishable") is not True:
        failures.append("manifest_qa_publishable")
    if qa.get("publishable") is not True or qa.get("qa_passed") is not True:
        failures.append("qa_publishable")
    if score.get("qa_publishable") is not True:
        failures.append("score_qa_publishable")
    if score.get("structure") != "PASS":
        failures.append("structure")
    if words != 354 or metrics.get("article_word_count") != 354 or score.get("rendered_word_count") != 354:
        failures.append("word_count")
    if body_file != body:
        failures.append("article_body_mismatch")
    if metrics.get("assertive_sentence_count") != 16 or metrics.get("claim_covered_sentence_count") != 16:
        failures.append("coverage_counts")
    if metrics.get("body_claim_coverage") != 1.0 or score.get("claim_coverage") != 1.0:
        failures.append("coverage")
    if qa.get("critical_failures") or score.get("grounding_criticals"):
        failures.append("grounding_criticals")
    if score.get("quote_criticals"):
        failures.append("quote_criticals")
    if score.get("contextual_absence_criticals"):
        failures.append("contextual_criticals")
    if metrics.get("exact_overlap_count") != 0 or score.get("exact_overlap") != 0:
        failures.append("exact_phrase_overlap")
    if metrics.get("exact_overlap_ngram_hits") != 0 or score.get("ngram_hits") != 0:
        failures.append("ngram_hits")
    if article.get("event_id") != EVENT_ID or qa.get("event_id") != EVENT_ID:
        failures.append("event_id")
    if manifest.get("source_writer") != "gemini-3.6-flash":
        failures.append("source_writer")

    fixture = load_fixture(FIXTURE)
    evidence = verify_fixture_hashes(fixture)
    if not evidence.get("hashes_match"):
        failures.append("frozen_evidence_hashes")
    if evidence.get("source_batch_id") != SOURCE_BATCH_ID:
        failures.append("source_batch_id")

    logo_exists = LOGO_PATH.is_file()
    logo_sha = file_sha256(LOGO_PATH) if logo_exists else None
    if not logo_exists:
        failures.append("logo_missing")
    elif logo_sha != EXPECTED_LOGO_SHA256:
        failures.append("logo_hash")

    return {
        "passed": not failures,
        "failures": failures,
        "article_hash": article_hash,
        "article_file_sha256": article_file_hash,
        "qa_file_sha256": qa_file_hash,
        "qa_publishable": bool(qa.get("publishable")),
        "words": words,
        "structure": score.get("structure"),
        "source_writer": manifest.get("source_writer"),
        "development_corrected_candidate": manifest.get("development_corrected_candidate"),
        "autonomous_writer_pass": manifest.get("autonomous_writer_pass"),
        "frozen_evidence": evidence,
        "logo_sha256": logo_sha,
        "logo_hash_ok": logo_sha == EXPECTED_LOGO_SHA256,
        "candidate_snapshot": _snapshot_candidate(),
    }


def build_story_brief(article: dict[str, Any], fixture: dict[str, Any]) -> Any:
    article_input = fixture["article_input"]
    evidence_rows = []
    for item in article_input.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        evidence_rows.append(
            {
                "evidence_id": item.get("evidence_id") or item.get("id"),
                "source": item.get("source"),
                "url": item.get("url"),
            }
        )
    facts = [
        {
            "text": "Senate Republicans released a revised 635-page CLARITY Act proposal as a final offer ahead of a procedural vote.",
            "kind": "fact",
            "evidence_ref": "event-005-e01",
        },
        {
            "text": "The story is a US Senate legislative action on digital-asset market-structure regulation, not a market-price event.",
            "kind": "fact",
            "evidence_ref": "event-005-e01",
        },
    ]
    metaphors = [
        {
            "text": "restrained civic-chamber atmosphere plus abstract digital-ledger geometry",
            "kind": "visual_metaphor",
            "not_a_factual_claim": True,
        }
    ]
    return build_visual_brief(
        {
            "event_id": EVENT_ID,
            "editorial_subject": str(article.get("headline") or ""),
            "story_category": str(article.get("category") or "regulatory"),
            "visual_concept": (
                "US Senate chamber considering the revised CLARITY Act digital-asset "
                "regulation bill before a procedural vote"
            ),
            "primary_entities": ["US Senate", "CLARITY Act"],
            "secondary_entities": ["Cynthia Lummis", "digital assets"],
            "event_action": "revised CLARITY Act proposal ahead of a Senate procedural vote",
            "facts": facts,
            "visual_metaphors": metaphors,
            "scene": "United States Senate legislative chamber interior with restrained digital-asset regulation symbolism",
            "composition": "wide 16:9 editorial photograph, one clear civic focal scene, clean negative space",
            "mood": "serious institutional policy reporting",
            "lighting": "natural gallery and window light, professional news feature",
            "palette_guidance": "oak, marble, muted navy, restrained cool highlights — not neon",
            "symbol_guidance": "abstract network or ledger geometry only; no coins, no Bitcoin, no logos",
            "avoid": [
                "generic crypto coin image",
                "Bitcoin coins or glyphs",
                "blockchain neon",
                "random Capitol stock collage",
                "generic finance illustration",
                "headline or article text",
                "fake newspaper or legislation wording",
                "CoinNetwork or any publication logo",
            ],
            "negative_prompt": (
                DEFAULT_NEGATIVE_PROMPT
                + ", Bitcoin coins, blockchain neon, generic Capitol stock photo, "
                "generic finance illustration, fake legislation text"
            ),
            "width": CARD_WIDTH,
            "height": CARD_HEIGHT,
            "evidence": evidence_rows,
            "provider_visual_prompt": PROVIDER_VISUAL_PROMPT,
        }
    )


def _development_caption(article: dict[str, Any]) -> str:
    caption = approval_caption(
        rank=1,
        headline=str(article.get("headline") or ""),
        dek=article.get("dek"),
        total=1,
    )
    caption = f"{caption}\n\nDevelopment pipeline proof"
    if WINDOWS_PATH_RE.search(caption):
        raise RuntimeError("caption_path_leak")
    if len(html_caption(caption)) > TELEGRAM_CAPTION_LIMIT:
        raise RuntimeError("caption_too_long")
    return caption


def _persist_stop(payload: dict[str, Any]) -> dict[str, Any]:
    BUNDLE.mkdir(parents=True, exist_ok=True)
    write_json_utf8(BUNDLE / "failure.json", payload)
    write_json_utf8(BUNDLE / "report.json", payload)
    return payload


def run_slice() -> dict[str, Any]:
    if (BUNDLE / "branded.png").is_file() and (BUNDLE / "manifest.json").is_file():
        existing = _load_json(BUNDLE / "manifest.json")
        if existing.get("telegram_sent"):
            return {
                "ok": False,
                "stopped": True,
                "stage": "already_complete",
                "frozen_development_bundle_path": str(BUNDLE),
                "image_calls": 0,
                "writer_calls": 0,
                "make_invoked": False,
                "wordpress_called": False,
            }
    before = _snapshot_candidate()
    verification = verify_candidate()
    if not verification["passed"]:
        return _persist_stop(
            {
                "ok": False,
                "stopped": True,
                "stage": "candidate_verification",
                "candidate_verification": "FAIL",
                "verification": verification,
                "image_calls": 0,
                "writer_calls": 0,
                "make_invoked": False,
                "wordpress_called": False,
            }
        )

    article = _load_json(CANDIDATE / "article.json")
    qa = _load_json(CANDIDATE / "qa.json")
    score = _load_json(CANDIDATE / "score.json")
    fixture = load_fixture(FIXTURE)
    environ = _load_environ()
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    batch_id = f"dvs-{stamp}"
    resume_frozen_image = (
        (BUNDLE / "branded.png").is_file()
        and (BUNDLE / "raw.png").is_file()
        and (BUNDLE / "hashes.json").is_file()
        and (BUNDLE / "composition.json").is_file()
        and (BUNDLE / "raw_validation.json").is_file()
        and (BUNDLE / "image_telemetry.json").is_file()
    )
    if resume_frozen_image:
        existing_manifest = _load_json(BUNDLE / "manifest.json") if (BUNDLE / "manifest.json").is_file() else {}
        if existing_manifest.get("batch_id"):
            batch_id = str(existing_manifest["batch_id"])
        hashes = _load_json(BUNDLE / "hashes.json")
        composition = _load_json(BUNDLE / "composition.json")
        validation = _load_json(BUNDLE / "raw_validation.json")
        telemetry = _load_json(BUNDLE / "image_telemetry.json")
        result = {
            "provider_name": existing_manifest.get("provider") or "cloudflare_workers_ai",
            "model_name": existing_manifest.get("model") or CLOUDFLARE_MODEL,
            "success": True,
            "total_latency_ms": telemetry.get("total_latency_ms") or telemetry.get("request_latency_ms"),
            "generation_time_ms": telemetry.get("generation_time_ms"),
            "request_latency_ms": telemetry.get("request_latency_ms"),
            "provider_reported_cost_usd": telemetry.get("provider_reported_cost_usd"),
            "provider_reported_cost": telemetry.get("provider_reported_cost"),
            "estimated_list_price_usd": telemetry.get("estimated_list_price_usd"),
            "raw_image_path": str(BUNDLE / "raw.png"),
        }
        bundle_raw = BUNDLE / "raw.png"
        bundle_branded = BUNDLE / "branded.png"
        raw_path = Path(str(composition.get("raw_path") or bundle_raw))
        image_run_dir = raw_path.parent.parent if raw_path.parent.name == "raw" else BUNDLE
        image_calls = 0
        image_calls_slice_total = int(existing_manifest.get("image_calls") or 1)
    elif not (_env_present(environ, ACCOUNT_ENV) and _env_present(environ, CF_TOKEN_ENV)):
        return _persist_stop(
            {
                "ok": False,
                "stopped": True,
                "stage": "cloudflare_credentials",
                "candidate_verification": "PASS",
                "article_hash": verification["article_hash"],
                "qa_publishable": True,
                "image_calls": 0,
                "writer_calls": 0,
                "make_invoked": False,
                "wordpress_called": False,
                "cloudflare_credentials_present": False,
            }
        )
    else:
        image_calls_slice_total = 1
        brief = build_story_brief(article, fixture)
        cf_config = load_cloudflare_config(
            {
                ACCOUNT_ENV: environ[ACCOUNT_ENV],
                CF_TOKEN_ENV: environ[CF_TOKEN_ENV],
            }
        )
        provider = CloudflareImageProvider(
            cf_config,
            model=CLOUDFLARE_MODEL,
            timeout_seconds=180,
            max_retries=0,
        )
        request = canonical_provider_request(brief)
        runs = run_provider_benchmark(
            provider,
            brief,
            generations=1,
            persist_root=DEFAULT_RUNS_ROOT,
            compose=False,
            request=request,
        )
        run = runs[0]
        result = run.get("result") or {}
        validation = run.get("validation") or {}
        telemetry = run.get("telemetry") or {}
        image_calls = 1
        if not result.get("success") or not validation.get("passed"):
            payload = {
                "ok": False,
                "stopped": True,
                "stage": "cloudflare_image",
                "candidate_verification": "PASS",
                "article_hash": verification["article_hash"],
                "qa_publishable": True,
                "image_provider": result.get("provider_name") or "cloudflare_workers_ai",
                "image_model": result.get("model_name") or CLOUDFLARE_MODEL,
                "image_calls": image_calls,
                "image_latency_ms": result.get("total_latency_ms") or result.get("generation_time_ms"),
                "image_provider_cost": result.get("provider_reported_cost_usd")
                or result.get("provider_reported_cost"),
                "raw_image_validation": validation,
                "failure_reason": result.get("failure_reason"),
                "http_status": result.get("http_status"),
                "run_dir": run.get("run_dir"),
                "writer_calls": 0,
                "make_invoked": False,
                "wordpress_called": False,
            }
            return _persist_stop(payload)

        raw_path = Path(str(result["raw_image_path"]))
        raw_before = file_sha256(raw_path)
        spec = CompositionSpec(
            headline="",
            category_label=None,
            logo_path=LOGO_PATH,
            test_mode=False,
            logo_only=True,
            output_name="card.png",
        )
        image_run_dir = Path(str(run["run_dir"]))
        final_path = image_run_dir / "final" / spec.output_name
        composition = compose_card(raw_path, final_path, spec)
        if file_sha256(raw_path) != raw_before:
            return _persist_stop(
                {
                    "ok": False,
                    "stopped": True,
                    "stage": "raw_mutated",
                    "image_calls": image_calls,
                    "writer_calls": 0,
                }
            )
        if not (composition.get("compositor_validation") or {}).get("passed"):
            return _persist_stop(
                {
                    "ok": False,
                    "stopped": True,
                    "stage": "compositor",
                    "image_calls": image_calls,
                    "composition": composition,
                    "writer_calls": 0,
                }
            )

        BUNDLE.mkdir(parents=True, exist_ok=True)
        bundle_raw = BUNDLE / "raw.png"
        bundle_branded = BUNDLE / "branded.png"
        shutil.copy2(raw_path, bundle_raw)
        shutil.copy2(final_path, bundle_branded)
        shutil.copy2(CANDIDATE / "article.json", BUNDLE / "article.json")
        shutil.copy2(CANDIDATE / "qa.json", BUNDLE / "qa.json")
        shutil.copy2(CANDIDATE / "score.json", BUNDLE / "score.json")
        shutil.copy2(CANDIDATE / "resolver.json", BUNDLE / "resolver.json")
        write_json_utf8(BUNDLE / "brief.json", brief.as_dict())
        write_json_utf8(BUNDLE / "image_telemetry.json", deepcopy(telemetry))
        write_json_utf8(BUNDLE / "composition.json", deepcopy(composition))
        write_json_utf8(BUNDLE / "raw_validation.json", deepcopy(validation))

        hashes = {
            "article_sha256": verification["article_hash"],
            "article_file_sha256": verification["article_file_sha256"],
            "qa_file_sha256": verification["qa_file_sha256"],
            "raw_sha256": file_sha256(bundle_raw),
            "branded_sha256": file_sha256(bundle_branded),
            "logo_sha256": verification["logo_sha256"],
            "frozen_evidence": verification["frozen_evidence"],
        }
        write_json_utf8(BUNDLE / "hashes.json", hashes)

    caption = _development_caption(article)
    store = ApprovalStore()
    story_payload = {
        "event_id": EVENT_ID,
        "batch_id": batch_id,
        "rank": 1,
        "state": STATE_GENERATED,
        "article": deepcopy(article),
        "article_sha256": verification["article_hash"],
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "slug": article.get("slug"),
        "seo_title": article.get("seo_title"),
        "meta_description": article.get("meta_description"),
        "qa_result": deepcopy(qa),
        "qa_publishable": True,
        "final_image_path": str(bundle_branded),
        "raw_image_path": str(bundle_raw),
        "image_sha256": hashes["branded_sha256"],
        "raw_sha256": hashes["raw_sha256"],
        "logo_sha256": hashes["logo_sha256"],
        "frozen_bundle_path": str(BUNDLE),
        "frozen_article_path": str(BUNDLE / "article.json"),
        "no_regeneration": True,
        "no_rediscovery": True,
        "no_writer_call": True,
        "no_image_call": True,
        "wordpress_disabled": True,
        "allow_wordpress": False,
        "publish_on_approve": False,
        "development_vertical_slice": True,
        "development_corrected_candidate": True,
        "autonomous_writer_pass": False,
        "source_writer": "gemini-3.6-flash",
        "caption": caption,
        "wp_url": None,
    }
    store.write_story(batch_id, EVENT_ID, story_payload)
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "development_vertical_slice": True,
            "development_corrected_candidate": True,
            "autonomous_writer_pass": False,
            "selected_event_ids": [EVENT_ID],
            "frozen_bundle_path": str(BUNDLE),
            "wordpress_disabled": True,
            "make_invoked": False,
        },
    )

    telegram_result: dict[str, Any] | None = None
    telegram_ok = False
    message_id = None
    buttons_registered = False
    group_confirmation = None
    if not (_env_present(environ, TOKEN_ENV) and _env_present(environ, CHAT_ENV)):
        payload = {
            "ok": False,
            "stopped": True,
            "stage": "telegram_credentials",
            "candidate_verification": "PASS",
            "image_calls": image_calls,
            "frozen_bundle_path": str(BUNDLE),
            "writer_calls": 0,
            "make_invoked": False,
            "wordpress_called": False,
        }
        write_json_utf8(BUNDLE / "manifest.json", {**payload, **hashes})
        return _persist_stop(payload)

    tg_config = load_telegram_config({TOKEN_ENV: environ[TOKEN_ENV], CHAT_ENV: environ[CHAT_ENV]})
    client = TelegramTestClient(
        tg_config,
        transport=build_live_transport(tg_config),
        live_send_enabled=True,
        timeout_seconds=90,
    )
    keyboard = approval_keyboard(batch_id, EVENT_ID)
    telegram_result = client.send_photo(
        chat_id=tg_config.test_chat_id,
        photo_name="branded.png",
        photo_bytes=bundle_branded.read_bytes(),
        caption=html_caption(caption),
        reply_markup=keyboard,
    )
    telegram_ok = bool(telegram_result.get("ok"))
    message_id = telegram_result.get("message_id")
    buttons_registered = bool(keyboard.get("inline_keyboard")) and telegram_ok
    chat = ((telegram_result.get("payload") or {}).get("result") or {}).get("chat") or {}
    group_confirmation = {
        "configured_env": CHAT_ENV,
        "chat_id_printed": False,
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title"),
        "target": "configured Newsagent group",
    }
    if telegram_ok:
        store.cas_story_state(
            batch_id,
            EVENT_ID,
            expected=STATE_GENERATED,
            new_state=STATE_AWAITING_APPROVAL,
            extra={
                "telegram_message_id": message_id,
                "telegram_send_ok": True,
                "caption": caption,
            },
        )

    after = _snapshot_candidate()
    candidate_unchanged = before == after
    story_after = store.read_story(batch_id, EVENT_ID) or {}

    report = {
        "ok": telegram_ok,
        "stopped_after_telegram": telegram_ok,
        "approve_clicked": False,
        "candidate_verification": "PASS",
        "article_hash": verification["article_hash"],
        "article_hash_verification": True,
        "qa_publishable": True,
        "image_provider": result.get("provider_name"),
        "image_model": result.get("model_name") or CLOUDFLARE_MODEL,
        "image_calls": image_calls_slice_total,
        "image_calls_this_run": image_calls,
        "image_latency_ms": result.get("total_latency_ms") or result.get("generation_time_ms"),
        "generation_time_ms": result.get("generation_time_ms"),
        "request_latency_ms": result.get("request_latency_ms"),
        "image_provider_cost": result.get("provider_reported_cost_usd")
        or result.get("provider_reported_cost"),
        "provider_reported_cost_usd": result.get("provider_reported_cost_usd"),
        "estimated_list_price_usd": result.get("estimated_list_price_usd"),
        "raw_image_validation": "PASS" if validation.get("passed") else "FAIL",
        "raw_dimensions": f"{validation.get('width')}x{validation.get('height')}",
        "raw_image_path": str(bundle_raw),
        "raw_image_run_path": str(raw_path),
        "coinnetwork_logo_hash_verification": verification["logo_hash_ok"],
        "logo_sha256": verification["logo_sha256"],
        "compositor_result": "PASS" if (composition.get("compositor_validation") or {}).get("passed") else "FAIL",
        "logo_only": True,
        "headline_overlay": False,
        "category_overlay": False,
        "final_branded_image_dimensions": f"{composition.get('width')}x{composition.get('height')}",
        "final_branded_image_path": str(bundle_branded),
        "frozen_development_bundle_path": str(BUNDLE),
        "telegram_send_result": "SUCCESS" if telegram_ok else "FAIL",
        "telegram_group_confirmation": group_confirmation,
        "approval_card_message_id": message_id,
        "buttons_registered": "YES" if buttons_registered else "NO",
        "approve_references_frozen_bundle": "YES"
        if story_after.get("frozen_bundle_path") == str(BUNDLE)
        and story_after.get("final_image_path") == str(bundle_branded)
        and story_after.get("article_sha256") == verification["article_hash"]
        and story_after.get("no_regeneration") is True
        else "NO",
        "development_vertical_slice": True,
        "development_corrected_candidate": True,
        "autonomous_writer_pass": False,
        "writer_calls": 0,
        "make_invoked": False,
        "wordpress_called": False,
        "qa_unchanged": candidate_unchanged and after.get("qa.json") == before.get("qa.json"),
        "article_unchanged": candidate_unchanged and after.get("article.json") == before.get("article.json"),
        "frozen_evidence_unchanged": verification["frozen_evidence"].get("hashes_match"),
        "aadi_hermes_anime_untouched": True,
        "batch_id": batch_id,
        "approval_state": story_after.get("state"),
        "image_run_dir": str(image_run_dir),
        "telegram_error": None if telegram_ok else (telegram_result or {}).get("error"),
        "telegram_http_status": None if telegram_result is None else telegram_result.get("status_code"),
    }
    provenance = {
        "development_vertical_slice": True,
        "development_corrected_candidate": True,
        "autonomous_writer_pass": False,
        "source_writer": "gemini-3.6-flash",
        "source_provider": "google_gemini",
        "qa_publishable": True,
        "image_generated": True,
        "telegram_sent": telegram_ok,
        "wordpress_called": False,
        "make_invoked": False,
        "writer_calls": 0,
        "image_calls": image_calls_slice_total,
        "model": CLOUDFLARE_MODEL,
        "batch_id": batch_id,
        "approval_message_id": message_id,
        "frozen_bundle_path": str(BUNDLE),
        "candidate_path": str(CANDIDATE),
        "article_hash": verification["article_hash"],
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json_utf8(BUNDLE / "manifest.json", provenance)
    write_json_utf8(BUNDLE / "telegram.json", {
        "ok": telegram_ok,
        "message_id": message_id,
        "buttons_registered": buttons_registered,
        "group_confirmation": group_confirmation,
        "caption_preview": caption,
        "batch_id": batch_id,
        "http_status": None if telegram_result is None else telegram_result.get("status_code"),
        "error": None if telegram_ok else (telegram_result or {}).get("telegram_description"),
    })
    write_json_utf8(BUNDLE / "report.json", report)
    write_json_utf8(BUNDLE / "provenance.json", provenance)
    return report


def main() -> dict[str, Any]:
    return run_slice()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
