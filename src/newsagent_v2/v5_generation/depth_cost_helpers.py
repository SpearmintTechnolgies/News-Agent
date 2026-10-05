"""Pure helpers for V5 depth fallback + authoritative Kimi/Vertex USD costs.

Safe for unit tests. Never invents pricing. No network I/O beyond reading local receipt files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DEPTH_FALLBACK_PASS = "DEPTH_FALLBACK_PASS"
DEPTH_NORMAL_PASS = "DEPTH_NORMAL_PASS"
DEPTH_FAIL = "DEPTH_FAIL"
SUPPRESSIBLE_DEPTH_CODE = "below_article_minimum_length"
SUPPRESSIBLE_DEPTH_CODES = frozenset(
    {
        "below_article_minimum_length",
        "below_absolute_publication_minimum",
    }
)
# Depth bands (output words): 600+ normal; 500-599 fallback; 400-499 exceptional; <400 hard block.
# LIMITED_DEPTH_BRIEF may fall back from 200.
NORMAL_PASS_MIN = 600
STANDARD_FALLBACK_MIN = 500
ABSOLUTE_HARD_MIN = NORMAL_PASS_MIN  # alias: normal generation-ready threshold
FALLBACK_FLOOR = 400
LIMITED_FALLBACK_FLOOR = 200
EXCEPTIONAL_FALLBACK_MIN = FALLBACK_FLOOR
DEFAULT_VERTEX_MODEL = "gemini-3.1-flash-image"

# Top-5 prequalification: evidence text must support ~600-800 grounded body words.
try:
    from newsagent_v2.article_readiness import MIN_EVIDENCE_WORDS as PUBLISHABLE_EVIDENCE_WORD_FLOOR
except Exception:  # pragma: no cover
    PUBLISHABLE_EVIDENCE_WORD_FLOOR = 100
try:
    from newsagent_v2.article_readiness import INTENTIONAL_ARTICLE_WORDS as TARGET_BODY_CAPACITY_MIN
except Exception:  # pragma: no cover
    TARGET_BODY_CAPACITY_MIN = 600


def evidence_supports_publishable_depth(metrics: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Return whether readiness metrics indicate enough grounded material for ~600-800 body words.

    Pure/local. Does not weaken ranking; used after bounded source expansion to
    skip evidence-thin candidates so the next ranked story can backfill Top-5.
    """
    metrics = metrics or {}
    reasons: list[str] = []
    evidence_words = int(metrics.get("evidence_word_count") or 0)
    sources = int(metrics.get("distinct_source_count") or 0)
    items = int(metrics.get("evidence_item_count") or 0)
    intended = int(metrics.get("intended_article_words") or TARGET_BODY_CAPACITY_MIN)
    if intended < TARGET_BODY_CAPACITY_MIN:
        reasons.append("intended_capacity_below_600")
    if evidence_words < PUBLISHABLE_EVIDENCE_WORD_FLOOR:
        reasons.append("insufficient_depth_capacity_for_publication")
    if sources < 2:
        reasons.append("source_diversity_below_minimum")
    if items < 2:
        reasons.append("evidence_quantity_below_minimum")
    return (not reasons), reasons

KIMI_VERIFIED = "NEWSAGENT_V2_KIMI_PRICING_VERIFIED"
KIMI_INPUT = "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK"
KIMI_OUTPUT = "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK"
VERTEX_VERIFIED = "NEWSAGENT_V2_VERTEX_PRICING_VERIFIED"
VERTEX_INPUT = "NEWSAGENT_V2_VERTEX_INPUT_USD_PER_MTOK"
VERTEX_IMAGE = "NEWSAGENT_V2_VERTEX_IMAGE_OUTPUT_USD_PER_MTOK"
VERTEX_MODEL = "NEWSAGENT_V2_VERTEX_MODEL"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def configured_vertex_model(environ: dict[str, str] | None) -> str:
    env = environ or {}
    return str(env.get(VERTEX_MODEL) or "").strip() or DEFAULT_VERTEX_MODEL


