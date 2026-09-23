from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article_readiness import preflight_article_readiness
from newsagent_v2.cluster import EventCluster
from newsagent_v2.control import live as live_module
from newsagent_v2.models import NewsItem
from newsagent_v2.rank import rank_clusters
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.listener import handle_update


class _OfflineWriter:
    generation_calls = 0
    transport = object()

    def __init__(self, *args, **kwargs):
        self.generation_calls = 0


def _cluster(event_id: str, *, ready: bool) -> EventCluster:
    summary = "Brief update." if not ready else (
        "Northwind Payments said in 2026 that regulators confirmed the event after "
        "12,400 records were exposed. The company described affected systems, "
        "response measures, customer notification, investigation status, remediation "
        "plans, follow-up reporting, public accountability updates, and independent "
        "review findings for the public. Investigators also recorded confirmed chronology, "
        "official statements, named figures, prior confirmed context, market implications, "
        "credible reactions, and documented next steps supporting a 600-800 word article."
    )
    members = [
        NewsItem(
            source="Official",
            title=f"Northwind Payments event {event_id}",
            url=f"https://official.example/{event_id}",
            published="Mon, 14 Sep 2026 12:00:00 +0000",
            summary=summary,
            source_role="primary_evidence",
            source_authority=0.9,
        )
    ]
    if ready:
        members.append(
            NewsItem(
                source="Wire",
                title=f"Northwind Payments event {event_id}",
                url=f"https://wire.example/{event_id}",
                published="Mon, 14 Sep 2026 12:00:00 +0000",
                summary=summary,
                source_role="newsroom",
                source_authority=0.7,
            )
        )
    return EventCluster(event_id=event_id, representative=members[0], members=members)


class MakeTop5ReadinessE2ETests(unittest.TestCase):
    def test_real_make_route_backfills_only_readiness_pass_stories(self) -> None:
        candidates = [_cluster("event-unready", ready=False)] + [
            _cluster(f"event-{index:03d}", ready=True) for index in range(1, 7)
        ]
        ready, readiness = preflight_article_readiness(candidates)
        ranked = rank_clusters(ready)
        self.assertNotIn("event-unready", [cluster.event_id for cluster in ranked[:5]])

        def discover() -> dict:
            return {
                "ranked_clusters": ranked,
                "article_readiness": readiness,
                "collected": len(candidates),
                "rejected": [],
            }

        calls = {"network": 0, "telegram_mock": 0, "kimi": 0, "wp": 0}

        def transport(*args, **kwargs):
            calls["telegram_mock"] += 1
            return SimpleNamespace(
                status_code=200,
                headers={},
                json=lambda: {"ok": True, "result": {"message_id": calls["telegram_mock"]}},
            )

        config = TelegramConfig("123456789:offline-test", "12345")
        client = TelegramTestClient(config, transport=transport, live_send_enabled=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ApprovalStore(Path(temp_dir))
            persisted_stories = []
            image_path = Path(temp_dir) / "image.png"
            image_path.write_bytes(b"offline-image")

            def approval_mock(*, batch_id, stories, store, **kwargs):
                cards = []
                for story in stories:
                    store.cas_story_state(
                        batch_id,
                        story["event_id"],
                        expected="GENERATED",
                        new_state="AWAITING_APPROVAL",
                        extra={"telegram_message_id": 1},
                    )
                    cards.append({"event_id": story["event_id"], "ok": True})
                return cards

            def invoke_final(**kwargs):
                from newsagent_v2.article.writer.v4 import final_pipeline

                try:
                    return final_pipeline.run_v4_final_pipeline(
                        **kwargs,
                        discover_fn=discover,
                        image_fn=lambda job: {
                            "success": True,
                            "event_id": job["event_id"],
                            "final_path": str(image_path),
                            "image_request_count": 0,
                        },
                    )
                except Exception as exc:
                    calls["error"] = f"{type(exc).__name__}: {exc}"
                    raise

            def no_kimi(*args, **kwargs):
                calls["kimi"] += 1
                raise AssertionError("unexpected Kimi call")

            with patch.object(live_module, "run_v4_final_pipeline", side_effect=invoke_final), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.assert_v4_writer_is_free"), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.build_v4_writer", return_value=_OfflineWriter()), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.Capability500V2Writer", _OfflineWriter), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.research_event", side_effect=lambda story, **kwargs: SimpleNamespace(pack=story["article_input"])), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.build_fact_bank", return_value=SimpleNamespace(propositions=["fact"])), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.assess_evidence_capacity", return_value=SimpleNamespace()), \
                patch("newsagent_v2.article.writer.v4.final_pipeline._validation_depth", return_value=SimpleNamespace(evidence_capacity="standard", recommended_word_min=1, recommended_word_max=10000)), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.compile_v4_article", return_value=SimpleNamespace(article_input=None)), \
                patch("newsagent_v2.article.writer.v4.final_pipeline._result_from_compile", return_value={
                    "ok": True,
                    "article": {"headline": "Offline headline", "article_body": "Offline body."},
                    "headline": "Offline headline",
                    "article_type": "STANDARD_ARTICLE",
                    "evidence_capacity": "standard",
                    "final_body_words": 2,
                    "canonical_body_hash": "offline-hash",
                    "qa": {"critical_count": 0},
                    "grounding_supported": 1,
                    "grounding_ambiguous": 0,
                    "grounding_unsupported": 0,
                }), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.send_approval_cards", side_effect=approval_mock), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.report_batch_from_attempts_root", return_value={"kimi_calls": 0}), \
                patch("newsagent_v2.article.writer.v4.final_pipeline.build_vertex_make_image_fn", side_effect=no_kimi):
                pipeline = live_module.build_live_pipeline(
                    environ={
                        "NEWSAGENT_V2_VERTEX_ENABLED": "true",
                        "NEWSAGENT_V2_WORDPRESS_PUBLISH": "0",
                    },
                    store=store,
                    telegram_config=config,
                    telegram_client=client,
                )
                result = handle_update(
                    {"message": {"chat": {"id": "12345"}, "text": "/make"}},
                    config=config,
                    client=client,
                    store=store,
                    pipeline=pipeline,
                )
                persisted_stories = [
                    store.read_story(result["batch_id"], event_id)
                    for event_id in result["selected_event_ids"]
                ]

        self.assertEqual(result["FINAL"], "PASS", result)
        self.assertEqual(len(result["stories"]), 5)
        self.assertNotIn("event-unready", result["selected_event_ids"])
        self.assertTrue(all(row["article_input"].get("article_readiness", {}).get("eligible") for row in result["stories"]))
        self.assertEqual(len(persisted_stories), 5)
        self.assertTrue(all(
            (row or {}).get("article_readiness", {}).get("eligible")
            for row in persisted_stories
        ))
        self.assertEqual(calls["network"], 0)
        self.assertEqual(calls["kimi"], 0)
        self.assertEqual(calls["wp"], 0)
        self.assertGreater(calls["telegram_mock"], 0)


if __name__ == "__main__":
    unittest.main()