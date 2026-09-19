"""Production /make Top-1 recovery: reserve candidates, freeze on first QA pass."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from newsagent_v2.approval.store import (
    STATE_AWAITING_APPROVAL,
    ApprovalStore,
)
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.capacity import (
    analyze_evidence_capacity,
    article_input_for_ledgers,
    refine_capacity_with_plan,
)
from newsagent_v2.article.writer.controlled.editorial_compile import compile_editorial_article
from newsagent_v2.article.writer.controlled.failures import INSUFFICIENT_EVIDENCE, WRITER_PROVIDER_ERROR
from newsagent_v2.article.writer.controlled.kimi_k25_renderer import (
    FALLBACK_ENV,
    KimiK25ProseRenderer,
    should_fallback_to_kimi,
)
from newsagent_v2.article.writer.controlled.paid_qwen_renderer import PaidQwenProseRenderer, QWEN_MODEL
from newsagent_v2.article.writer.controlled.pipeline import CompileResult
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.research import research_story
from newsagent_v2.article.writer.controlled.writer_feasibility import (
    EDITORIAL_TARGET_MAX_WORDS,
    EDITORIAL_TARGET_MIN_WORDS,
    WRITER_INFEASIBLE,
    evaluate_writer_feasibility,
)
from newsagent_v2.article.writer.bedrock_mantle import KIMI_MODEL
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.qwen_vllm import default_transport
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.batch.viability import cluster_to_story
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.control.make import persist_story_artifacts, send_approval_cards
from newsagent_v2.control.make_summary import format_make_summary
from newsagent_v2.image.artifacts import DEFAULT_RUNS_ROOT
from newsagent_v2.image.benchmark.runner import run_provider_benchmark
from newsagent_v2.image.brief import build_visual_brief
from newsagent_v2.image.compositor import CompositionSpec, compose_card
from newsagent_v2.image.contract import CARD_HEIGHT, CARD_WIDTH, DEFAULT_NEGATIVE_PROMPT
from newsagent_v2.image.providers.cloudflare import (
    ACCOUNT_ENV,
    DEFAULT_MODEL as FLUX_MODEL,
    TOKEN_ENV as CF_TOKEN_ENV,
    CloudflareImageProvider,
    load_cloudflare_config,
)
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.telegram.cards import approval_caption
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

MAX_CANDIDATE_GENERATIONS = 3
LOGO_PATH = Path(__file__).resolve().parents[3] / "brand" / "coinnetwork_logo.png"
EXPECTED_LOGO_SHA256 = "09c8106c0458e5225530ac6736a56944c81fbde6d7b81ed5b6ce69586e516818"
MAKE_RUNS_ROOT = Path(__file__).resolve().parents[3] / "output" / "make_runs"

# TEMPORARY / DEMO: set NEWSAGENT_V2_WORDPRESS_PUBLISH=1 with WordPress env to publish on APPROVE.
WORDPRESS_PUBLISH_ENV = "NEWSAGENT_V2_WORDPRESS_PUBLISH"


def wordpress_publish_enabled(environ: dict[str, str]) -> tuple[bool, list[str]]:
    """Return (enabled, missing_or_blocking_names). Explicit flag required."""
    from newsagent_v2.wordpress.config import (
        BASE_ENV as WP_BASE_ENV,
        PASSWORD_ENV as WP_PASSWORD_ENV,
        USER_ENV as WP_USER_ENV,
        WordPressConfigError,
        load_wordpress_config,
    )

    flag = str(environ.get(WORDPRESS_PUBLISH_ENV) or "").strip().lower()
    if flag not in {"1", "true", "yes", "on"}:
        return False, [WORDPRESS_PUBLISH_ENV]
    missing = [
        name
        for name in (WP_BASE_ENV, WP_USER_ENV, WP_PASSWORD_ENV)
        if not str(environ.get(name) or "").strip()
    ]
    if missing:
        return False, missing
    try:
        load_wordpress_config(environ)
    except WordPressConfigError:
        return False, [WP_BASE_ENV, WP_USER_ENV, WP_PASSWORD_ENV]
    return True, []


_SECRET_KEY_FRAGMENTS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "token",
    "secret",
    "password",
)


def _sha256_payload(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(frag in lowered for frag in _SECRET_KEY_FRAGMENTS):
                out[str(key)] = "[REDACTED]"
            else:
                out[str(key)] = _redact_secrets(item)
        return out
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        # Never persist long secret-looking blobs.
        if value.lower().startswith("bearer "):
            return "Bearer [REDACTED]"
        return value
    return value


def persist_candidate_attempt(
    *,
    attempts_root: Path,
    event_id: str,
    rank: int,
    compiled: CompileResult,
    writer_provider: str,
    writer_model: str,
    primary_calls: int,
    supplemental_calls: int,
) -> Path:
    """Persist failed/successful generation diagnostics. No secrets."""
    dest = attempts_root / str(event_id)
    dest.mkdir(parents=True, exist_ok=True)
    article = compiled.article if isinstance(compiled.article, dict) else {}
    qa = compiled.qa if isinstance(compiled.qa, dict) else {}
    metrics = qa.get("metrics") if isinstance(qa.get("metrics"), dict) else {}
    body = str(article.get("article_body") or "")
    retained_words = int(metrics.get("article_word_count") or word_count(body) or 0)
    quarantine = compiled.quarantine if isinstance(compiled.quarantine, dict) else {}
    generated_words = int(quarantine.get("generated_words") or retained_words)
    critical = [
        str(item.get("code") or "")
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    ]
    warnings = [
        str(item.get("code") or "")
        for item in (qa.get("warnings") or qa.get("warning_failures") or [])
        if isinstance(item, dict) and str(item.get("code") or "").strip()
    ]
    attempt = {
        "event_id": event_id,
        "candidate_rank": rank,
        "writer_provider": writer_provider,
        "writer_model": writer_model,
        "primary_call_count": int(primary_calls),
        "supplemental_call_count": int(supplemental_calls),
        "generated_word_count": generated_words,
        "retained_word_count": retained_words,
        "quarantined_word_count": max(0, generated_words - retained_words),
        "quarantine": quarantine,
        "grounding_coverage": metrics.get("body_claim_coverage"),
        "copyright_exact_overlaps": metrics.get("exact_overlap_count"),
        "copyright_max_similarity": metrics.get("max_sentence_similarity"),
        "qa_publishable": bool(qa.get("publishable") and qa.get("qa_passed")),
        "critical_failure_codes": critical,
        "warnings": warnings,
        "failure_class": compiled.failure_class,
        "notes": compiled.notes,
        "writer_feasible": compiled.writer_feasible,
        "capacity_advisory": compiled.capacity_advisory,
        "render_attempts": compiled.render_attempts,
    }
    (dest / "attempt.json").write_text(
        json.dumps(_redact_secrets(attempt), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if article:
        (dest / "article.json").write_text(
            json.dumps(_redact_secrets(article), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (dest / "article_body.txt").write_text(body + ("\n" if body else ""), encoding="utf-8")
    if qa:
        (dest / "qa.json").write_text(
            json.dumps(_redact_secrets(qa), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if compiled.diagnostic:
        (dest / "writer_diagnostic.json").write_text(
            json.dumps(_redact_secrets(compiled.diagnostic), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return dest


def assess_make_candidate(story: dict[str, Any]) -> dict[str, Any]:
    raw = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    pack = article_input_for_ledgers(raw)
    ledgers = build_evidence_ledgers(pack)
    capacity = analyze_evidence_capacity(
        ledgers,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
    )
    plan = plan_article(
        ledgers,
        pack,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
        target_min_words=EDITORIAL_TARGET_MIN_WORDS,
        target_max_words=EDITORIAL_TARGET_MAX_WORDS,
    )
    capacity = refine_capacity_with_plan(
        capacity,
        planned_safe_words=plan.planned_safe_words,
        minimum_surviving_words=plan.minimum_surviving_words,
        paragraph_loss_tolerance=plan.paragraph_loss_tolerance,
        hard_minimum_words=NORMAL_ARTICLE_POLICY.hard_minimum_words,
    )
    feasibility = evaluate_writer_feasibility(
        article_input=raw,
        ledgers=ledgers,
        capacity=capacity,
        plan=plan,
    )
    eligible = bool(feasibility.get("writer_feasible"))
    return {
        "eligible": eligible,
        "writer_feasible": eligible,
        "feasibility": feasibility,
        "capacity": capacity,
        "plan": plan,
        "pack": pack,
        "planned_safe_words": plan.planned_safe_words,
        "planned_factual_assertions": sum(len(para.allowed_claim_ids) for para in plan.paragraph_plans),
        "capacity_class": capacity.capacity_class,
        "feasibility_status": feasibility.get("status"),
    }


def _evidence_rows(story: dict[str, Any]) -> list[dict[str, Any]]:
    raw = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), list) else []
    return [item for item in evidence if isinstance(item, dict)]


def candidate_eligible_for_research(story: dict[str, Any]) -> bool:
    if story.get("_research_applied"):
        return False
    return any(str(item.get("url") or "").startswith("http") for item in _evidence_rows(story))


def _story_from_research(result: Any, original: dict[str, Any]) -> dict[str, Any]:
    if isinstance(result, dict) and isinstance(result.get("article_input"), dict):
        return result
    if isinstance(result, dict) and isinstance(result.get("story"), dict):
        return result["story"]
    return original


def recover_publishable_article(
    stories: list[dict[str, Any]],
    *,
    compile_fn: Callable[[dict[str, Any]], CompileResult],
    assess_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    research_fn: Callable[..., Any] | None = None,
    max_generations: int = MAX_CANDIDATE_GENERATIONS,
    attempts_root: Path | None = None,
    writer_provider: str = "unknown",
    writer_model: str = "unknown",
) -> dict[str, Any]:
    """Attempt distinct eligible candidates until first qa_publishable article."""
    assess = assess_fn or assess_make_candidate
    research = research_fn or research_story
    failures: list[dict[str, Any]] = []
    generations = 0
    seen: set[str] = set()
    winner: dict[str, Any] | None = None
    for index, story in enumerate(stories, start=1):
        event_id = str(story.get("event_id") or f"rank-{index}")
        if event_id in seen:
            continue
        working = dict(story)
        if candidate_eligible_for_research(working):
            working = _story_from_research(research(working), working)
            working["_research_applied"] = True
        assessed = assess(working)
        if not assessed.get("eligible"):
            working["_writer_feasible"] = False
            failures.append(
                {
                    "event_id": event_id,
                    "rank": working.get("original_rank") or index,
                    "failure_class": WRITER_INFEASIBLE
                    if assessed.get("feasibility_status") == WRITER_INFEASIBLE
                    else INSUFFICIENT_EVIDENCE,
                    "generated": False,
                    "researched": bool(working.get("_research_applied")),
                    "planned_safe_words": assessed.get("planned_safe_words"),
                    "capacity_class": assessed.get("capacity_class"),
                    "writer_feasible": False,
                }
            )
            continue
        if generations >= max_generations:
            break
        working["_writer_feasible"] = True
        seen.add(event_id)
        generations += 1
        compiled = compile_fn(working)
        qa = compiled.qa or {}
        publishable = bool(compiled.ok and qa.get("publishable") and qa.get("qa_passed"))
        notes = compiled.notes or ""
        primary_calls = 1 if "kimi_calls=" in notes or "paid_qwen_calls=" in notes or compiled.render_attempts else int(compiled.render_attempts or 0)
        if "kimi_calls=" in notes:
            try:
                primary_calls = int(notes.split("kimi_calls=")[1].split()[0])
            except (IndexError, ValueError):
                primary_calls = int(compiled.render_attempts or 0)
        elif "paid_qwen_calls=" in notes:
            try:
                primary_calls = int(notes.split("paid_qwen_calls=")[1].split()[0])
            except (IndexError, ValueError):
                primary_calls = int(compiled.render_attempts or 0)
        supplemental_calls = 1 if "supplemental=1" in notes else 0
        if primary_calls >= 2 and supplemental_calls:
            # notes count total renders for that candidate; split for telemetry.
            primary_calls = max(1, primary_calls - supplemental_calls)
        attempt_path = None
        if attempts_root is not None:
            attempt_path = persist_candidate_attempt(
                attempts_root=attempts_root,
                event_id=str(compiled.event_id or event_id),
                rank=int(working.get("original_rank") or index),
                compiled=compiled,
                writer_provider=writer_provider,
                writer_model=writer_model,
                primary_calls=primary_calls,
                supplemental_calls=supplemental_calls,
            )
        if not publishable:
            failures.append(
                {
                    "event_id": compiled.event_id or event_id,
                    "rank": working.get("original_rank") or index,
                    "failure_class": compiled.failure_class or "qa_publishable=false",
                    "generated": True,
                    "notes": compiled.notes,
                    "critical_failure_codes": [
                        str(item.get("code") or "")
                        for item in ((compiled.qa or {}).get("critical_failures") or [])
                        if isinstance(item, dict)
                    ],
                    "attempt_path": str(attempt_path) if attempt_path else None,
                    "writer_feasible": True,
                    "capacity_advisory": compiled.capacity_advisory,
                }
            )
            continue
        winner = {
            "story": working,
            "compiled": compiled,
            "assessed": assessed,
            "rank": working.get("original_rank") or index,
            "attempt_path": str(attempt_path) if attempt_path else None,
        }
        break
    return {
        "ok": winner is not None,
        "winner": winner,
        "failures": failures,
        "candidate_attempts": generations,
        "max_candidate_generations": max_generations,
        "qa_weakened": False,
        "status": "SUCCESS" if winner is not None else "NO SAFE ARTICLE",
        "attempts_root": str(attempts_root) if attempts_root else None,
    }


def qwen_compile_fn(environ: dict[str, str], *, http_post: Any = None) -> Callable[[dict[str, Any]], CompileResult]:
    poster = default_transport if http_post is None else http_post
    fallback_env = dict(environ)
    fallback_env[FALLBACK_ENV] = "1"
    state = {"use_kimi": False, "kimi_calls": 0, "writer": f"paid_qwen/{QWEN_MODEL}", "qwen_dns_ok": None}

    def _qwen_dns_ok() -> bool:
        if state["qwen_dns_ok"] is not None:
            return bool(state["qwen_dns_ok"])
        try:
            import socket
            from urllib.parse import urlparse

            from newsagent_v2.article.writer.qwen_vllm import BASE_ENV

            base = str(environ.get(BASE_ENV) or "").strip()
            host = urlparse(base).netloc.split("@")[-1].split(":")[0]
            if not host:
                state["qwen_dns_ok"] = False
                return False
            socket.getaddrinfo(host, 443)
            state["qwen_dns_ok"] = True
            return True
        except OSError:
            state["qwen_dns_ok"] = False
            return False

    if not _qwen_dns_ok() and should_fallback_to_kimi(
        failure_class=WRITER_PROVIDER_ERROR,
        environ=fallback_env,
    ):
        state["use_kimi"] = True
        state["writer"] = f"kimi_k25/{KIMI_MODEL}"

    def compile_one(story: dict[str, Any]) -> CompileResult:
        if not state["use_kimi"]:
            renderer = PaidQwenProseRenderer(environ=environ, http_post=poster, max_calls=2)
            compiled = compile_editorial_article(story, renderer=renderer)
            compiled.notes = (compiled.notes + f" paid_qwen_calls={renderer.generation_calls}").strip()
            if compiled.ok:
                return compiled
            if compiled.failure_class == WRITER_PROVIDER_ERROR and should_fallback_to_kimi(
                failure_class=WRITER_PROVIDER_ERROR,
                environ=fallback_env,
            ):
                state["use_kimi"] = True
                state["writer"] = f"kimi_k25/{KIMI_MODEL}"
            else:
                return compiled

        kimi = KimiK25ProseRenderer(environ=fallback_env, allow_real_http=True, max_calls=2)
        compiled = compile_editorial_article(story, renderer=kimi)
        state["kimi_calls"] += int(kimi.generation_calls)
        compiled.notes = (
            compiled.notes
            + f" writer=kimi_k25 kimi_calls={kimi.generation_calls} total_kimi_calls={state['kimi_calls']}"
        ).strip()
        return compiled

    compile_one.writer_state = state  # type: ignore[attr-defined]
    return compile_one


def build_flux_make_image_fn(environ: dict[str, str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def image_fn(job: dict[str, Any]) -> dict[str, Any]:
        event_id = str(job.get("event_id") or "")
        article = job.get("article") if isinstance(job.get("article"), dict) else {}
        if not (str(environ.get(ACCOUNT_ENV) or "").strip() and str(environ.get(CF_TOKEN_ENV) or "").strip()):
            return {"success": False, "event_id": event_id, "reason": "Cloudflare credentials missing", "image_request_count": 0}
        if file_sha256(LOGO_PATH) != EXPECTED_LOGO_SHA256:
            return {"success": False, "event_id": event_id, "reason": "logo hash mismatch", "image_request_count": 0}
        headline = str(article.get("headline") or "").strip()
        dek = str(article.get("dek") or "").strip()
        names = [
            str(row.get("name") or "").strip()
            for row in (article.get("entities") or [])
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        ]
        prompt = (
            "Professional editorial-news photograph, artwork only, no writing of any kind. "
            f"Story visual: {headline}. {dek[:220]}. Photorealistic Reuters/Bloomberg 16:9, "
            "clean negative space. Do not render headlines, captions, newspapers, logos, "
            "CoinNetwork branding, or readable text."
        )
        brief = build_visual_brief(
            {
                "event_id": event_id,
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
                "provider_visual_prompt": prompt,
            }
        )
        provider = CloudflareImageProvider(
            load_cloudflare_config({ACCOUNT_ENV: environ[ACCOUNT_ENV], CF_TOKEN_ENV: environ[CF_TOKEN_ENV]}),
            model=FLUX_MODEL,
            timeout_seconds=300,
            max_retries=0,
        )
        runs = run_provider_benchmark(provider, brief, generations=1, persist_root=DEFAULT_RUNS_ROOT, compose=False)
        run = runs[0]
        result = run.get("result") or {}
        validation = run.get("validation") or {}
        if not result.get("success") or not validation.get("passed"):
            return {
                "success": False,
                "event_id": event_id,
                "reason": str(result.get("failure_reason") or validation or "raw validation failed"),
                "image_request_count": 1,
                "http_status": result.get("http_status"),
                "latency_ms": result.get("total_latency_ms"),
            }
        raw_path = Path(str(result["raw_image_path"]))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        card_dir = Path(__file__).resolve().parents[3] / "output" / "make_runs" / f"{event_id}-{stamp}"
        card_dir.mkdir(parents=True, exist_ok=True)
        card_path = card_dir / "branded.png"
        composition = compose_card(
            raw_path,
            card_path,
            CompositionSpec(
                headline="",
                category_label=None,
                logo_path=LOGO_PATH,
                test_mode=False,
                logo_only=True,
                output_name="branded.png",
            ),
        )
        if not (composition.get("compositor_validation") or {}).get("passed"):
            return {"success": False, "event_id": event_id, "reason": "compositor failed", "image_request_count": 1}
        return {
            "success": True,
            "event_id": event_id,
            "final_path": str(card_path),
            "image_request_count": 1,
            "http_status": result.get("http_status"),
            "latency_ms": result.get("total_latency_ms"),
        }

    return image_fn


def run_make_top1(
    *,
    environ: dict[str, str],
    store: ApprovalStore,
    telegram_config: TelegramConfig,
    telegram_client: TelegramTestClient,
    discover_fn: Callable[[], dict[str, Any]] | None = None,
    enrich_fn: Callable[..., dict[str, Any]] | None = None,
    compile_fn: Callable[[dict[str, Any]], CompileResult] | None = None,
    image_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    assess_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    research_fn: Callable[..., Any] | None = None,
    max_generations: int = MAX_CANDIDATE_GENERATIONS,
    stories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Shared /make Top-1 path: recover article, then Flux, then Telegram."""
    if stories is None:
        discovered = (discover_fn or discover_ranked_top5)()
        ranked = list(discovered.get("ranked_clusters") or [])
        stories = []
        for index, cluster in enumerate(ranked, start=1):
            stories.append(cluster_to_story(cluster, original_rank=index))
    compile = compile_fn or qwen_compile_fn(environ)
    writer_state = getattr(compile, "writer_state", None)
    writer_label = str((writer_state or {}).get("writer") or f"paid_qwen/{QWEN_MODEL}")
    writer_model = KIMI_MODEL if writer_label.startswith("kimi_") else QWEN_MODEL
    writer_provider = "bedrock_mantle" if writer_label.startswith("kimi_") else "paid_qwen"
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    attempts_root = MAKE_RUNS_ROOT / f"make-{run_stamp}" / "attempts"
    attempts_root.mkdir(parents=True, exist_ok=True)
    recovered = recover_publishable_article(
        stories,
        compile_fn=compile,
        assess_fn=assess_fn,
        research_fn=research_fn or enrich_fn or research_story,
        max_generations=max_generations,
        attempts_root=attempts_root,
        writer_provider=writer_provider,
        writer_model=writer_model,
    )
    wp_on, wp_missing = wordpress_publish_enabled(environ)
    telemetry = {
        "writer": writer_label,
        "kimi_calls": int((writer_state or {}).get("kimi_calls") or 0),
        "candidate_attempts": recovered["candidate_attempts"],
        "failures": recovered["failures"],
        "attempts_root": str(attempts_root),
        "image_request_count": 0,
        "approval_card_sent": False,
        "wordpress_disabled": not wp_on,
        "wordpress_missing": wp_missing,
        "make_story_count": MAKE_STORY_COUNT,
    }
    if not recovered.get("ok"):
        summary_lines = ["NO SAFE ARTICLE"]
        for row in recovered["failures"]:
            if not row.get("generated"):
                continue
            codes = ",".join(row.get("critical_failure_codes") or []) or row.get("failure_class")
            summary_lines.append(
                f"{row.get('event_id')}: {row.get('failure_class')} codes={codes} path={row.get('attempt_path')}"
            )
        summary = "\n".join(summary_lines)
        telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary[:3500])
        return {
            "ok": False,
            "status": "NO SAFE ARTICLE",
            "stories": [],
            "approval_cards": [],
            "completion_text": summary,
            "telemetry": telemetry,
            "candidate_attempts": recovered["candidate_attempts"],
            "failures": recovered["failures"],
            "batch_run_id": f"make-{run_stamp}",
            "attempts_root": str(attempts_root),
        }

    winner = recovered["winner"] or {}
    compiled: CompileResult = winner["compiled"]
    story = dict(winner["story"])
    article = deepcopy(compiled.article or {})
    qa = deepcopy(compiled.qa or {})
    story["article"] = article
    story["qa_result"] = qa
    story["qa_publishable"] = True
    image = (image_fn or build_flux_make_image_fn(environ))(story)
    telemetry["image_request_count"] = int(image.get("image_request_count") or 0)
    batch_id = f"make-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    event_id = str(story.get("event_id") or compiled.event_id)
    row = {
        "event_id": event_id,
        "story_index": 1,
        "original_rank": winner.get("rank"),
        "article": article,
        "article_input": story.get("article_input"),
        "qa_result": qa,
        "qa_publishable": True,
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "deliverable": False,
        "final_image_path": None,
        "image": image,
        "generation_failure": None,
        "evidence_sufficiency": story.get("evidence_sufficiency"),
        "writer_model": writer_model,
        "allow_wordpress": wp_on,
        "wordpress_disabled": not wp_on,
        "publish_on_approve": wp_on,
        "article_sha256": _sha256_payload(article),
    }
    if not image.get("success"):
        persist_story_artifacts(store=store, batch_id=batch_id, row=row)
        return {
            "ok": False,
            "status": "IMAGE FAILED",
            "stories": [row],
            "approval_cards": [],
            "completion_text": f"IMAGE_FAILED: {image.get('reason')}",
            "telemetry": telemetry,
            "candidate_attempts": recovered["candidate_attempts"],
            "headline": article.get("headline"),
            "words": int((qa.get("metrics") or {}).get("article_word_count") or word_count(str(article.get("article_body") or ""))),
            "qa": qa,
            "batch_id": batch_id,
            "image_failure": image.get("reason"),
        }

    row["final_image_path"] = image.get("final_path")
    row["deliverable"] = True
    persist_story_artifacts(store=store, batch_id=batch_id, row=row)
    extra = store.read_story(batch_id, event_id) or {}
    extra.update(
        {
            "no_regeneration": True,
            "allow_wordpress": wp_on,
            "publish_on_approve": wp_on,
            "wordpress_disabled": not wp_on,
            "wordpress_missing": wp_missing,
            "writer_model": writer_model,
            "frozen_bundle_path": str(Path(str(image.get("final_path"))).parent),
        }
    )
    store.write_story(batch_id, event_id, extra)
    cards = send_approval_cards(
        batch_id=batch_id,
        stories=[row],
        client=telegram_client,
        store=store,
        chat_id=telegram_config.test_chat_id,
    )
    sent_ok = bool(cards) and cards[0].get("ok") is not False
    if not sent_ok:
        return {
            "ok": False,
            "status": "TELEGRAM FAILED",
            "stories": [row],
            "approval_cards": cards,
            "completion_text": str((cards[0] if cards else {}).get("send") or "send_photo failed"),
            "telemetry": telemetry,
            "candidate_attempts": recovered["candidate_attempts"],
            "headline": article.get("headline"),
            "qa": qa,
            "batch_id": batch_id,
        }
    telemetry["approval_card_sent"] = True
    caption = approval_caption(rank=1, headline=str(article.get("headline") or ""), dek=str(article.get("dek") or ""), total=1)
    summary = format_make_summary(
        stories=[{**row, "deliverable": True}],
        telemetry=telemetry,
        viability={},
        approval_cards=cards,
    )
    telegram_client.send_message(chat_id=telegram_config.test_chat_id, text=summary)
    store.write_batch(
        batch_id,
        {
            "batch_id": batch_id,
            "selected_event_ids": [event_id],
            "awaiting": 1,
            "failed": 0,
            "telemetry": telemetry,
            "summary": summary,
            "state": STATE_AWAITING_APPROVAL,
            "wordpress_disabled": not wp_on,
            "wordpress_missing": wp_missing,
        },
    )
    message_id = cards[0].get("send", {}).get("message_id") if isinstance(cards[0].get("send"), dict) else cards[0].get("send")
    if message_id is None and isinstance(cards[0].get("send"), dict):
        message_id = cards[0]["send"].get("message_id")
    metrics = qa.get("metrics") or {}
    return {
        "ok": True,
        "status": "SUCCESS",
        "stories": [row],
        "approval_cards": cards,
        "completion_text": summary,
        "telemetry": telemetry,
        "candidate_attempts": recovered["candidate_attempts"],
        "headline": article.get("headline"),
        "words": int(metrics.get("article_word_count") or word_count(str(article.get("article_body") or ""))),
        "coverage": metrics.get("body_claim_coverage"),
        "exact_overlap": metrics.get("exact_overlap_count"),
        "qa": qa,
        "message_id": cards[0].get("send", {}).get("message_id") if isinstance(cards[0].get("send"), dict) else None,
        "state": STATE_AWAITING_APPROVAL,
        "batch_id": batch_id,
        "bundle": str(Path(str(image.get("final_path"))).parent),
        "caption": caption,
        "writer": writer_label,
        "article_sha256": row.get("article_sha256"),
        "article_hash": row.get("article_sha256"),
    }
