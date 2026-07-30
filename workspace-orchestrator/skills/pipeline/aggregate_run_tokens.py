#!/usr/bin/env python3
"""
aggregate_run_tokens.py — Sum LLM token usage for a pipeline run/iteration.

Scans subagent session logs under ~/.openclaw/agents/*/sessions/ for entries
that reference the run dir, groups usage by model (message-level), applies an
iteration time window for worker agents, time-slices orchestrator messages
within the iteration window, prorates picker overhead across batch.target_count,
computes optional USD cost from openclaw.json model catalog, and writes
publish/tokens.json plus manifest.results.tokens.

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
IMAGE_PRICING_JSON = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/config/image-model-pricing.json"
)
WORKER_AGENTS = ("researcher", "writer", "creator", "chart-generator")
BATCH_AGENTS = ("picker",)
ORCHESTRATOR_AGENTS = ("orchestrator",)
ALL_AGENTS = WORKER_AGENTS + BATCH_AGENTS + ORCHESTRATOR_AGENTS

ZERO_RESULT: dict[str, Any] = {
    "tokens_in": 0,
    "tokens_out": 0,
    "tokens_total": 0,
    "by_agent": {},
    "by_model": {},
    "cost_usd": 0.0,
    "pricing_available": False,
    "duration_seconds": 0,
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


def message_timestamp(obj: dict) -> datetime | None:
    ts = parse_timestamp(obj.get("timestamp"))
    if ts:
        return ts
    message = obj.get("message") or {}
    raw = message.get("timestamp")
    if isinstance(raw, (int, float)) and raw > 0:
        return datetime.fromtimestamp(float(raw) / 1000.0, tz=timezone.utc)
    if raw is not None:
        return parse_timestamp(str(raw))
    return None


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


def iteration_window_end(manifest: dict) -> datetime:
    steps = manifest.get("steps") or {}
    wordpress = steps.get("wordpress") or {}
    finished = parse_timestamp(wordpress.get("finished_at"))
    if finished:
        return finished

    latest: datetime | None = None
    for step in steps.values():
        if not isinstance(step, dict):
            continue
        for key in ("finished_at", "started_at"):
            ts = parse_timestamp(step.get(key))
            if ts and (latest is None or ts > latest):
                latest = ts
    return latest or datetime.now(timezone.utc)


def duration_from_steps(manifest: dict) -> int | None:
    steps = manifest.get("steps") or {}
    earliest: datetime | None = None
    latest: datetime | None = None
    for step in steps.values():
        if not isinstance(step, dict):
            continue
        started = parse_timestamp(step.get("started_at"))
        finished = parse_timestamp(step.get("finished_at"))
        for ts in (started, finished):
            if ts is None:
                continue
            if earliest is None or ts < earliest:
                earliest = ts
            if latest is None or ts > latest:
                latest = ts
    if earliest and latest and latest >= earliest:
        return max(0, int((latest - earliest).total_seconds()))
    return None


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
    return {"tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_total": 0}


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


def _accumulate_message_usage(
    by_model: dict[str, dict[str, int]], message: dict
) -> None:
    usage = message.get("usage") or {}
    if not usage:
        return
    model_id = str(message.get("model") or "unknown").strip() or "unknown"
    bucket = by_model.setdefault(model_id, empty_model_bucket())
    tokens_in = int(usage.get("input") or usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    tokens_out = int(usage.get("output") or usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cacheRead") or usage.get("cache_read_input_tokens") or usage.get("cache_read") or 0)
    tokens_total = int(usage.get("totalTokens") or usage.get("total_tokens") or usage.get("total") or 0)
    if tokens_total <= 0 and (tokens_in or tokens_out):
        tokens_total = tokens_in + tokens_out
    bucket["tokens_in"] += tokens_in
    bucket["tokens_out"] += tokens_out
    bucket["tokens_cache_read"] = bucket.get("tokens_cache_read", 0) + cache_read
    bucket["tokens_total"] += tokens_total


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
                _accumulate_message_usage(by_model, message)
    except OSError:
        return {}
    return by_model


def usage_by_model_in_window(
    path: str,
    window_start: datetime | None,
    window_end: datetime | None,
) -> dict[str, dict[str, int]]:
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
                msg_ts = message_timestamp(obj)
                if msg_ts is None:
                    continue
                if window_start is not None and msg_ts < window_start:
                    continue
                if window_end is not None and msg_ts > window_end:
                    continue
                message = obj.get("message") or {}
                _accumulate_message_usage(by_model, message)
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
                    "per_image": float(cost.get("per_image") or 0),
                },
            }
    return catalog


def model_cost_usd(tokens_in: int, tokens_out: int, cost: dict[str, float], tokens_cache_read: int = 0) -> float:
    input_price = float(cost.get("input") or 0)
    output_price = float(cost.get("output") or 0)
    cache_price = float(cost.get("cacheRead") or input_price)
    
    uncached_in = max(0, tokens_in - tokens_cache_read)
    
    cost_in = (uncached_in / 1_000_000.0) * input_price
    cost_cache = (tokens_cache_read / 1_000_000.0) * cache_price
    cost_out = (tokens_out / 1_000_000.0) * output_price
    
    return cost_in + cost_cache + cost_out


def model_is_priced(cost: dict[str, float]) -> bool:
    return (
        float(cost.get("input") or 0) > 0
        or float(cost.get("output") or 0) > 0
        or float(cost.get("per_image") or 0) > 0
    )


def pricing_available(catalog: dict[str, dict[str, Any]]) -> bool:
    for entry in catalog.values():
        cost = entry.get("cost") or {}
        if model_is_priced(cost):
            return True
    return False


def load_image_pricing() -> dict[str, dict[str, Any]]:
    data = load_json(IMAGE_PRICING_JSON)
    out: dict[str, dict[str, Any]] = {}
    for model_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        out[str(model_id)] = {
            "name": str(entry.get("name") or model_id),
            "per_image": float(entry.get("per_image") or 0),
        }
    return out


def image_model_cost_usd(model_id: str, images: int, catalog: dict[str, dict[str, Any]]) -> tuple[float, str, bool]:
    pricing = load_image_pricing()
    meta = pricing.get(model_id) or catalog.get(model_id) or {}
    name = str(meta.get("name") or model_id)
    per_image = float(meta.get("per_image") or (meta.get("cost") or {}).get("per_image") or 0)
    priced = per_image > 0
    return round(per_image * images, 6), name, priced


def load_image_cost(run_dir: str) -> dict[str, Any] | None:
    path = os.path.join(run_dir, "publish", "image-cost.json")
    data = load_json(path)
    if not data or not data.get("model"):
        return None
    return data


def merge_image_cost(
    result: dict[str, Any],
    image_cost: dict[str, Any],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    model_id = str(image_cost.get("model") or "").strip()
    if not model_id:
        return result

    images = int(image_cost.get("images") or 1)
    image_cost_usd, model_name, priced = image_model_cost_usd(model_id, images, catalog)
    if float(image_cost.get("cost_usd") or 0) > 0:
        image_cost_usd = round(float(image_cost["cost_usd"]) * images, 6)

    by_model = dict(result.get("by_model") or {})
    existing = by_model.get(model_id) or {}
    by_model[model_id] = {
        "name": str(image_cost.get("model_name") or model_name or model_id),
        "tokens_in": int(existing.get("tokens_in") or 0),
        "tokens_out": int(existing.get("tokens_out") or 0),
        "tokens_total": int(existing.get("tokens_total") or 0),
        "images": images,
        "cost_usd": round(float(existing.get("cost_usd") or 0) + image_cost_usd, 6),
        "priced": priced or bool(existing.get("priced")),
    }

    by_agent = dict(result.get("by_agent") or {})
    creator = dict(by_agent.get("creator") or {})
    creator["cost_usd"] = round(float(creator.get("cost_usd") or 0) + image_cost_usd, 6)
    by_agent["creator"] = creator

    result["by_model"] = by_model
    result["by_agent"] = by_agent
    result["image_cost"] = {
        "model": model_id,
        "model_name": str(image_cost.get("model_name") or model_name or model_id),
        "images": images,
        "cost_usd": image_cost_usd,
        "priced": priced,
    }
    result["image_cost_usd"] = image_cost_usd
    result["cost_usd"] = round(float(result.get("cost_usd") or 0) + image_cost_usd, 6)
    if priced:
        result["pricing_available"] = True
    return result


def finalize_by_model(
    raw_by_model: dict[str, dict[str, int]], catalog: dict[str, dict[str, Any]]
) -> tuple[dict[str, dict[str, Any]], float, list[str]]:
    by_model: dict[str, dict[str, Any]] = {}
    total_cost = 0.0
    missing_prices: list[str] = []
    for model_id, usage in raw_by_model.items():
        tokens_in = int(usage.get("tokens_in") or 0)
        tokens_out = int(usage.get("tokens_out") or 0)
        tokens_total = int(usage.get("tokens_total") or 0)
        if tokens_total <= 0 and not (tokens_in or tokens_out):
            continue
        meta = catalog.get(model_id) or {}
        cost = meta.get("cost") or {}
        priced = model_is_priced(cost)
        if not priced and (tokens_in or tokens_out or tokens_total):
            missing_prices.append(model_id)
        cost_usd = model_cost_usd(tokens_in, tokens_out, cost)
        total_cost += cost_usd
        by_model[model_id] = {
            "name": str(meta.get("name") or model_id),
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_total,
            "cost_usd": round(cost_usd, 6),
            "priced": priced,
        }
    return by_model, total_cost, missing_prices


def primary_model_for_agent(agent_models: dict[str, dict[str, int]]) -> str | None:
    if not agent_models:
        return None
    return max(
        agent_models.items(),
        key=lambda kv: int(kv[1].get("tokens_total") or 0),
    )[0]


def session_time_span(
    path: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Return (first, last) message timestamps in a session, optionally window-bounded."""
    first: datetime | None = None
    last: datetime | None = None
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
                msg_ts = message_timestamp(obj)
                if msg_ts is None:
                    continue
                if window_start is not None and msg_ts < window_start:
                    continue
                if window_end is not None and msg_ts > window_end:
                    continue
                if first is None or msg_ts < first:
                    first = msg_ts
                if last is None or msg_ts > last:
                    last = msg_ts
    except OSError:
        return None, None
    return first, last


