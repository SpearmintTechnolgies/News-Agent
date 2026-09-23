from __future__ import annotations

import argparse
import json
from pathlib import Path

from .collect import load_sources, collect_rss
from .dedupe import dedupe
from .rank import rank_clusters, dimension_breakdown
from .evidence import build_evidence_pack
from .quality import validate_evidence_pack
from .metrics import RunMetrics, Timer
from .providers.mock import MockEditorialProvider
from .publishers.dryrun import publish_dry_run
from .diagnostics import filter_candidates, build_diagnostics
from .cluster import cluster_events
from .article_readiness import preflight_article_readiness

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "sources.json"
OUTPUT = ROOT / "output"


def run() -> int:
    metrics = RunMetrics()
    OUTPUT.mkdir(parents=True, exist_ok=True)

    with Timer() as timer:
        sources = load_sources(CONFIG)

        items, source_health = collect_rss(sources)
        metrics.collected = len(items)

        # Persist every normalized candidate before filtering/ranking.
        (OUTPUT / "candidates.json").write_text(
            json.dumps(
                [item.to_dict() for item in items],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        filtered, rejected = filter_candidates(items)

        diagnostics = build_diagnostics(
            items,
            source_health,
            rejected,
        )

        (OUTPUT / "discovery_diagnostics.json").write_text(
            json.dumps(
                diagnostics,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        unique = dedupe(filtered)
        metrics.deduped = len(unique)

        clusters = cluster_events(unique)
        ready_clusters, article_readiness = preflight_article_readiness(clusters)
        ranked_clusters = rank_clusters(ready_clusters)

        (OUTPUT / "article_readiness.json").write_text(
            json.dumps(article_readiness, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        (OUTPUT / "event_clusters.json").write_text(
            json.dumps(
                [cluster.to_dict() for cluster in ranked_clusters],
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        ranking_audit = []

        for cluster in ranked_clusters:
            representative = cluster.representative
            dimensions = dimension_breakdown(representative)

            corroboration = round(
                cluster.event_score - representative.score,
                3,
            )

            ranking_audit.append(
                {
                    "event_id": cluster.event_id,
                    "title": representative.title,
                    "representative_source": representative.source,
                    "representative_score": representative.score,
                    "corroboration": corroboration,
                    "event_score": cluster.event_score,
                    "source_count": cluster.source_count,
                    "sources": cluster.sources,
                    "dimensions": dimensions,
                }
            )

        (OUTPUT / "ranking_audit.json").write_text(
            json.dumps(
                ranking_audit,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        evidence = build_evidence_pack(
            ranked_clusters,
            limit=5,
        )
        metrics.selected = evidence["story_count"]

        errors = validate_evidence_pack(evidence)

        if errors:
            print("QUALITY GATE FAILED")
            for error in errors:
                print(" -", error)
            return 2

        # Still zero real AI calls.
        provider = MockEditorialProvider()
        editorial = provider.generate(evidence)

        publish_path = publish_dry_run(editorial, OUTPUT)

    metrics.elapsed_seconds = round(timer.elapsed, 3)

    (OUTPUT / "evidence_pack.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    (OUTPUT / "metrics.json").write_text(
        json.dumps(metrics.to_dict(), indent=2),
        encoding="utf-8",
    )

    print()
    print("NEWS AGENT V2 - SELECTOR DIAGNOSTICS")
    print("------------------------------------")
    print(f"Collected:        {metrics.collected}")
    print(f"After gates:      {len(filtered)}")
    print(f"After dedupe:     {metrics.deduped}")
    print(f"Event clusters:   {len(clusters)}")
    print(f"Selected:         {metrics.selected}")
    print()

    print("SOURCE HEALTH")
    for name, health in source_health.items():
        status = "OK" if health["healthy"] else "FAILED/EMPTY"
        print(
            f"{name:<20} "
            f"{health['accepted']:>3} accepted  "
            f"{status}"
        )

    print()
    print("FRESHNESS")
    for bucket, count in diagnostics["freshness"].items():
        print(f"{bucket:<10} {count:>3}")

    print()
    print(f"Rejected by gates: {len(rejected)}")
    for rejection in rejected:
        print(
            f"  [{rejection['source']}] "
            f"{rejection['reason']} :: "
            f"{rejection['title']}"
        )

    print()
    print(f"AI calls:         {metrics.ai_calls}")
    print(f"Paid AI calls:    {metrics.paid_ai_calls}")
    print(f"Estimated cost:   INR {metrics.estimated_cost_inr:.2f}")
    print(f"Elapsed:          {metrics.elapsed_seconds}s")
    print(f"Dry-run payload:  {publish_path}")
    print()
    print("NO production publishing occurred.")

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.parse_args()
    raise SystemExit(run())