def missing_kimi_pricing_keys(environ: dict[str, str] | None, usage: dict[str, Any] | None = None) -> list[str]:
    usage = usage or {}
    for key in ("cost_usd", "billed_cost_usd", "article_generation_cost_usd", "provider_reported_cost_usd", "total_cost_usd"):
        if _num(usage.get(key)) is not None:
            return []
    env = environ or {}
    missing: list[str] = []
    if not _truthy(env.get(KIMI_VERIFIED)):
        missing.append(KIMI_VERIFIED)
    for key in (KIMI_INPUT, KIMI_OUTPUT):
        raw = str(env.get(key) or "").strip()
        if not raw:
            missing.append(key)
            continue
        try:
            float(raw)
        except ValueError:
            missing.append(key)
    return missing


def missing_vertex_pricing_keys(environ: dict[str, str] | None, image: dict[str, Any] | None = None) -> list[str]:
    image = image or {}
    if _num(image.get("provider_reported_cost_usd")) is not None:
        return []
    # accumulated billed already numeric?
    if _num(image.get("accumulated_cost_usd")) is not None:
        return []
    env = environ or {}
    missing: list[str] = []
    if not _truthy(env.get(VERTEX_VERIFIED)):
        missing.append(VERTEX_VERIFIED)
    for key in (VERTEX_INPUT, VERTEX_IMAGE):
        raw = str(env.get(key) or "").strip()
        if not raw:
            missing.append(key)
            continue
        try:
            float(raw)
        except ValueError:
            missing.append(key)
    return missing


