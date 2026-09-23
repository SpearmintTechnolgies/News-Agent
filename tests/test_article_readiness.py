from __future__ import annotations

import unittest

from newsagent_v2.article_readiness import assess_article_readiness, preflight_article_readiness
from newsagent_v2.cluster import EventCluster
from newsagent_v2.models import NewsItem
from newsagent_v2.rank import rank_clusters


def _item(event_id: str, source: str, *, thin: bool = False) -> NewsItem:
    if thin:
        summary = "Brief update."
    else:
        summary = (
            "Northwind Payments said in 2026 that regulators confirmed the event after "
            "12,400 records were exposed. The company described the timeline, affected "
            "systems, response measures, customer notification process, investigation "
            "status, remediation plan, expected follow-up reporting schedule, and "
            "public accountability updates. Investigators also recorded confirmed chronology, official statements, named figures, prior confirmed context, market implications, credible reactions, and documented next steps for a publishable grounded article."
        )
    return NewsItem(
        source=source,
        title=f"Northwind Payments event {event_id}",
        url=f"https://{source.lower()}.example/{event_id}",
        published="Mon, 14 Sep 2026 12:00:00 +0000",
        summary=summary,
        source_role="primary_evidence" if source == "Official" else "newsroom",
        source_authority=0.9 if source == "Official" else 0.7,
    )


def _cluster(event_id: str, *, thin: bool = False, score: float = 1.0) -> EventCluster:
    members = [_item(event_id, "Official", thin=thin)]
    if not thin:
        members.append(_item(event_id, "Wire", thin=False))
    cluster = EventCluster(event_id=event_id, representative=members[0], members=members)
    cluster.event_score = score
    return cluster


class ArticleReadinessTests(unittest.TestCase):
    def test_evidence_poor_high_ranking_story_is_rejected(self) -> None:
        poor = _cluster("event-poor", thin=True, score=999.0)
        candidates = [poor] + [_cluster(f"event-{index:03d}", score=100.0 - index) for index in range(1, 7)]

        ready, audit = preflight_article_readiness(candidates)
        ranked = rank_clusters(ready)

        self.assertNotIn(poor, ready)
        self.assertFalse(audit["event-poor"]["eligible"])
        self.assertIn("evidence_quantity_below_minimum", audit["event-poor"]["reasons"])
        self.assertIn("source_diversity_below_minimum", audit["event-poor"]["reasons"])
        self.assertEqual([cluster.event_id for cluster in ranked[:5]], [
            "event-001", "event-002", "event-003", "event-004", "event-005",
        ])
        self.assertNotIn("event-poor", [cluster.event_id for cluster in ranked[:5]])
        self.assertIn("event-006", [cluster.event_id for cluster in ranked])

    def test_readiness_is_deterministic_and_no_fetch_is_needed(self) -> None:
        result = assess_article_readiness(_cluster("event-ready"))
        self.assertTrue(result.eligible)
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.metrics["distinct_source_count"], 2)
        self.assertGreaterEqual(result.metrics["evidence_word_count"], 60)


if __name__ == "__main__":
    unittest.main()