def span_duration_seconds(first: datetime | None, last: datetime | None) -> int:
    if first is None or last is None or last < first:
        return 0
    return max(0, int((last - first).total_seconds()))


def merge_time_span(
    agent_first: datetime | None,
    agent_last: datetime | None,
    session_first: datetime | None,
    session_last: datetime | None,
) -> tuple[datetime | None, datetime | None]:
    if session_first is None and session_last is None:
        return agent_first, agent_last
    if agent_first is None or (session_first is not None and session_first < agent_first):
        agent_first = session_first
    if agent_last is None or (session_last is not None and session_last > agent_last):
        agent_last = session_last
    return agent_first, agent_last


def agent_cost_usd(
    agent_models: dict[str, dict[str, int]],
    catalog: dict[str, dict[str, Any]],
) -> float:
    total = 0.0
    for model_id, usage in agent_models.items():
        meta = catalog.get(model_id) or {}
        cost = meta.get("cost") or {}
        total += model_cost_usd(
            int(usage.get("tokens_in") or 0),
            int(usage.get("tokens_out") or 0),
            cost,
        )
    return round(total, 6)


def finalize_by_agent(
    by_agent_raw: dict[str, dict[str, Any]],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for agent, data in by_agent_raw.items():
        agent_models = data.get("_models") or {}
        primary = primary_model_for_agent(agent_models)
        first = data.get("_first_ts")
        last = data.get("_last_ts")
        out[agent] = {
            "tokens_in": int(data.get("tokens_in") or 0),
            "tokens_out": int(data.get("tokens_out") or 0),
            "tokens_total": int(data.get("tokens_total") or 0),
            "models": sorted(agent_models.keys()),
            "primary_model": primary,
            "cost_usd": agent_cost_usd(agent_models, catalog),
            "duration_seconds": span_duration_seconds(first, last),
        }
    return out


def total_run_duration_seconds(
    by_agent_raw: dict[str, dict[str, Any]],
    manifest: dict,
    window_start: datetime | None,
    window_end: datetime | None,
) -> int:
    orch = by_agent_raw.get("orchestrator") or {}
    orch_first = orch.get("_first_ts")
    orch_last = orch.get("_last_ts")
    orch_duration = span_duration_seconds(orch_first, orch_last)
    if orch_duration > 0:
        return orch_duration

    global_first: datetime | None = None
    global_last: datetime | None = None
    for data in by_agent_raw.values():
        global_first, global_last = merge_time_span(
            global_first,
            global_last,
            data.get("_first_ts"),
            data.get("_last_ts"),
        )
    all_agents_duration = span_duration_seconds(global_first, global_last)
    if all_agents_duration > 0:
        return all_agents_duration

    if window_start is not None:
        return max(0, int((window_end - window_start).total_seconds()))
    return duration_from_steps(manifest) or 0


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
    window_end = iteration_window_end(manifest)
    raw_by_model: dict[str, dict[str, int]] = {}
    by_agent_raw: dict[str, dict[str, Any]] = {}

    for agent in ALL_AGENTS:
        agent_models: dict[str, dict[str, int]] = {}
        agent_first: datetime | None = None
        agent_last: datetime | None = None
        apply_session_window = agent in WORKER_AGENTS
        use_time_slice = agent in ORCHESTRATOR_AGENTS
        picker_share = 1.0 / target_count if agent in BATCH_AGENTS else 1.0

        for path in list_session_logs(agent):
            if not file_references_run_dir(path, run_dir):
                continue

            if use_time_slice:
                if window_start is None:
                    continue
                session_models = usage_by_model_in_window(path, window_start, window_end)
                session_first, session_last = session_time_span(path, window_start, window_end)
            else:
                if apply_session_window and window_start is not None:
                    started = session_start_timestamp(path)
                    if started is not None and started < window_start:
                        continue
                session_models = session_usage_by_model(path)
                session_first, session_last = session_time_span(path)

            if not session_models:
                continue

            agent_first, agent_last = merge_time_span(
                agent_first, agent_last, session_first, session_last
            )

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

        by_agent_raw[agent] = {
            "tokens_in": agent_in,
            "tokens_out": agent_out,
            "tokens_total": agent_total,
            "_models": agent_models,
            "_first_ts": agent_first,
            "_last_ts": agent_last,
        }

    by_model, cost_usd, missing_prices = finalize_by_model(raw_by_model, catalog)
    for model_id in missing_prices:
        print(f"TOKENS_PRICE_MISSING: {model_id}", file=sys.stderr)

    by_agent = finalize_by_agent(by_agent_raw, catalog)
    tokens_in = sum(int(m.get("tokens_in") or 0) for m in by_model.values())
    tokens_out = sum(int(m.get("tokens_out") or 0) for m in by_model.values())
    tokens_total = sum(int(m.get("tokens_total") or 0) for m in by_model.values())

    duration_seconds = total_run_duration_seconds(
        by_agent_raw, manifest, window_start, window_end
    )

    result = {
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_total,
        "by_agent": by_agent,
        "by_model": by_model,
        "cost_usd": round(cost_usd, 6),
        "pricing_available": pricing_available(catalog),
        "duration_seconds": duration_seconds,
        "run_id": manifest.get("run_id"),
        "run_dir": run_dir,
        "aggregated_at": datetime.now(timezone.utc).isoformat(),
    }

    image_cost = load_image_cost(run_dir)
    if image_cost:
        result = merge_image_cost(result, image_cost, catalog)

    return result


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
