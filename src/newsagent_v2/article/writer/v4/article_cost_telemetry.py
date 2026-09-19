"""
Kimi article-generation cost telemetry — REPORTING ONLY.

Does not change writer behavior, prompts, models, or call counts.
Does not invent pricing. Unverified rates → monetary fields = UNVERIFIED.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

UNVERIFIED = "UNVERIFIED"

ENV_PRICE_VERIFIED = "NEWSAGENT_V2_KIMI_PRICING_VERIFIED"
ENV_INPUT_USD_PER_MTOK = "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK"
ENV_OUTPUT_USD_PER_MTOK = "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK"
ENV_USD_TO_INR = "NEWSAGENT_V2_USD_TO_INR"
ENV_PRICING_SOURCE = "NEWSAGENT_V2_KIMI_PRICING_SOURCE"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def extract_token_usage(usage: dict[str, Any] | None) -> dict[str, int | None]:
    """Normalize provider usage dict to input/output/total token fields."""
    data = usage if isinstance(usage, dict) else {}
    inp = _as_int(data.get("prompt_tokens"))
    if inp is None:
        inp = _as_int(data.get("input_tokens"))
    out = _as_int(data.get("completion_tokens"))
    if out is None:
        out = _as_int(data.get("output_tokens"))
    total = _as_int(data.get("total_tokens"))
    if total is None and inp is not None and out is not None:
        total = inp + out
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": total,
    }


def resolve_verified_kimi_pricing(
    environ: dict[str, str] | None = None,
    *,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Pricing is verified only when:
    1) provider usage includes an explicit billed USD amount, or
    2) trusted env marks pricing verified with both input/output USD-per-MTok rates.
    Never falls back to guessed list prices.
    """
    env = environ or {}
    usage = usage if isinstance(usage, dict) else {}

    billed = None
    for key in (
        "billed_cost_usd",
        "provider_reported_cost_usd",
        "cost_usd",
        "total_cost_usd",
    ):
        raw = usage.get(key)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            billed = float(raw)
            break

    if billed is not None:
        fx = _parse_float(env.get(ENV_USD_TO_INR))
        inr = round(billed * fx, 6) if fx is not None else UNVERIFIED
        return {
            "pricing_verified": True,
            "pricing_source": "provider_reported_billed_usd",
            "input_usd_per_mtok": None,
            "output_usd_per_mtok": None,
            "usd_to_inr": fx,
            "billed_cost_usd": billed,
            "billed_cost_inr": inr,
        }

    verified_flag = str(env.get(ENV_PRICE_VERIFIED) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    inp_rate = _parse_float(env.get(ENV_INPUT_USD_PER_MTOK))
    out_rate = _parse_float(env.get(ENV_OUTPUT_USD_PER_MTOK))
    fx = _parse_float(env.get(ENV_USD_TO_INR))
    source = str(env.get(ENV_PRICING_SOURCE) or "").strip() or "trusted_env_config"

    if verified_flag and inp_rate is not None and out_rate is not None:
        return {
            "pricing_verified": True,
            "pricing_source": source,
            "input_usd_per_mtok": inp_rate,
            "output_usd_per_mtok": out_rate,
            "usd_to_inr": fx,
            "billed_cost_usd": None,
            "billed_cost_inr": None,
        }

    return {
        "pricing_verified": False,
        "pricing_source": "unverified",
        "input_usd_per_mtok": None,
        "output_usd_per_mtok": None,
        "usd_to_inr": None,
        "billed_cost_usd": None,
        "billed_cost_inr": None,
    }


def _parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compute_cost_from_tokens(
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    pricing: dict[str, Any],
) -> dict[str, Any]:
    if not pricing.get("pricing_verified"):
        return {
            "article_generation_cost_usd": UNVERIFIED,
            "article_generation_cost_inr": UNVERIFIED,
            "pricing_verified": False,
            "pricing_source": pricing.get("pricing_source") or "unverified",
        }

    if pricing.get("billed_cost_usd") is not None:
        usd = float(pricing["billed_cost_usd"])
        inr = pricing.get("billed_cost_inr")
        return {
            "article_generation_cost_usd": usd,
            "article_generation_cost_inr": inr if inr is not None else UNVERIFIED,
            "pricing_verified": True,
            "pricing_source": pricing.get("pricing_source"),
        }

    inp_rate = pricing.get("input_usd_per_mtok")
    out_rate = pricing.get("output_usd_per_mtok")
    if inp_rate is None or out_rate is None:
        return {
            "article_generation_cost_usd": UNVERIFIED,
            "article_generation_cost_inr": UNVERIFIED,
            "pricing_verified": False,
            "pricing_source": "unverified",
        }
    if input_tokens is None or output_tokens is None:
        return {
            "article_generation_cost_usd": UNVERIFIED,
            "article_generation_cost_inr": UNVERIFIED,
            "pricing_verified": True,
            "pricing_source": pricing.get("pricing_source"),
            "note": "pricing_verified_but_token_counts_missing",
        }

    usd = (input_tokens / 1_000_000.0) * float(inp_rate) + (
        output_tokens / 1_000_000.0
    ) * float(out_rate)
    usd = round(usd, 8)
    fx = pricing.get("usd_to_inr")
    inr: Any = round(usd * float(fx), 8) if isinstance(fx, (int, float)) else UNVERIFIED
    return {
        "article_generation_cost_usd": usd,
        "article_generation_cost_inr": inr,
        "pricing_verified": True,
        "pricing_source": pricing.get("pricing_source"),
    }


def infer_call_type(diagnostic: dict[str, Any] | None, attempt: dict[str, Any] | None = None) -> str:
    diag = diagnostic if isinstance(diagnostic, dict) else {}
    att = attempt if isinstance(attempt, dict) else {}
    if diag.get("capability_500_v2") or diag.get("capability_500_v3"):
        return "rich_initial"
    repair = att.get("repair_log") if isinstance(att.get("repair_log"), dict) else {}
    if int(repair.get("model_calls") or 0) > 0:
        return "repair"
    if int(att.get("Kimi_regenerations") or 0) > 0:
        return "regeneration"
    return "initial"


def build_article_cost_record(
    *,
    batch_id: str,
    event_id: str,
    article: dict[str, Any] | None = None,
    attempt: dict[str, Any] | None = None,
    diagnostic: dict[str, Any] | None = None,
    publishable: bool | None = None,
    environ: dict[str, str] | None = None,
    call_type: str | None = None,
) -> dict[str, Any]:
    article = article if isinstance(article, dict) else {}
    attempt = attempt if isinstance(attempt, dict) else {}
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    usage_raw = diagnostic.get("usage") if isinstance(diagnostic.get("usage"), dict) else {}
    if not usage_raw and isinstance(attempt.get("usage"), dict):
        usage_raw = attempt["usage"]

    tokens = extract_token_usage(usage_raw)
    body = str(article.get("article_body") or "")
    article_hash = str(
        attempt.get("canonical_body_hash")
        or article.get("canonical_body_hash")
        or (_sha256_text(body) if body else "")
        or ""
    )
    depth = attempt.get("depth") if isinstance(attempt.get("depth"), dict) else {}
    article_type = (
        attempt.get("article_type")
        or depth.get("article_type")
        or article.get("article_type")
    )
    body_words = (
        attempt.get("final_word_count")
        or attempt.get("final_body_words")
        or article.get("body_words")
    )
    if body_words is None and body:
        body_words = len(body.split())

    pricing = resolve_verified_kimi_pricing(environ, usage=usage_raw)
    costs = compute_cost_from_tokens(
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        pricing=pricing,
    )

    ok = publishable
    if ok is None:
        ok = bool(attempt.get("ok")) if "ok" in attempt else None

    return {
        "batch_id": batch_id,
        "event_id": event_id,
        "headline": article.get("headline") or attempt.get("headline"),
        "article_hash": article_hash or None,
        "article_type": article_type,
        "body_words": body_words,
        "call_type": call_type or infer_call_type(diagnostic, attempt),
        "kimi_input_tokens": tokens["input_tokens"],
        "kimi_output_tokens": tokens["output_tokens"],
        "kimi_total_tokens": tokens["total_tokens"],
        "input_tokens": tokens["input_tokens"],
        "output_tokens": tokens["output_tokens"],
        "total_tokens": tokens["total_tokens"],
        "article_generation_cost_usd": costs["article_generation_cost_usd"],
        "article_generation_cost_inr": costs["article_generation_cost_inr"],
        "cost_usd": costs["article_generation_cost_usd"],
        "cost_inr": costs["article_generation_cost_inr"],
        "pricing_verified": bool(costs["pricing_verified"]),
        "pricing_source": costs.get("pricing_source"),
        "publishable": ok,
        "telemetry_only": True,
    }


def _sum_tokens(rows: list[dict[str, Any]], field: str) -> int:
    total = 0
    for row in rows:
        val = row.get(field)
        if isinstance(val, int):
            total += val
    return total


def _money_sum(rows: list[dict[str, Any]], field: str, *, verified: bool) -> Any:
    if not verified:
        return UNVERIFIED
    total = 0.0
    for row in rows:
        val = row.get(field)
        if val == UNVERIFIED or val is None:
            return UNVERIFIED
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            total += float(val)
        else:
            return UNVERIFIED
    return round(total, 8)


def build_batch_cost_report(
    *,
    batch_id: str,
    article_rows: list[dict[str, Any]],
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    rows = list(article_rows)
    pricing = resolve_verified_kimi_pricing(environ)
    verified = bool(pricing.get("pricing_verified")) and all(
        bool(r.get("pricing_verified")) for r in rows
    ) and all(
        r.get("article_generation_cost_usd") != UNVERIFIED for r in rows
    )

    publishable = [r for r in rows if r.get("publishable") is True]
    failed = [r for r in rows if r.get("publishable") is not True]

    kimi_in = _sum_tokens(rows, "kimi_input_tokens")
    kimi_out = _sum_tokens(rows, "kimi_output_tokens")
    kimi_total = _sum_tokens(rows, "kimi_total_tokens")
    if kimi_total == 0 and (kimi_in or kimi_out):
        kimi_total = kimi_in + kimi_out

    succ_tokens = _sum_tokens(publishable, "kimi_total_tokens")
    fail_tokens = _sum_tokens(failed, "kimi_total_tokens")

    succ_usd = _money_sum(publishable, "article_generation_cost_usd", verified=verified)
    succ_inr = _money_sum(publishable, "article_generation_cost_inr", verified=verified)
    fail_usd = _money_sum(failed, "article_generation_cost_usd", verified=verified)
    fail_inr = _money_sum(failed, "article_generation_cost_inr", verified=verified)
    total_usd = _money_sum(rows, "article_generation_cost_usd", verified=verified)
    total_inr = _money_sum(rows, "article_generation_cost_inr", verified=verified)

    n_pub = len(publishable)
    if not verified:
        eff_usd: Any = UNVERIFIED
        eff_inr: Any = UNVERIFIED
    elif n_pub == 0:
        eff_usd = None
        eff_inr = None
    elif isinstance(total_usd, (int, float)) and isinstance(total_inr, (int, float)):
        # Effective cost per publishable = total batch article-gen cost / publishable count
        # (includes failed-candidate spend amortized).
        eff_usd = round(float(total_usd) / n_pub, 8)
        eff_inr = round(float(total_inr) / n_pub, 8)
    else:
        eff_usd = UNVERIFIED
        eff_inr = UNVERIFIED

    return {
        "batch_id": batch_id,
        "articles_generated": len(rows),
        "publishable_articles": n_pub,
        "kimi_calls": len(rows),  # one aggregated diagnostic record per candidate attempt
        "kimi_input_tokens": kimi_in,
        "kimi_output_tokens": kimi_out,
        "kimi_total_tokens": kimi_total,
        "successful_article_tokens": succ_tokens,
        "failed_candidate_tokens": fail_tokens,
        "successful_article_cost_usd": succ_usd if verified else UNVERIFIED,
        "successful_article_cost_inr": succ_inr if verified else UNVERIFIED,
        "failed_candidate_cost_usd": fail_usd if verified else UNVERIFIED,
        "failed_candidate_cost_inr": fail_inr if verified else UNVERIFIED,
        "total_article_generation_cost_usd": total_usd if verified else UNVERIFIED,
        "total_article_generation_cost_inr": total_inr if verified else UNVERIFIED,
        "effective_cost_per_publishable_article_usd": eff_usd if verified else UNVERIFIED,
        "effective_cost_per_publishable_article_inr": eff_inr if verified else UNVERIFIED,
        "pricing_verified": verified,
        "pricing_source": pricing.get("pricing_source") if verified else "unverified",
        "articles": rows,
        "telemetry_only": True,
    }


def load_attempt_cost_from_disk(
    attempt_dir: Path,
    *,
    batch_id: str,
    environ: dict[str, str] | None = None,
    publishable_event_ids: set[str] | None = None,
) -> dict[str, Any] | None:
    attempt_dir = Path(attempt_dir)
    if not attempt_dir.is_dir():
        return None
    event_id = attempt_dir.name
    attempt: dict[str, Any] = {}
    diagnostic: dict[str, Any] = {}
    article: dict[str, Any] = {}
    att_path = attempt_dir / "attempt.json"
    diag_path = attempt_dir / "writer_request_diagnostic.json"
    art_path = attempt_dir / "article.json"
    if att_path.is_file():
        try:
            attempt = json.loads(att_path.read_text(encoding="utf-8"))
        except Exception:
            attempt = {}
    if diag_path.is_file():
        try:
            diagnostic = json.loads(diag_path.read_text(encoding="utf-8"))
        except Exception:
            diagnostic = {}
    if art_path.is_file():
        try:
            article = json.loads(art_path.read_text(encoding="utf-8"))
        except Exception:
            article = {}
    if not diagnostic and not attempt:
        return None

    publishable = None
    if publishable_event_ids is not None:
        publishable = event_id in publishable_event_ids
    elif "ok" in attempt:
        publishable = bool(attempt.get("ok"))

    return build_article_cost_record(
        batch_id=batch_id,
        event_id=event_id,
        article=article,
        attempt=attempt,
        diagnostic=diagnostic,
        publishable=publishable,
        environ=environ,
    )


def report_batch_from_attempts_root(
    attempts_root: Path,
    *,
    batch_id: str,
    environ: dict[str, str] | None = None,
    publishable_event_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    root = Path(attempts_root)
    rows: list[dict[str, Any]] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            row = load_attempt_cost_from_disk(
                child,
                batch_id=batch_id,
                environ=environ,
                publishable_event_ids=set(publishable_event_ids)
                if publishable_event_ids is not None
                else None,
            )
            if row is not None:
                rows.append(row)
    return build_batch_cost_report(batch_id=batch_id, article_rows=rows, environ=environ)


def format_article_cost_block(row: dict[str, Any]) -> str:
    return (
        "ARTICLE COST\n"
        f"event={row.get('event_id')}\n"
        f"headline={row.get('headline')}\n"
        f"body_words={row.get('body_words')}\n"
        f"input_tokens={row.get('input_tokens')}\n"
        f"output_tokens={row.get('output_tokens')}\n"
        f"total_tokens={row.get('total_tokens')}\n"
        f"cost_usd={row.get('cost_usd')}\n"
        f"cost_inr={row.get('cost_inr')}\n"
        f"pricing_verified={row.get('pricing_verified')}"
    )


def persist_article_cost_json(attempt_dir: Path, record: dict[str, Any]) -> Path:
    path = Path(attempt_dir) / "article_cost.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return path
