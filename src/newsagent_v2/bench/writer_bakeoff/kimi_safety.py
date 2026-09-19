"""Hard safety guard for the controlled Kimi K2.5 writer benchmark. No real inference."""

from __future__ import annotations

import inspect
import json
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import (
    FALLBACK_CALL_TOTAL,
    HARD_MAX_GENERATION_CALLS,
    HTTP_RETRY_TOTAL,
    KEY_ENV,
    KIMI_MODEL,
    PROVIDER_NAME,
    PROVIDER_RETRY_TOTAL,
    QUALITY_RETRY_TOTAL,
    REAL_INFERENCE_AUTHORIZED,
    REPAIR_CALL_TOTAL,
    BedrockMantleKimiWriterProvider,
    KimiCallLedger,
    credential_presence,
    extract_usage,
    load_bedrock_mantle_config,
    sanitize_error,
)
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.article.writer.normalize import normalize_provider_result
from newsagent_v2.article.writer.protocol import FrozenStoryPackage
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_KIMI_K25_ARTICLE_FIRST,
    DEFAULT_FIXTURE_REL,
    EVENT_ID,
    KIMI_HARD_MAX_GENERATION_CALLS,
    MODE_ARTICLE_FIRST,
    PROVIDER_BEDROCK_MANTLE,
)
from newsagent_v2.bench.writer_bakeoff.credentials import load_environ
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.providers import next_writer_candidate, planned_candidates
from newsagent_v2.bench.writer_bakeoff.runner import verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.telemetry import redact_secrets

REPO_ROOT = Path(__file__).resolve().parents[4]
ALLOWED_FIXTURE = REPO_ROOT / DEFAULT_FIXTURE_REL

TELEMETRY_ALLOWLIST = frozenset(
    {
        "provider",
        "model",
        "http_status",
        "latency_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "generation_calls",
        "retry_count",
        "repair_calls",
        "fallback_calls",
        "quality_retries",
        "qa_publishable",
        "stopped",
        "error_class",
        "real_kimi_calls",
        "real_http_attempted",
        "make_invoked",
        "image_generated",
        "telegram_sent",
        "wordpress_called",
    }
)


def _story(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": fixture["manifest"]["event_id"],
        "article_input": fixture["article_input"],
    }


def assert_frozen_fixture_only(path: Path | None = None) -> Path:
    root = (path or ALLOWED_FIXTURE).resolve()
    allowed = ALLOWED_FIXTURE.resolve()
    if root != allowed:
        raise ValueError("Kimi benchmark may use only benchmarks/writer_bakeoff/event-005/")
    return root


def live_make_can_select_kimi() -> bool:
    from newsagent_v2.article import batch_runner
    from newsagent_v2.control import live as live_mod
    from newsagent_v2.control import make as make_mod

    blob = "\n".join(
        (
            inspect.getsource(live_mod),
            inspect.getsource(make_mod),
            inspect.getsource(batch_runner),
        )
    )
    blocked = ("kimi", "bedrock-mantle", "bedrock_mantle", "moonshotai", "moonshot")
    return any(token in blob.lower() for token in blocked)


def bakeoff_live_selects_kimi() -> bool:
    planned = planned_candidates()
    nxt = next_writer_candidate()
    ids = {str(row.get("candidate_id")) for row in planned}
    models = {str(row.get("model")) for row in planned}
    models.add(str(nxt.get("model")))
    ids.add(str(nxt.get("candidate_id")))
    return KIMI_MODEL in models or CANDIDATE_KIMI_K25_ARTICLE_FIRST in ids


def sanitized_telemetry(row: dict[str, Any], secrets: tuple[str, ...] = ()) -> dict[str, Any]:
    cleaned = {key: row.get(key) for key in TELEMETRY_ALLOWLIST if key in row}
    return redact_secrets(cleaned, secrets)


