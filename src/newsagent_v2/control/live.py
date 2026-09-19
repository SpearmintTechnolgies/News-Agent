"""Wire the reviewed /make listener to the V4 final Top-5 pipeline."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from time import perf_counter
from uuid import uuid4

from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.writer.v4.final_pipeline import run_v4_final_pipeline
from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.batch.contract import BatchError
from newsagent_v2.image.brief import VisualBrief
from newsagent_v2.image.cloudflare_reference import derive_cloudflare_reference
from newsagent_v2.image.compositor import CompositionSpec, compose_card, discover_approved_logo
from newsagent_v2.image.hero import HeroImageError, acquire_hero_image
from newsagent_v2.image.provider import canonical_provider_request
from newsagent_v2.image.providers.cloudflare import CloudflareImageProvider, load_cloudflare_config
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

REPO_ROOT = Path(__file__).resolve().parents[3]


def wordpress_disabled_publish(**_kwargs: Any) -> dict[str, Any]:
    """Record-only publisher for the first live test. Does not create a WordPress post."""
    return {
        "ok": True,
        "held": True,
        "wordpress_disabled": True,
        "url": None,
        "post_id": None,
    }


def resolve_wordpress_publish_fn(environ: dict[str, str]) -> Callable[..., dict[str, Any]]:
    """Use live WordPress when explicitly enabled + configured; otherwise hold on APPROVE."""
    from newsagent_v2.control.make_recovery import wordpress_publish_enabled
    from newsagent_v2.wordpress.adapter import build_live_wordpress_transport, publish_frozen_story
    from newsagent_v2.wordpress.config import load_wordpress_config

    enabled, _missing = wordpress_publish_enabled(environ)
    if not enabled:
        return wordpress_disabled_publish
    config = load_wordpress_config(environ)
    transport = build_live_wordpress_transport()

    def _publish(*, article: dict[str, Any], image_path: str | None = None, **_kwargs: Any) -> dict[str, Any]:
        return publish_frozen_story(
            config=config,
            article=article,
            image_path=image_path,
            transport=transport,
        )

    return _publish


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _evidence_url(job: dict[str, Any]) -> str:
    primary = job.get("primary_url")
    if isinstance(primary, str) and primary.strip():
        return primary.strip()
    evidence = (job.get("article_input") or {}).get("evidence") or []
    if isinstance(evidence, list) and evidence and isinstance(evidence[0], dict):
        url = evidence[0].get("url")
        if isinstance(url, str):
            return url.strip()
    return ""


def _pack_stories(ranked_clusters: list[Any], evidence_pack: dict[str, Any]) -> list[dict[str, Any]]:
    """Pack evidence-pack rows for tests; live /make uses select_viable_stories."""
    by_id = {cluster.event_id: cluster for cluster in ranked_clusters}
    stories: list[dict[str, Any]] = []
    for row in evidence_pack.get("stories") or []:
        event_id = str(row["event_id"])
        cluster = by_id[event_id]
        candidate = {
            "event_id": event_id,
            "representative_title": cluster.representative.title,
            "deterministic_rank": None,
            "event_score": cluster.event_score,
            "sources": cluster.sources,
            "source_count": cluster.source_count,
            "evidence": [item.to_dict() for item in cluster.members],
        }
        stories.append(
            {
                "event_id": event_id,
                "article_input": build_article_input(candidate),
                "article_url": cluster.representative.url,
                "source_count": cluster.source_count,
            }
        )
    return stories


def build_live_image_fn(environ: dict[str, str]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    cf_config = load_cloudflare_config(environ)
    provider = CloudflareImageProvider(cf_config, max_retries=0)
    logo = discover_approved_logo(REPO_ROOT)
    cold = {"first": True}

    def image_fn(job: dict[str, Any]) -> dict[str, Any]:
        event_id = str(job["event_id"])
        article = job.get("article") if isinstance(job.get("article"), dict) else {}
        headline = str((article or {}).get("headline") or event_id)
        dek = str((article or {}).get("dek") or "")
        run_dir = REPO_ROOT / "output" / "image_runs" / f"live-{event_id}-{_utc_stamp()}-{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        article_url = _evidence_url(job)
        try:
            hero = acquire_hero_image(
                article_url,
                run_dir / "hero-source",
                event_id=event_id,
            )
        except (HeroImageError, Exception) as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            return {
                "success": False,
                "event_id": event_id,
                "reason": f"hero_failed:{code}",
                "image_request_count": 0,
            }
        master = Path(str(hero["local_path"]))
        ref_path = run_dir / "cloudflare-reference.jpg"
        try:
            derive_cloudflare_reference(master, ref_path)
        except Exception as exc:
            code = getattr(exc, "code", None) or exc.__class__.__name__
            return {
                "success": False,
                "event_id": event_id,
                "reason": f"reference_failed:{code}",
                "image_request_count": 0,
            }
        facts = []
        subject = dek or headline
        if subject:
            facts.append({"text": subject[:240], "kind": "fact"})
        brief = VisualBrief(
            event_id=event_id,
            editorial_subject=headline,
            visual_concept=(
                "Original premium financial-news editorial artwork. "
                "New composition. No readable text, logos, watermarks, or fake UI."
            ),
            facts=facts,
        )
        request = canonical_provider_request(
            brief,
            reference_image_path=str(ref_path),
            reference_image_role="story_reference",
            reference_required=True,
        )
        dest = run_dir / "artwork.png"
        generated = provider.generate(request, dest_path=str(dest), cold_start=bool(cold["first"]))
        cold["first"] = False
        if not generated.success or not generated.raw_image_path:
            return {
                "success": False,
                "event_id": event_id,
                "reason": generated.failure_reason or "image_failed",
                "image_request_count": 1,
                "latency_ms": generated.total_latency_ms,
                "http_status": generated.http_status,
            }
        card_path = run_dir / "final" / "card.png"
        compose_card(
            Path(generated.raw_image_path),
            card_path,
            CompositionSpec(
                headline="",
                logo_path=logo,
                logo_only=True,
                test_mode=False,
            ),
        )
        return {
            "success": True,
            "event_id": event_id,
            "final_path": str(card_path),
            "reference_path": str(ref_path),
            "reference_sha256": file_sha256(ref_path),
            "latency_ms": generated.total_latency_ms,
            "image_request_count": 1,
            "http_status": generated.http_status,
        }

    return image_fn


def build_live_pipeline(
    *,
    environ: dict[str, str],
    store: ApprovalStore,
    telegram_config: TelegramConfig,
    telegram_client: TelegramTestClient,
) -> Callable[[], dict[str, Any]]:
    """Wire /make → V4 Top-5 final pipeline (Kimi articles + guarded Vertex images)."""

    def pipeline() -> dict[str, Any]:
        pipeline_started = perf_counter()
        env = dict(environ)
        # Controlled integration: WordPress stays disabled.
        env["NEWSAGENT_V2_WORDPRESS_PUBLISH"] = "0"
        if "NEWSAGENT_V2_VERTEX_ENABLED" not in env:
            env["NEWSAGENT_V2_VERTEX_ENABLED"] = "true"
        try:
            result = run_v4_final_pipeline(
                environ=env,
                store=store,
                telegram_config=telegram_config,
                telegram_client=telegram_client,
            )
            result["elapsed_pipeline_ms"] = int(round((perf_counter() - pipeline_started) * 1000))
            result.setdefault("batch_run_id", result.get("batch_id"))
            return result
        except BatchError:
            summary = (
                "⚠️ NewsAgent Top-5 — PIPELINE FAILED\n"
                "Generation: not completed\n"
                "Images: not reached"
            )
            telegram_client.send_message(
                chat_id=telegram_config.test_chat_id,
                text=summary,
            )
            return {
                "ok": False,
                "FINAL": "FAIL",
                "status": "FAIL",
                "stories": [],
                "selected_event_ids": [],
                "approval_cards": [],
                "completion_text": summary,
                "batch_run_id": "failed",
                "wordpress_publish_calls": 0,
                "elapsed_pipeline_ms": int(round((perf_counter() - pipeline_started) * 1000)),
            }
        except Exception as exc:
            summary = (
                "⚠️ NewsAgent Top-5 — PIPELINE FAILED\n"
                "Generation: not completed\n"
                f"Error: {type(exc).__name__}"
            )
            telegram_client.send_message(
                chat_id=telegram_config.test_chat_id,
                text=summary,
            )
            return {
                "ok": False,
                "FINAL": "FAIL",
                "status": "FAIL",
                "stories": [],
                "selected_event_ids": [],
                "approval_cards": [],
                "completion_text": summary,
                "batch_run_id": "failed",
                "wordpress_publish_calls": 0,
                "elapsed_pipeline_ms": int(round((perf_counter() - pipeline_started) * 1000)),
            }

    return pipeline
