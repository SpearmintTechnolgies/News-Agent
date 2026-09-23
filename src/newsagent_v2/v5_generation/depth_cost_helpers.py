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
# Depth bands (output words): 600+ normal; 500-599 fallback; 400-499 exceptional; <400 hard block.
NORMAL_PASS_MIN = 600
STANDARD_FALLBACK_MIN = 500
ABSOLUTE_HARD_MIN = NORMAL_PASS_MIN  # alias: normal generation-ready threshold
FALLBACK_FLOOR = 400
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
    from newsagent_v2.article.writer.v4.article_cost_telemetry import (
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
        if inp is not None:
            input_tokens += inp
        if img is not None:
            image_tokens += img
        if tot is not None:
            total_tokens += tot
        cost, missing = compute_vertex_receipt_cost_usd(receipt, environ)
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


def critical_codes(qa: dict[str, Any] | None) -> list[str]:
    qa = qa or {}
    codes: list[str] = []
    for item in qa.get("critical_failures") or []:
        if isinstance(item, dict) and item.get("code"):
            codes.append(str(item["code"]))
    return codes


def apply_depth_fallback(
    *,
    article: dict[str, Any],
    qa: dict[str, Any] | None,
    word_count: int,
    recovery_history: list[dict[str, Any]] | None,
    recovery_succeeded: bool,
) -> dict[str, Any]:
    """Return depth decision metadata and possibly rewritten QA.

    Bands:
      >=600 normal pass
      500-599 fallback after bounded recovery
      400-499 exceptional fallback after recovery exhausted
      <400 hard block
    Does not pad article body.
    """
    history = list(recovery_history or [])
    recovery_exhausted = bool(history) and not recovery_succeeded
    result: dict[str, Any] = {
        "depth_status": DEPTH_FAIL,
        "final_word_count": word_count,
        "recovery_exhausted": recovery_exhausted,
        "suppressed_critical_code": None,
        "qa": qa,
        "ok": False,
        "exceptional_fallback": False,
    }
    if word_count >= NORMAL_PASS_MIN:
        codes = critical_codes(qa)
        if not codes:
            result["depth_status"] = DEPTH_NORMAL_PASS
            result["ok"] = True
        else:
            result["depth_status"] = DEPTH_FAIL
            result["ok"] = False
        return result

    if word_count < FALLBACK_FLOOR:
        result["depth_status"] = DEPTH_FAIL
        result["ok"] = False
        result["reason"] = "below_hard_block_floor"
        return result

    # 400-599: require exhausted recovery; 400-499 marked exceptional
    if not recovery_exhausted:
        result["depth_status"] = DEPTH_FAIL
        result["ok"] = False
        result["reason"] = "recovery_not_exhausted"
        return result
    if word_count < STANDARD_FALLBACK_MIN:
        result["exceptional_fallback"] = True

    qa = dict(qa or {})
    failures = [dict(item) for item in (qa.get("critical_failures") or []) if isinstance(item, dict)]
    other = [item for item in failures if str(item.get("code") or "") != SUPPRESSIBLE_DEPTH_CODE]
    length_hits = [item for item in failures if str(item.get("code") or "") == SUPPRESSIBLE_DEPTH_CODE]
    if not length_hits:
        # no length critical to suppress; still blocked if other criticals or soft fail
        result["ok"] = False
        result["depth_status"] = DEPTH_FAIL
        return result
    if other:
        result["ok"] = False
        result["depth_status"] = DEPTH_FAIL
        result["remaining_critical_codes"] = [str(i.get("code")) for i in other]
        return result

    # Suppress ONLY below_article_minimum_length and recompute QA flags
    from newsagent_v2.article.qa.result import build_qa_result, SEVERITY_CRITICAL, SEVERITY_WARNING

    warnings = [dict(item) for item in (qa.get("warnings") or []) if isinstance(item, dict)]
    # keep suppressed code as an informational warning for audit
    warnings.append(
        {
            "code": SUPPRESSIBLE_DEPTH_CODE,
            "message": f"suppressed for {DEPTH_FALLBACK_PASS}: {word_count} words after exhausted bounded recovery",
            "severity": SEVERITY_WARNING,
            "module": "depth",
        }
    )
    metrics = dict(qa.get("metrics") or {})
    metrics["depth_status"] = DEPTH_FALLBACK_PASS
    metrics["depth_fallback_word_count"] = word_count
    metrics["depth_fallback_recovery_exhausted"] = True
    metrics["depth_fallback_suppressed_critical"] = SUPPRESSIBLE_DEPTH_CODE
    new_qa = build_qa_result(
        event_id=qa.get("event_id"),
        issues=other + warnings,
        metrics=metrics,
    )
    # build_qa_result treats warnings by severity — ensure suppressed not critical
    new_qa["critical_failures"] = [
        item for item in new_qa.get("critical_failures") or []
        if str(item.get("code") or "") != SUPPRESSIBLE_DEPTH_CODE
    ]
    new_qa["critical_count"] = len(new_qa["critical_failures"])
    publishable = new_qa["critical_count"] == 0
    new_qa["qa_passed"] = publishable
    new_qa["publishable"] = publishable
    new_qa["qa_publishable"] = publishable

    result.update(
        {
            "depth_status": DEPTH_FALLBACK_PASS,
            "ok": publishable,
            "suppressed_critical_code": SUPPRESSIBLE_DEPTH_CODE,
            "qa": new_qa,
            "recovery_exhausted": True,
        }
    )
    return result


def build_review_cost_text(
    *,
    text_usage: dict[str, Any],
    image_usage: dict[str, Any],
    environ: dict[str, str] | None,
    escape_html,
) -> str:
    text_provider = text_usage.get("provider") or "kimi"
    text_model = text_usage.get("model") or "configured model"
    text_cost, text_missing = compute_kimi_cost_usd(text_usage, environ)
    image_cost = _num(image_usage.get("accumulated_cost_usd") or image_usage.get("provider_reported_cost_usd"))
    image_missing = list(image_usage.get("missing_pricing_keys") or [])
    if image_cost is None:
        image_cost, image_missing = compute_vertex_receipt_cost_usd(
            {
                "provider_name": image_usage.get("provider"),
                "model_name": image_usage.get("model"),
                "provider_reported_usage": image_usage.get("provider_reported_usage"),
                "provider_reported_cost_usd": image_usage.get("provider_reported_cost_usd"),
            },
            environ,
        )
    text_in = text_usage.get("prompt_tokens", text_usage.get("input_tokens", "unavailable"))
    text_out = text_usage.get("completion_tokens", text_usage.get("output_tokens", "unavailable"))
    text_tot = text_usage.get("total_tokens", "unavailable")
    usage = image_usage.get("provider_reported_usage") if isinstance(image_usage.get("provider_reported_usage"), dict) else {}
    img_in, img_out, img_tot = _image_modality_tokens(usage)
    image_provider = image_usage.get("provider") or "vertex"
    image_model = image_usage.get("model") or configured_vertex_model(environ)
    total = None
    if text_cost is not None and image_cost is not None:
        total = round(float(text_cost) + float(image_cost), 8)
    elif text_cost is not None and not image_usage.get("requests"):
        total = float(text_cost)
    lines = [
        f"TEXT — {escape_html(str(text_provider))}/{escape_html(str(text_model))}",
        f"Input: {text_in} | Output: {text_out} | Total: {text_tot}",
        f"Cost: {format_usd(text_cost, text_missing)}",
        f"IMAGE — {escape_html(str(image_provider))}/{escape_html(str(image_model))}",
        f"Input: {img_in if img_in is not None else 'unavailable'} | Image/Output: {img_out if img_out is not None else 'unavailable'} | Total: {img_tot if img_tot is not None else 'unavailable'}",
        f"Cost: {format_usd(image_cost, image_missing)}",
        f"TOTAL COST: {format_usd(total, (text_missing or []) + (image_missing or []) if total is None else None)}",
    ]
    return "\n".join(lines)
