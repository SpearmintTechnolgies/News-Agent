"""Reuse existing discovery → cluster → rank → Top-5 evidence cut."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from newsagent_v2.cluster import cluster_events
from newsagent_v2.collect import collect_rss, load_sources
from newsagent_v2.dedupe import dedupe
from newsagent_v2.diagnostics import filter_candidates
from newsagent_v2.evidence import build_evidence_pack
from newsagent_v2.rank import rank_clusters

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config" / "sources.json"


def discover_ranked_top5(*, sources_path: Path | None = None) -> dict[str, Any]:
    sources = load_sources(sources_path or CONFIG)
    items, source_health = collect_rss(sources)
    filtered, rejected = filter_candidates(items)
    unique = dedupe(filtered)
    clusters = cluster_events(unique)
    ranked = rank_clusters(clusters)
    evidence = build_evidence_pack(ranked, limit=5)
    return {
        "ranked_clusters": ranked,
        "evidence_pack": evidence,
        "source_health": source_health,
        "rejected": rejected,
        "collected": len(items),
    }