def compute_kimi_cost_usd(usage: dict[str, Any], environ: dict[str, str] | None) -> tuple[float | None, list[str]]:
    usage = dict(usage or {})
    for key in ("cost_usd", "billed_cost_usd", "article_generation_cost_usd", "provider_reported_cost_usd", "total_cost_usd"):
        billed = _num(usage.get(key))
        if billed is not None:
            return billed, []
    missing = missing_kimi_pricing_keys(environ, usage)
    if missing:
        return None, missing
    from newsagent_v2.v5_generation.kimi_pricing import (
        compute_cost_from_tokens,
        extract_token_usage,
        resolve_verified_kimi_pricing,
    )
    tokens = extract_token_usage(usage)
    cost = compute_cost_from_tokens(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        pricing=resolve_verified_kimi_pricing(environ or {}, usage=usage),
    ).get("article_generation_cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost), []
    return None, missing_kimi_pricing_keys(environ, usage) or [KIMI_VERIFIED]


def _image_modality_tokens(usage: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """Return (input_tokens, image_output_tokens, total_tokens)."""
    if not isinstance(usage, dict):
        return None, None, None
    inp = usage.get("promptTokenCount")
    total = usage.get("totalTokenCount")
    image_tokens = None
    details = usage.get("candidatesTokensDetails") or []
    if isinstance(details, list):
        for row in details:
            if isinstance(row, dict) and str(row.get("modality") or "").upper() == "IMAGE":
                try:
                    image_tokens = int(row["tokenCount"])
                    break
                except (KeyError, TypeError, ValueError):
                    pass
    if image_tokens is None:
        cand = usage.get("candidatesTokenCount")
        try:
            image_tokens = int(cand) if cand is not None else None
        except (TypeError, ValueError):
            image_tokens = None
    try:
        inp_i = int(inp) if inp is not None else None
    except (TypeError, ValueError):
        inp_i = None
    try:
        total_i = int(total) if total is not None else None
    except (TypeError, ValueError):
        total_i = None
    return inp_i, image_tokens, total_i


def compute_vertex_receipt_cost_usd(receipt: dict[str, Any], environ: dict[str, str] | None) -> tuple[float | None, list[str]]:
    billed = _num(receipt.get("provider_reported_cost_usd"))
    if billed is not None:
        return billed, []
    env = environ or {}
    provider = str(receipt.get("provider_name") or receipt.get("provider") or "").strip().lower()
    model = str(receipt.get("model_name") or receipt.get("model") or "").strip()
    expected = configured_vertex_model(env)
    if provider and provider != "vertex":
        return None, []
    if model and model != expected:
        return None, [f"model_mismatch:{model}!={expected}"]
    missing = missing_vertex_pricing_keys(env, receipt)
    if missing:
        return None, missing
    usage = receipt.get("provider_reported_usage") or {}
    inp, image_tok, _ = _image_modality_tokens(usage if isinstance(usage, dict) else {})
    if inp is None or image_tok is None:
        return None, ["provider_reported_usage.promptTokenCount", "candidatesTokensDetails[IMAGE].tokenCount"]
    input_rate = float(str(env[VERTEX_INPUT]).strip())
    image_rate = float(str(env[VERTEX_IMAGE]).strip())
    return round(inp / 1_000_000 * input_rate + image_tok / 1_000_000 * image_rate, 8), []


def accumulate_vertex_receipts(
    event_id: str,
    make_runs_root: Path | str,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = Path(make_runs_root)
    receipts: list[dict[str, Any]] = []
    if root.is_dir():
        paths = sorted(root.glob(f"{event_id}-*/provider_result.json"), key=lambda p: p.stat().st_mtime)
        for path in paths:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            data = dict(data)
            data["_receipt_path"] = str(path)
            receipts.append(data)

    expected = configured_vertex_model(environ)
    total_cost = 0.0
    cost_known = True
    missing_keys: list[str] = []
    input_tokens = 0
    image_tokens = 0
    total_tokens = 0
    requests = 0
    provider = "vertex"
    model = expected
    usages: list[dict[str, Any]] = []
    image_attempts: list[dict[str, Any]] = []

    for receipt in receipts:
        prov = str(receipt.get("provider_name") or receipt.get("provider") or "vertex").strip().lower()
        mod = str(receipt.get("model_name") or receipt.get("model") or expected).strip()
        # include vertex receipts for configured model, or any with billed USD
        billed = _num(receipt.get("provider_reported_cost_usd"))
        if prov not in {"", "vertex"} and billed is None:
            continue
        if mod and mod != expected and billed is None:
            continue
        requests += 1
        provider = prov or provider
        model = mod or model
        usage = receipt.get("provider_reported_usage") if isinstance(receipt.get("provider_reported_usage"), dict) else {}
        usages.append(usage)
        inp, img, tot = _image_modality_tokens(usage)
        image_attempts.append({"input_tokens": inp, "output_tokens": img, "cost_usd": None})
        if inp is not None:
            input_tokens += inp
        if img is not None:
            image_tokens += img
        if tot is not None:
            total_tokens += tot
        cost, missing = compute_vertex_receipt_cost_usd(receipt, environ)
        if image_attempts:
            image_attempts[-1]["cost_usd"] = cost
        if cost is None:
            cost_known = False
            for key in missing:
                if key not in missing_keys and not str(key).startswith("model_mismatch"):
                    missing_keys.append(key)
        else:
            total_cost += cost

    image: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "requests": requests,
        "provider_reported_usage": {
            "promptTokenCount": input_tokens if requests else None,
            "candidatesTokenCount": image_tokens if requests else None,
            "totalTokenCount": total_tokens if requests else None,
            "image_tokens": image_tokens if requests else None,
            "candidatesTokensDetails": (
                [{"modality": "IMAGE", "tokenCount": image_tokens}] if requests else []
            ),
        },
        "receipt_count": len(receipts),
        "accumulated_receipts": requests,
        "attempts": image_attempts,
    }
    if cost_known and requests:
        image["provider_reported_cost_usd"] = round(total_cost, 8)
        image["accumulated_cost_usd"] = round(total_cost, 8)
    elif missing_keys:
        image["missing_pricing_keys"] = missing_keys
    return image


def format_usd(value: float | None, missing_keys: list[str] | None = None) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"${float(value):.4f} USD"
    if missing_keys:
        return "unavailable (missing: " + ", ".join(missing_keys) + ")"
    return "unavailable"


def merge_usage(prior: dict[str, Any] | None, new: dict[str, Any] | None, *, provider_calls: int) -> dict[str, Any]:
    """Accumulate billable usage. Zero provider calls must not inflate totals."""
    prior = dict(prior or {})
    new = dict(new or {})
    if provider_calls <= 0:
        return prior or new

    def _i(d: dict[str, Any], *keys: str) -> int:
        for k in keys:
            v = d.get(k)
            try:
                if v is not None:
                    return int(v)
            except (TypeError, ValueError):
                pass
        return 0

    out = dict(prior)
    out.update({k: v for k, v in new.items() if v is not None})
    out["prompt_tokens"] = _i(prior, "prompt_tokens", "input_tokens") + _i(new, "prompt_tokens", "input_tokens")
    out["completion_tokens"] = _i(prior, "completion_tokens", "output_tokens") + _i(new, "completion_tokens", "output_tokens")
    out["input_tokens"] = out["prompt_tokens"]
    out["output_tokens"] = out["completion_tokens"]
    out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    out["requests"] = _i(prior, "requests", "request_count") + _i(new, "requests", "request_count")
    out["request_count"] = out["requests"]
    # costs: sum numeric only
    pc = _num(prior.get("cost_usd") or prior.get("article_generation_cost_usd") or prior.get("accumulated_cost_usd"))
    nc = _num(new.get("cost_usd") or new.get("article_generation_cost_usd") or new.get("provider_reported_cost_usd") or new.get("accumulated_cost_usd"))
    if pc is not None or nc is not None:
        total = (pc or 0.0) + (nc or 0.0)
        out["cost_usd"] = round(total, 8)
        out["article_generation_cost_usd"] = out["cost_usd"]
        out["accumulated_cost_usd"] = out["cost_usd"]
        out["provider_reported_cost_usd"] = out["cost_usd"]
    return out


def _stage_label(stage: str) -> str:
    name = str(stage or "call").replace("_", " ")
    if name.startswith("revise "):
        return name
    return name or "call"


def _money(prompt: int, completion: int, environ: dict[str, str] | None) -> tuple[float | None, list[str]]:
    return compute_kimi_cost_usd(
        {"prompt_tokens": prompt, "completion_tokens": completion},
        environ,
    )


def _group_lines(
    title: str,
    rows: list[dict[str, Any]],
    environ: dict[str, str] | None,
    escape_html,
) -> tuple[list[str], float | None, list[str]]:
    """One heading plus a cost line for every call in the group."""
    if not rows:
        return [f"{title} — none — $0.0000"], 0.0, []
    priced: list[tuple[dict[str, Any], float | None, list[str]]] = []
    missing: list[str] = []
    subtotal = 0.0
    known = True
    for row in rows:
        prompt = int(row.get("prompt_tokens") or row.get("input_tokens") or 0)
        completion = int(row.get("completion_tokens") or row.get("output_tokens") or 0)
        cost = row.get("cost_usd")
        row_missing: list[str] = []
        if cost is None:
            cost, row_missing = _money(prompt, completion, environ)
        if cost is None:
            known = False
            for key in row_missing:
                if key not in missing:
                    missing.append(key)
        else:
            subtotal += float(cost)
        priced.append((row, None if cost is None else float(cost), row_missing))
    group_cost = round(subtotal, 8) if known else None
    lines = [
        f"{title} — {len(rows)} {'call' if len(rows) == 1 else 'calls'} — {format_usd(group_cost, missing)}"
    ]
    for index, (row, cost, row_missing) in enumerate(priced, start=1):
        prompt = row.get("prompt_tokens", row.get("input_tokens", "unavailable"))
        completion = row.get("completion_tokens", row.get("output_tokens", "unavailable"))
        label = escape_html(_stage_label(str(row.get("stage") or "")))
        prefix = f"{index}. {label} — " if label and label != "call" else f"{index}. "
        lines.append(
            f"{prefix}Input: {prompt} | Output: {completion} | {format_usd(cost, row_missing)}"
        )
    return lines, group_cost, missing


def build_review_cost_text(
    *,
    text_usage: dict[str, Any],
    image_usage: dict[str, Any],
    environ: dict[str, str] | None,
    escape_html,
) -> str:
    attempts = [row for row in (text_usage.get("attempts") or []) if isinstance(row, dict)]
    if not attempts and (text_usage.get("prompt_tokens") or text_usage.get("completion_tokens")):
        attempts = [{
            "kind": "content",
            "stage": "draft",
            "prompt_tokens": text_usage.get("prompt_tokens") or 0,
            "completion_tokens": text_usage.get("completion_tokens") or 0,
        }]
    content_rows = [row for row in attempts if str(row.get("kind") or "content") != "orchestration"]
    orchestration_rows = [row for row in attempts if str(row.get("kind") or "") == "orchestration"]
    content_lines, content_cost, content_missing = _group_lines("CONTENT", content_rows, environ, escape_html)
    orchestration_lines, orchestration_cost, orchestration_missing = _group_lines(
        "ORCHESTRATION", orchestration_rows, environ, escape_html
    )

    image_cost = _num(image_usage.get("accumulated_cost_usd") or image_usage.get("provider_reported_cost_usd"))
    image_missing = list(image_usage.get("missing_pricing_keys") or [])
    image_rows = [row for row in (image_usage.get("attempts") or []) if isinstance(row, dict)]
    if not image_rows:
        usage = image_usage.get("provider_reported_usage") if isinstance(image_usage.get("provider_reported_usage"), dict) else {}
        img_in, img_out, _img_tot = _image_modality_tokens(usage)
        if image_usage.get("requests") or img_in is not None or img_out is not None or image_cost is not None:
            if image_cost is None:
                image_cost, image_missing = compute_vertex_receipt_cost_usd(
                    {
                        "provider_name": image_usage.get("provider"),
                        "model_name": image_usage.get("model"),
                        "provider_reported_usage": usage,
                        "provider_reported_cost_usd": image_usage.get("provider_reported_cost_usd"),
                    },
                    environ,
                )
            image_rows = [{
                "stage": "image",
                "prompt_tokens": 0 if img_in is None else img_in,
                "completion_tokens": 0 if img_out is None else img_out,
                "cost_usd": image_cost,
            }]
    image_lines = [f"IMAGE — {len(image_rows)} {'call' if len(image_rows) == 1 else 'calls'} — {format_usd(image_cost, image_missing)}"]
    if image_rows:
        running = 0.0
        image_known = image_cost is not None or all(row.get("cost_usd") is not None for row in image_rows)
        for index, row in enumerate(image_rows, start=1):
            cost = _num(row.get("cost_usd"))
            if cost is not None:
                running += cost
            prompt = row.get("input_tokens", row.get("prompt_tokens", "unavailable"))
            completion = row.get("output_tokens", row.get("completion_tokens", "unavailable"))
            image_lines.append(f"{index}. Input: {prompt} | Output: {completion} | {format_usd(cost, image_missing)}")
        if image_cost is None and image_known and image_rows:
            image_cost = round(running, 8)
            image_lines[0] = f"IMAGE — {len(image_rows)} {'call' if len(image_rows) == 1 else 'calls'} — {format_usd(image_cost, image_missing)}"
    else:
        image_lines = ["IMAGE — none — $0.0000"]
        image_cost = 0.0

    known = [cost for cost in (content_cost, orchestration_cost, image_cost) if cost is not None]
    total = round(sum(known), 8) if known else None
    lines = content_lines + [""] + orchestration_lines + [""] + image_lines + [
        "",
        f"TOTAL COST — {format_usd(total, None)}",
    ]
    return "\n".join(lines)