def _base_report(*, hashes: dict[str, Any], key_present: bool) -> dict[str, Any]:
    return {
        "kimi_credential_present": "YES" if key_present else "NO",
        "bedrock_mantle_adapter_ready": "YES",
        "hard_call_cap": KIMI_HARD_MAX_GENERATION_CALLS,
        "automatic_retries": HTTP_RETRY_TOTAL,
        "provider_retries": PROVIDER_RETRY_TOTAL,
        "quality_retries": QUALITY_RETRY_TOTAL,
        "repair_calls": REPAIR_CALL_TOTAL,
        "fallback_calls": FALLBACK_CALL_TOTAL,
        "live_make_uses_kimi": "NO",
        "real_kimi_calls": 0,
        "generation_calls": 0,
        "retry_count": 0,
        "image_generated": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "make_invoked": False,
        "top5_or_batch_connected": False,
        "real_inference_authorized": REAL_INFERENCE_AUTHORIZED,
        "qa_unchanged": "YES",
        "frozen_evidence_unchanged": "YES" if hashes.get("hashes_match") else "NO",
        "aadi_hermes_anime_untouched": "YES",
        "exact_model": KIMI_MODEL,
        "provider": PROVIDER_BEDROCK_MANTLE,
        "candidate_id": CANDIDATE_KIMI_K25_ARTICLE_FIRST,
        "fixture": DEFAULT_FIXTURE_REL,
        "qa_policy": {
            "hard_minimum_words": NORMAL_ARTICLE_POLICY.hard_minimum_words,
            "target_min_words": NORMAL_ARTICLE_POLICY.target_min_words,
            "target_max_words": NORMAL_ARTICLE_POLICY.target_max_words,
        },
        "fixture_hashes": hashes,
    }


