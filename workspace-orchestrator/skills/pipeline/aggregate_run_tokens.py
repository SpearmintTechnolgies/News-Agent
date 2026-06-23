#!/usr/bin/env python3
"""
aggregate_run_tokens.py — Sum LLM token usage for a pipeline run/iteration.

Scans subagent session logs under ~/.openclaw/agents/*/sessions/ for entries
that reference the run dir, groups usage by model (message-level), applies an
iteration time window for worker agents, prorates picker overhead across
batch.target_count, computes optional USD cost from openclaw.json model catalog,
and writes publish/tokens.json plus manifest.results.tokens.

Usage:
    python3 aggregate_run_tokens.py --manifest /path/to/manifest.json

Always exits 0 (fail-open). Prints TOKENS_AGGREGATED: {...}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

OPENCLAW_AGENTS = os.path.expanduser("~/.openclaw/agents")
OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
WORKER_AGENTS = ("researcher", "writer", "creator", "chart-generator")
BATCH_AGENTS = ("picker",)
ALL_AGENTS = WORKER_AGENTS + BATCH_AGENTS

ZERO_RESULT: dict[str, Any] = {
    "tokens_in": 0,
    "tokens_out": 0,
    "tokens_total": 0,
    "by_agent": {},
    "by_model": {},
    "cost_usd": 0.0,
    "pricing_available": False,
}


def load_json(path: str, default: dict | None = None) -> dict:
    if not path or not os.path.isfile(path):
        return default or {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else (default or {})
    except (OSError, json.JSONDecodeError):
        return default or {}


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iteration_window_start(manifest: dict, run_dir: str) -> datetime | None:
    run_started = os.path.join(run_dir, ".run_started")
    if os.path.isfile(run_started):
        try:
            with open(run_started, encoding="utf-8") as f:
                epoch = int(str(f.read()).strip())
            return datetime.fromtimestamp(epoch, tz=timezone.utc)
        except (OSError, ValueError):
            pass

    steps = manifest.get("steps") or {}
    research = steps.get("research") or {}
    started = parse_timestamp(research.get("started_at"))
    if started:
        return started

    return parse_timestamp(manifest.get("created_at"))


def file_references_run_dir(path: str, run_dir: str) -> bool:
    needle = run_dir.rstrip("/")
    if not needle:
        return False
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            chunk = f.read(262_144)
        return needle in chunk
    except OSError:
        return False


def session_start_timestamp(path: str) -> datetime | None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "session":
                    return parse_timestamp(obj.get("timestamp"))
                break
    except OSError:
        return None
    return None


def session_primary_model(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "model_change":
                    model_id = str(obj.get("modelId") or "").strip()
                    if model_id:
                        return model_id
    except OSError:
        return None
    return None


def empty_model_bucket() -> dict[str, int]:
    return {"tokens_in": 0, "tokens_out": 0, "tokens_total": 0}


def merge_model_usage(
    target: dict[str, dict[str, int]], source: dict[str, dict[str, int]]
) -> None:
    for model_id, usage in source.items():
        bucket = target.setdefault(model_id, empty_model_bucket())
        bucket["tokens_in"] += int(usage.get("tokens_in") or 0)
        bucket["tokens_out"] += int(usage.get("tokens_out") or 0)
        bucket["tokens_total"] += int(usage.get("tokens_total") or 0)


def prorate_model_usage(
    usage: dict[str, dict[str, int]], share: float
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for model_id, bucket in usage.items():
        out[model_id] = {
            "tokens_in": int(round(bucket["tokens_in"] * share)),
            "tokens_out": int(round(bucket["tokens_out"] * share)),
            "tokens_total": int(round(bucket["tokens_total"] * share)),
        }
    return out


def usage_by_model_from_session_jsonl(path: str) -> dict[str, dict[str, int]]:
    by_model: dict[str, dict[str, int]] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "message":
                    continue
                message = obj.get("message") or {}
                usage = message.get("usage") or {}
                if not usage:
                    continue
                model_id = str(message.get("model") or "unknown").strip() or "unknown"
                bucket = by_model.setdefault(model_id, empty_model_bucket())
                tokens_in = int(usage.get("input") or 0)
                tokens_out = int(usage.get("output") or 0)
                tokens_total = int(usage.get("totalTokens") or 0)
                if tokens_total <= 0 and (tokens_in or tokens_out):
                    tokens_total = tokens_in + tokens_out
                bucket["tokens_in"] += tokens_in
                bucket["tokens_out"] += tokens_out
                bucket["tokens_total"] += tokens_total
    except OSError:
        return {}
    return by_model


def trajectory_fallback_usage(path: str) -> dict[str, dict[str, int]]:
    base, ext = os.path.splitext(path)
    if ext != ".jsonl" or path.endswith(".trajectory.jsonl"):
        return {}

    trajectory_path = f"{base}.trajectory.jsonl"
    if not os.path.isfile(trajectory_path):
        return {}

    model_id = session_primary_model(path) or "unknown"
    try:
        with open(trajectory_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or "trace.artifacts" not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "trace.artifacts":
                    continue
                model_id = str(obj.get("modelId") or model_id).strip() or model_id
                usage = (obj.get("data") or {}).get("usage") or {}
                tokens_in = int(usage.get("input") or 0)
                tokens_out = int(usage.get("output") or 0)
                tokens_total = int(usage.get("total") or usage.get("totalTokens") or 0)
                if tokens_total <= 0 and (tokens_in or tokens_out):
                    tokens_total = tokens_in + tokens_out
                if tokens_total > 0 or tokens_in or tokens_out:
                    return {
                        model_id: {
                            "tokens_in": tokens_in,
                            "tokens_out": tokens_out,
                            "tokens_total": tokens_total,
                        }
                    }
    except OSError:
        return {}
    return {}


def session_usage_by_model(path: str) -> dict[str, dict[str, int]]:
    by_model = usage_by_model_from_session_jsonl(path)
    if by_model:
        return by_model
    return trajectory_fallback_usage(path)


def list_session_logs(agent: str) -> list[str]:
    sessions_dir = os.path.join(OPENCLAW_AGENTS, agent, "sessions")
    if not os.path.isdir(sessions_dir):
        return []
    out: list[str] = []
    for name in os.listdir(sessions_dir):
        if not name.endswith(".jsonl"):
            continue
        if name.endswith(".trajectory.jsonl"):
            continue
        path = os.path.join(sessions_dir, name)
        if os.path.isfile(path):
            out.append(path)
    out.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def load_model_catalog() -> dict[str, dict[str, Any]]:
    catalog: dict[str, dict[str, Any]] = {}
    data = load_json(OPENCLAW_JSON)
    providers = (data.get("models") or {}).get("providers") or {}
    for provider in providers.values():
        if not isinstance(provider, dict):
            continue
        for model in provider.get("models") or []:
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or "").strip()
            if not model_id:
                continue
            cost = model.get("cost") or {}
            catalog[model_id] = {
                "name": str(model.get("name") or model_id),
                "cost": {
                    "input": float(cost.get("input") or 0),
                    "output": float(cost.get("output") or 0),
                    "cacheRead": float(cost.get("cacheRead") or 0),
                    "cacheWrite": float(cost.get("cacheWrite") or 0),
                },
            }
    return catalog


def model_cost_usd(tokens_in: int, tokens_out: int, cost: dict[str, float]) -> float:
    return (
        (tokens_in / 1_000_000.0) * float(cost.get("input") or 0)
        + (tokens_out / 1_000_000.0) * float(cost.get("output") or 0)
    )


def pricing_available(catalog: dict[str, dict[str, Any]]) -> bool:
    for entry in catalog.values():
        cost = entry.get("cost") or {}
        if float(cost.get("input") or 0) > 0 or float(cost.get("output") or 0) > 0:
            return True
    return False


def finalize_by_model(
    raw_by_model: dict[str, dict[str, int]], catalog: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], float]:
    by_model: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    for model_id, usage in raw_by_model.items():
        tokens_in = int(usage.get("tokens_in") or 0)
        tokens_out = int(usage.get("tokens_out") or 0)
        tokens_total = int(usage.get("tokens_total") or 0)
        if tokens_total <= 0 and not (tokens_in or tokens_out):
            continue
        meta = catalog.get(model_id) or {}
        cost = meta.get("cost") or {}
        cost_usd = model_cost_usd(tokens_in, tokens_out, cost)
        total_cost += cost_usd
        by_model[model_id] = {
            "name": str(meta.get("name") or model_id),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_total,
            "cost_usd": round(cost_usd, 6),
        }
    return by_model, total_cost


def aggregate_tokens(manifest_path: str) -> dict[str, Any]:
    manifest = load_json(manifest_path)
    run_dir = str(manifest.get("run_dir") or "").strip()
    if not run_dir:
        return dict(ZERO_RESULT)

    batch = manifest.get("batch") or {}
    target_count = int(batch.get("target_count") or 1)
    if target_count < 1:
        target_count = 1

    catalog = load_model_catalog()
    window_start = iteration_window_start(manifest, run_dir)
    raw_by_model: dict[str, dict[str, int]] = {}
    by_agent: dict[str, dict[str, Any]] = {}

    for agent in ALL_AGENTS:
        agent_models: dict[str, dict[str, int]] = {}
        apply_window = agent in WORKER_AGENTS
        picker_share = 1.0 / target_count if agent in BATCH_AGENTS else 1.0

        for path in list_session_logs(agent):
            if not file_references_run_dir(path, run_dir):
                continue

            if apply_window and window_start is not None:
                started = session_start_timestamp(path)
                if started is not None and started < window_start:
                    continue

            session_models = session_usage_by_model(path)
            if not session_models:
                continue

            if agent in BATCH_AGENTS:
                session_models = prorate_model_usage(session_models, picker_share)

            merge_model_usage(agent_models, session_models)
            merge_model_usage(raw_by_model, session_models)

        if not agent_models:
            continue

        agent_in = agent_out = agent_total = 0
        for usage in agent_models.values():
            agent_in += int(usage.get("tokens_in") or 0)
            agent_out += int(usage.get("tokens_out") or 0)
            agent_total += int(usage.get("tokens_total") or 0)

        by_agent[agent] = {
            "tokens_in": agent_in,
            "tokens_out": agent_out,
            "tokens_total": agent_total,
            "models": sorted(agent_models.keys()),
        }

    by_model, cost_usd = finalize_by_model(raw_by_model, catalog)
    tokens_in = sum(int(m.get("tokens_in") or 0) for m in by_model.values())
    tokens_out = sum(int(m.get("tokens_out") or 0) for m in by_model.values())
    tokens_total = sum(int(m.get("tokens_total") or 0) for m in by_model.values())

    return {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "by_agent": by_agent,
        "by_model": by_model,
        "cost_usd": round(cost_usd, 6),
        "pricing_available": pricing_available(catalog),
        "run_id": manifest.get("run_id"),
        "run_dir": run_dir,
        "aggregated_at": datetime.now(timezone.utc).isoformat(),
    }


def write_tokens_artifacts(manifest_path: str, result: dict[str, Any]) -> None:
    manifest = load_json(manifest_path)
    run_dir = str(manifest.get("run_dir") or "").strip()
    if not run_dir:
        return

    publish_dir = os.path.join(run_dir, "publish")
    os.makedirs(publish_dir, exist_ok=True)
    tokens_path = os.path.join(publish_dir, "tokens.json")
    with open(tokens_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    if "results" not in manifest or not isinstance(manifest.get("results"), dict):
        manifest["results"] = {}
    manifest["results"]["tokens"] = result

    tmp = manifest_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    os.replace(tmp, manifest_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Aggregate LLM token usage for a pipeline run")
    parser.add_argument("--manifest", required=True, help="Path to pipeline manifest.json")
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)
    try:
        result = aggregate_tokens(manifest_path)
        write_tokens_artifacts(manifest_path, result)
        print(f"TOKENS_AGGREGATED: {json.dumps(result, ensure_ascii=False)}")
    except Exception as exc:
        print(f"TOKENS_AGGREGATED: {json.dumps(ZERO_RESULT, ensure_ascii=False)}", file=sys.stderr)
        print(f"TOKENS_AGGREGATE_WARN: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
