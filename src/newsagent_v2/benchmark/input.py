"""
Frozen editorial benchmark-input generator.

Reads existing selector outputs and copies the Top-N ranked events plus
every cluster evidence member. Does not rerank, merge, edit, or call AI.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .contract import (
    BENCHMARK_CANDIDATE_LIMIT,
    EDITORIAL_INPUT_SCHEMA_VERSION,
    EVIDENCE_FIELDS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RANKING_AUDIT = REPO_ROOT / "output" / "ranking_audit.json"
DEFAULT_EVENT_CLUSTERS = REPO_ROOT / "output" / "event_clusters.json"
DEFAULT_OUTPUT_PATH = (
    REPO_ROOT / "output" / "benchmarks" / "editorial_input.json"
)


def _load_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def _evidence_row(member: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {}
    for field in EVIDENCE_FIELDS:
        row[field] = deepcopy(member.get(field))
    return row


def build_editorial_input(
    ranking_audit: list[dict[str, Any]],
    event_clusters: list[dict[str, Any]],
    *,
    top_n: int = BENCHMARK_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    if not isinstance(ranking_audit, list):
        raise TypeError("ranking_audit must be a list")
    if not isinstance(event_clusters, list):
        raise TypeError("event_clusters must be a list")
    if top_n < 1:
        raise ValueError("top_n must be >= 1")

    clusters_by_id: dict[str, dict[str, Any]] = {}
    for cluster in event_clusters:
        if not isinstance(cluster, dict):
            raise TypeError("each event cluster must be a dict")
        event_id = cluster.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("event cluster missing event_id")
        clusters_by_id[event_id] = cluster

    selected_rows = ranking_audit[:top_n]
    candidates: list[dict[str, Any]] = []

    for rank, row in enumerate(selected_rows, start=1):
        if not isinstance(row, dict):
            raise TypeError("each ranking_audit row must be a dict")

        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError(f"ranking_audit row {rank} missing event_id")

        cluster = clusters_by_id.get(event_id)
        if cluster is None:
            raise ValueError(
                f"ranking_audit event_id {event_id!r} has no matching cluster"
            )

        members = cluster.get("members")
        if members is None:
            raise ValueError(f"cluster {event_id!r} has no members list")
        if not isinstance(members, list):
            raise TypeError(f"cluster {event_id!r} members must be a list")

        evidence = [_evidence_row(member) for member in members]

        candidates.append(
            {
                "event_id": event_id,
                "deterministic_rank": rank,
                "event_score": deepcopy(row.get("event_score")),
                "representative_score": deepcopy(
                    row.get("representative_score")
                ),
                "corroboration": deepcopy(row.get("corroboration")),
                "source_count": deepcopy(row.get("source_count")),
                "sources": deepcopy(row.get("sources")),
                "dimensions": deepcopy(row.get("dimensions")),
                "representative_title": deepcopy(row.get("title")),
                "evidence": evidence,
            }
        )

    return {
        "schema_version": EDITORIAL_INPUT_SCHEMA_VERSION,
        "purpose": (
            "Frozen editorial-intelligence benchmark input. "
            "Values are copied from selector outputs without reranking."
        ),
        "candidate_count": len(candidates),
        "top_n_requested": top_n,
        "candidates": candidates,
    }


def build_editorial_input_from_files(
    ranking_audit_path: Path = DEFAULT_RANKING_AUDIT,
    event_clusters_path: Path = DEFAULT_EVENT_CLUSTERS,
    *,
    top_n: int = BENCHMARK_CANDIDATE_LIMIT,
) -> dict[str, Any]:
    ranking_audit = _load_json(ranking_audit_path)
    event_clusters = _load_json(event_clusters_path)
    return build_editorial_input(
        ranking_audit,
        event_clusters,
        top_n=top_n,
    )


def write_json_utf8(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        path.write_bytes(raw[3:])
    return path


def write_editorial_input(
    output_path: Path = DEFAULT_OUTPUT_PATH,
    ranking_audit_path: Path = DEFAULT_RANKING_AUDIT,
    event_clusters_path: Path = DEFAULT_EVENT_CLUSTERS,
    *,
    top_n: int = BENCHMARK_CANDIDATE_LIMIT,
) -> Path:
    payload = build_editorial_input_from_files(
        ranking_audit_path,
        event_clusters_path,
        top_n=top_n,
    )
    return write_json_utf8(output_path, payload)