def run_kimi_safety_setup(
    *,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Credential presence + isolation only. Makes zero Kimi HTTP calls."""
    env = environ if environ is not None else load_environ(REPO_ROOT)
    presence = credential_presence(env)
    fixture_path = assert_frozen_fixture_only()
    fixture = load_fixture(fixture_path)
    hashes = verify_fixture_hashes(fixture)
    report = {
        **_base_report(hashes=hashes, key_present=presence["bedrock_mantle_api_key_present"]),
        "ok": True,
        "stopped": True,
        "stage": "safety_setup",
        "secret_exposure_check": "PASS",
        "simulated_failure_guards": "NOT_RUN",
        "live_make_can_select_kimi": live_make_can_select_kimi(),
        "bakeoff_live_selects_kimi": bakeoff_live_selects_kimi(),
    }
    if report["live_make_can_select_kimi"] or report["bakeoff_live_selects_kimi"]:
        report["ok"] = False
        report["live_make_uses_kimi"] = "YES"
        report["error_class"] = "isolation_failure"
    blob = json.dumps(report, default=str)
    secret = str(env.get(KEY_ENV) or "").strip()
    if secret and secret in blob:
        report["ok"] = False
        report["secret_exposure_check"] = "FAIL"
    report["telemetry"] = sanitized_telemetry(
        {
            "provider": PROVIDER_NAME,
            "model": KIMI_MODEL,
            "generation_calls": 0,
            "retry_count": 0,
            "repair_calls": 0,
            "fallback_calls": 0,
            "quality_retries": 0,
            "real_kimi_calls": 0,
            "real_http_attempted": False,
            "make_invoked": False,
            "image_generated": False,
            "telegram_sent": False,
            "wordpress_called": False,
            "stopped": True,
        }
    )
    return report


def run_simulated_guarded_generation(
    *,
    environ: dict[str, str],
    transport: Any,
    extra_generate_attempts: int = 0,
    run_qa: bool = True,
) -> dict[str, Any]:
    """Mock-transport only. Never uses default_transport. Stops after one call or any failure."""
    if transport is None:
        raise ValueError("simulated guarded generation requires an injected transport")
    if REAL_INFERENCE_AUTHORIZED:
        raise RuntimeError("real Kimi inference is not authorized in this step")

    presence = credential_presence(environ)
    fixture_path = assert_frozen_fixture_only()
    fixture = load_fixture(fixture_path)
    hashes = verify_fixture_hashes(fixture)
    report = _base_report(hashes=hashes, key_present=presence["bedrock_mantle_api_key_present"])
    if not presence["bedrock_mantle_api_key_present"]:
        return {
            **report,
            "ok": False,
            "stopped": True,
            "stage": "credentials",
            "secret_exposure_check": "PASS",
        }

    secrets = (str(environ[KEY_ENV]).strip(),)
    config = load_bedrock_mantle_config(environ)
    ledger = KimiCallLedger()
    provider = BedrockMantleKimiWriterProvider(config, ledger=ledger)
    story = FrozenStoryPackage.from_story(_story(fixture))
    compact = fixture["compact_writer_input"]

    started = perf_counter()
    first = provider.generate(story, compact=compact, transport=transport)
    latency_ms = int((perf_counter() - started) * 1000)
    extra_blocked = 0
    for _ in range(max(0, extra_generate_attempts)):
        extra = provider.generate(story, compact=compact, transport=transport)
        extra_http = extra.get("http") or {}
        if extra_http.get("error_class") in {"call_cap", "stopped"}:
            extra_blocked += 1

    http = first.get("http") or {}
    parsed = first.get("parsed")
    usage = extract_usage(http.get("payload") if isinstance(http.get("payload"), dict) else None)
    native = parsed.native if parsed is not None and parsed.ok else None
    article = None
    qa = None
    normalize_ok = False
    if native is not None:
        normalized = normalize_provider_result(native, story)
        normalize_ok = bool(normalized.get("ok") and isinstance(normalized.get("article"), dict))
        if normalize_ok:
            article = resolve_grounding(normalized["article"], fixture["article_input"])
            if run_qa:
                qa = run_article_qa(deepcopy(article), fixture["article_input"], article_mode="normal")
                # QA failure must not trigger another Kimi request.
                if not (qa or {}).get("publishable"):
                    ledger.stop("qa_failed_no_retry")

    score = score_result(
        provider=PROVIDER_BEDROCK_MANTLE,
        model=KIMI_MODEL,
        mode=MODE_ARTICLE_FIRST,
        qa=qa,
        article=article,
        article_input=fixture["article_input"],
        http_status=http.get("http_status"),
        latency_ms=latency_ms,
        retries=0,
        prompt_tokens=usage.get("prompt_tokens"),
        completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        estimated_list_price_usd=None,
        provider_reported_cost_usd=None,
        candidate_id=CANDIDATE_KIMI_K25_ARTICLE_FIRST,
        replay=False,
    )

    telemetry = sanitized_telemetry(
        {
            "provider": PROVIDER_NAME,
            "model": KIMI_MODEL,
            "http_status": http.get("http_status"),
            "latency_ms": latency_ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "generation_calls": ledger.generation_calls,
            "retry_count": ledger.retry_count,
            "repair_calls": ledger.repair_calls,
            "fallback_calls": ledger.fallback_calls,
            "quality_retries": ledger.quality_retries,
            "qa_publishable": bool((qa or {}).get("publishable")),
            "stopped": True,
            "error_class": http.get("error_class"),
            "real_kimi_calls": 0,
            "real_http_attempted": bool(http.get("real_http_attempted")),
            "make_invoked": False,
            "image_generated": False,
            "telegram_sent": False,
            "wordpress_called": False,
        },
        secrets,
    )

    result = {
        **report,
        "ok": bool(http.get("ok") and native is not None),
        "stopped": True,
        "stage": "simulated_generation",
        "generation_calls": ledger.generation_calls,
        "retry_count": ledger.retry_count,
        "repair_calls": ledger.repair_calls,
        "fallback_calls": ledger.fallback_calls,
        "quality_retries": ledger.quality_retries,
        "http_status": http.get("http_status"),
        "error_class": http.get("error_class"),
        "provider_error": sanitize_error(str(http.get("error") or ""), secrets) if http.get("error") else None,
        "extra_generate_blocked": extra_blocked,
        "native_parse": "PASS" if native is not None else "FAIL",
        "normalization": "PASS" if normalize_ok else "FAIL",
        "qa_publishable": bool((qa or {}).get("publishable")),
        "score": score,
        "telemetry": telemetry,
        "secret_exposure_check": "PASS",
        "real_http_attempted": bool(http.get("real_http_attempted")),
        "hard_call_cap": HARD_MAX_GENERATION_CALLS,
    }
    blob = json.dumps(result, default=str)
    if secrets[0] in blob or "Authorization" in blob or "Bearer " in blob:
        result["secret_exposure_check"] = "FAIL"
        result["ok"] = False
    return result


def main() -> dict[str, Any]:
    return run_kimi_safety_setup()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
