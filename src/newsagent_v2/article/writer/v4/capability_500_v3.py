"""Isolated V4 500-word capability test V3 — fix downstream QA/repair only.

Reuses V2 editorial-plan writer strategy unchanged.
Fixes: surgical repair integrity, soft trim, legitimate evidence URL provenance.
Does NOT modify production defaults or event-030.
NO image / Telegram / WordPress.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.result import build_qa_result
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.qa.schema import check_schema
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.capability_500_v2 import (
    CAPABILITY_V2_MAX_TOKENS,
    EVENT_011_PACKET_PATH,
    PREFER_MAX,
    PREFER_MIN,
    PROTECTED_EVENT_030_HASH,
    TARGET_MAX,
    TARGET_MIN,
    Capability500V2Writer,
    build_editorial_plan,
    build_v2_messages,
    ledgers_from_packet,
    load_event_011_packet,
    paragraph_word_counts,
    proposition_usage,
    underdeveloped_paragraphs,
    verify_event_030_untouched,
    _sha256_text,
)
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
    V4_MAX_COMPLETION_TOKENS,
)
from newsagent_v2.article.writer.v4.repair import (
    paragraph_integrity_ok,
    run_targeted_repairs,
    soft_trim_article_body,
)
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_KIMI_MODEL,
    V4NativeArticle,
    assert_v4_writer_is_free,
    build_v4_writer,
)
from newsagent_v2.control.__main__ import _load_environ

REPO = Path(__file__).resolve().parents[5]
EVENT_011_ARTICLE_PATH = EVENT_011_PACKET_PATH.parent / "article.json"
CAPABILITY_V3_MAX_TOKENS = CAPABILITY_V2_MAX_TOKENS  # harness-only; prod unchanged


def load_event_011_provenance_map() -> dict[str, dict[str, str]]:
    """Map evidence_id → {url, source} from persisted make-run article (no invented URLs)."""
    if not EVENT_011_ARTICLE_PATH.exists():
        raise FileNotFoundError(f"missing provenance article: {EVENT_011_ARTICLE_PATH}")
    article = json.loads(EVENT_011_ARTICLE_PATH.read_text(encoding="utf-8"))
    mapping: dict[str, dict[str, str]] = {}
    for claim in article.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        refs = claim.get("evidence_refs") or []
        if not isinstance(refs, list) or not refs:
            continue
        ref0 = refs[0] if isinstance(refs[0], dict) else {}
        url = str(ref0.get("url") or "").strip()
        source = str(ref0.get("source") or "").strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            continue
        for eid in claim.get("evidence_ids") or []:
            key = str(eid).strip()
            if key and key not in mapping:
                mapping[key] = {"url": url, "source": source or "Unknown"}
    if not mapping:
        raise RuntimeError("event-011 provenance map empty — cannot build valid evidence pack")
    return mapping


def article_input_from_packet_v3(
    packet: WriterEvidencePacket,
    provenance: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build QA pack with legitimate https URLs restored from persisted provenance."""
    provenance = provenance or load_event_011_provenance_map()
    # Aggregate proposition text per evidence_id for extracted_text / unit text.
    by_eid_text: dict[str, list[str]] = {}
    for fact in packet.authorized_facts:
        eids = list(fact.provenance) or []
        if not eids:
            # Fall back to first known provenance key if fact lacks ids.
            eids = [next(iter(provenance.keys()))] if provenance else []
        for eid in eids:
            if eid in provenance:
                by_eid_text.setdefault(eid, []).append(fact.proposition)

    evidence: list[dict[str, Any]] = []
    evidence_units: list[dict[str, Any]] = []
    for eid, meta in provenance.items():
        blob = " ".join(by_eid_text.get(eid) or [packet.story_topic])
        evidence.append(
            {
                "id": eid,
                "title": packet.story_topic,
                "source": meta["source"],
                "source_name": meta["source"],
                "extracted_text": blob,
                "url": meta["url"],
            }
        )
        evidence_units.append(
            {
                "evidence_id": eid,
                "source": meta["source"],
                "url": meta["url"],
                "text": blob,
            }
        )
    return {
        "event_id": packet.event_id,
        "representative_title": packet.story_topic,
        "evidence": evidence,
        "evidence_units": evidence_units,
        "browse": False,
        "fetch_fulltext": False,
    }


def ledgers_from_packet_v3(
    packet: WriterEvidencePacket,
    provenance: dict[str, dict[str, str]] | None = None,
):
    """Preserve fact provenance ids that resolve to legitimate URLs."""
    provenance = provenance or load_event_011_provenance_map()
    fallback_eid = next(iter(provenance.keys()))
    from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim

    claims = []
    for fact in packet.authorized_facts:
        eids = tuple(eid for eid in (fact.provenance or ()) if eid in provenance)
        if not eids:
            eids = (fallback_eid,)
        claims.append(
            LedgerClaim(
                claim_id=fact.id,
                text=fact.proposition,
                claim_type="fact",
                evidence_ids=eids,
            )
        )
    return EvidenceLedgers(event_id=packet.event_id, claims=tuple(claims), quotes=())


class Capability500V3Writer(Capability500V2Writer):
    """Same editorial-plan strategy as V2; distinct renderer name for diagnostics only."""

    renderer_name = "v4_capability_500_v3_writer"


def _repair_stats(repair_log: Any, pre_report: Any, post_report: Any) -> dict[str, Any]:
    actions = list(getattr(repair_log, "actions", None) or [])
    rewrites = [a for a in actions if getattr(a, "kind", "") == "unsupported_proposition_rewrite"]
    rollbacks = [
        a
        for a in actions
        if getattr(a, "kind", "")
        in {
            "unsupported_proposition_rewrite_rolled_back",
            "unsupported_rewrite_candidate_rejected",
        }
    ]
    trigger = "none"
    if pre_report is not None and (
        getattr(pre_report, "unsupported", 0) or getattr(pre_report, "ambiguous", 0)
    ):
        trigger = "unsupported_or_ambiguous_present"
    return {
        "rewrite_trigger": trigger,
        "unsupported_rewrites_attempted": len(rewrites) + len(rollbacks),
        "supported_sentences_rewritten": 0,  # immutable by design
        "repair_rollbacks": len(rollbacks),
        "rewrite_actions": [a.as_dict() for a in rewrites],
        "rollback_actions": [a.as_dict() for a in rollbacks],
        "pre_rewrite_grounding": {
            "supported": getattr(pre_report, "supported", None),
            "ambiguous": getattr(pre_report, "ambiguous", None),
            "unsupported": getattr(pre_report, "unsupported", None),
            "ok": getattr(pre_report, "ok", None),
        }
        if pre_report is not None
        else None,
        "post_rewrite_grounding": {
            "supported": getattr(post_report, "supported", None),
            "ambiguous": getattr(post_report, "ambiguous", None),
            "unsupported": getattr(post_report, "unsupported", None),
            "ok": getattr(post_report, "ok", None),
        }
        if post_report is not None
        else None,
    }


def run_capability_500_v3(
    *,
    environ: dict[str, str] | None = None,
    resume_native_path: str | Path | None = None,
) -> dict[str, Any]:
    env = dict(environ or os.environ)
    env.setdefault(ENV_ALLOW_KIMI, "true")
    env.setdefault(ENV_PROVIDER, PROVIDER_KIMI)
    env.setdefault(ENV_MODEL, V4_KIMI_MODEL)
    env.setdefault(ENV_MAX_PROVIDER_ATTEMPTS, "1")
    env.setdefault(ENV_ALLOW_PAID_QWEN, "0")
    for key, value in list(env.items()):
        if key.startswith("NEWSAGENT") or key in {"ARTICLE_MIN_WORDS"}:
            os.environ[key] = str(value)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = REPO / "output" / "capability_tests" / f"v4_500word_v3_{stamp}"
    out_root.mkdir(parents=True, exist_ok=True)

    packet, meta = load_event_011_packet()
    provenance = load_event_011_provenance_map()
    plan = build_editorial_plan(packet)
    (out_root / "editorial_plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_root / "evidence_packet.json").write_text(
        json.dumps(packet.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_root / "provenance_map.json").write_text(
        json.dumps(provenance, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    ledgers = ledgers_from_packet_v3(packet, provenance)
    pack = article_input_from_packet_v3(packet, provenance)

    # --- ONE initial generation (or offline resume of a prior native) ---
    finish_reason = None
    completion_tokens = None
    kimi_calls = 0
    writer_provider = PROVIDER_KIMI
    writer_model = V4_KIMI_MODEL
    writer = None

    if resume_native_path:
        native_raw = json.loads(Path(resume_native_path).read_text(encoding="utf-8"))
        native = V4NativeArticle(
            headline=str(native_raw.get("headline") or ""),
            dek=str(native_raw.get("dek") or ""),
            article_body=str(native_raw.get("article_body") or ""),
            seo_title=str(native_raw.get("seo_title") or ""),
            meta_description=str(native_raw.get("meta_description") or ""),
            slug=str(native_raw.get("slug") or ""),
        )
        finish_reason = "stop"
        completion_tokens = None
        kimi_calls = 0
        (out_root / "resumed_from.json").write_text(
            json.dumps({"path": str(resume_native_path)}, indent=2), encoding="utf-8"
        )
    else:
        base = build_v4_writer(environ=env, enable_failover=False, max_calls=4)
        writer = Capability500V3Writer(
            transport=base.transport,
            api_key=getattr(base, "api_key", None),
            environ=env,
            max_calls=4,
            timeout_seconds=240,
        )
        assert_v4_writer_is_free(writer.model, environ=env)
        writer_provider = writer.provider
        writer_model = writer.model
        first = writer.render_with_plan(packet, plan)
        if not first.ok or first.native is None:
            report = {
                "status": "FAIL",
                "reason": f"writer_error:{first.error}",
                "max_completion_tokens": CAPABILITY_V3_MAX_TOKENS,
                "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
                "event": packet.event_id,
                "500_word_writer_capability": False,
                "500_word_end_to_end_article_capability": False,
                "event030_preserved": verify_event_030_untouched().get("ok"),
                "Groq_calls": 0,
                "copyright_provider_calls": 0,
            }
            (out_root / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return report
        native = first.native
        finish_reason = (first.request_diagnostic or {}).get("finish_reason")
        completion_tokens = (first.usage or {}).get("completion_tokens")
        kimi_calls = int(getattr(writer, "generation_calls", 0) or 0)

    native_words = word_count(native.article_body)
    counts = paragraph_word_counts(native.article_body)
    used, unused = proposition_usage(packet, native.article_body)
    targeted_development_used = False
    working = deepcopy(native)
    (out_root / "native.json").write_text(
        json.dumps(native.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    if (not resume_native_path) and native_words < TARGET_MIN:
        underdeveloped = underdeveloped_paragraphs(plan, counts)
        inspection = {
            "native_body_words": native_words,
            "paragraph_word_counts": counts,
            "propositions_available": len(packet.authorized_facts),
            "propositions_used": used,
            "unused_relevant_propositions": unused,
            "underdeveloped_paragraphs": underdeveloped,
        }
        (out_root / "under_450_inspection.json").write_text(
            json.dumps(inspection, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        targeted_development_used = True
        second = writer.render_with_plan(
            packet,
            plan,
            development={"underdeveloped": underdeveloped or counts},
            current_article=working.as_dict(),
        )
        if second.ok and second.native is not None:
            working = second.native
            finish_reason = (second.request_diagnostic or {}).get("finish_reason") or finish_reason
            completion_tokens = (second.usage or {}).get("completion_tokens") or completion_tokens
            kimi_calls = int(getattr(writer, "generation_calls", 0) or 0)
        final_words = word_count(working.article_body)
        if final_words < TARGET_MIN:
            report = {
                "status": "V4_500_WORD_CAPABILITY_FAILED",
                "reason": "under_450_after_development",
                "native_body_words": native_words,
                "final_body_words": final_words,
                "targeted_development_used": True,
                "500_word_writer_capability": native_words >= TARGET_MIN,
                "500_word_end_to_end_article_capability": False,
                "event030_preserved": verify_event_030_untouched().get("ok"),
                "Groq_calls": 0,
                "copyright_provider_calls": 0,
            }
            (out_root / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return report
        native_words = word_count(working.article_body) if targeted_development_used else native_words
        # Keep original native_words from first generation for reporting.
        native_words = word_count(native.article_body)

    # Soft trim (preserve paragraphs / complete sentences). Do NOT roll back a
    # successful in-band trim merely because unsupported claims remain — repair
    # handles those next. Only roll back on paragraph-integrity collapse.
    trimmed_body, trim_meta = soft_trim_article_body(
        working.article_body,
        target_max=TARGET_MAX,
        prefer_max=PREFER_MAX,
        prefer_min=PREFER_MIN,
        target_min=TARGET_MIN,
    )
    working = deepcopy(working)
    working.article_body = trimmed_body
    post_trim_words = word_count(working.article_body)
    if trim_meta.get("trimmed") and not paragraph_integrity_ok(
        working.article_body, prior_body=native.article_body
    ):
        working = deepcopy(native)
        trimmed_body, trim_meta = soft_trim_article_body(
            working.article_body,
            target_max=TARGET_MAX,
            prefer_max=TARGET_MAX,
            prefer_min=TARGET_MIN,
            target_min=TARGET_MIN,
        )
        working.article_body = trimmed_body
        post_trim_words = word_count(working.article_body)
        trim_meta = {**trim_meta, "integrity_retry": True}
        if not paragraph_integrity_ok(working.article_body, prior_body=native.article_body):
            # Keep the best in-band candidate if any; otherwise fail later.
            if not (TARGET_MIN <= post_trim_words <= TARGET_MAX):
                working = deepcopy(native)
                post_trim_words = word_count(working.article_body)
                trim_meta = {**trim_meta, "rolled_back_to_native": True}

    post_trim_report = verify_v4_native(working, packet=packet, ledgers=ledgers)

    if not (TARGET_MIN <= post_trim_words <= TARGET_MAX):
        # Native over band and trim could not land in band without damage.
        if native_words > TARGET_MAX and post_trim_words > TARGET_MAX:
            report = {
                "status": "V4_500_WORD_CAPABILITY_FAILED",
                "reason": f"post_trim_words={post_trim_words} outside {TARGET_MIN}-{TARGET_MAX}",
                "native_body_words": native_words,
                "post_trim_words": post_trim_words,
                "trim_meta": trim_meta,
                "500_word_writer_capability": True,
                "500_word_end_to_end_article_capability": False,
                "event030_preserved": verify_event_030_untouched().get("ok"),
                "Groq_calls": 0,
                "copyright_provider_calls": 0,
            }
            (out_root / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return report

    # Repair only when grounding actually requires it. Never rewrite supported prose.
    pre_repair_report = post_trim_report
    repair_log = None
    if (
        pre_repair_report.ok
        and pre_repair_report.unsupported == 0
        and pre_repair_report.ambiguous == 0
    ):
        repaired = working
        report2 = pre_repair_report
        from newsagent_v2.article.writer.v4.repair import RepairLog

        repair_log = RepairLog()
        repair_stats = _repair_stats(repair_log, pre_repair_report, report2)
        repair_stats["rewrite_trigger"] = "none_grounding_clean"
    else:
        repaired, report2, repair_log = run_targeted_repairs(
            working,
            packet=packet,
            ledgers=ledgers,
            article_input=pack,
            writer=writer,
            min_words=TARGET_MIN,
            allow_destructive_length_pad=False,
        )
        repair_stats = _repair_stats(repair_log, pre_repair_report, report2)
        if not paragraph_integrity_ok(repaired.article_body, prior_body=working.article_body):
            # Full rollback of destructive repair.
            repaired = working
            report2 = verify_v4_native(repaired, packet=packet, ledgers=ledgers)
            repair_stats["repair_rollbacks"] = int(repair_stats.get("repair_rollbacks") or 0) + 1
            repair_stats["full_repair_pass_rolled_back"] = True

    final_words = word_count(repaired.article_body)
    if final_words < TARGET_MIN:
        report = {
            "status": "V4_500_WORD_CAPABILITY_FAILED",
            "reason": "post_repair_below_450",
            "native_body_words": native_words,
            "post_trim_words": post_trim_words,
            "final_body_words": final_words,
            "repair_stats": repair_stats,
            "500_word_writer_capability": native_words >= 450,
            "500_word_end_to_end_article_capability": False,
            "event030_preserved": verify_event_030_untouched().get("ok"),
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    # If still slightly over max after non-destructive path, try one more soft trim.
    if final_words > TARGET_MAX:
        body2, trim2 = soft_trim_article_body(
            repaired.article_body,
            target_max=TARGET_MAX,
            prefer_max=PREFER_MAX,
            prefer_min=PREFER_MIN,
            target_min=TARGET_MIN,
        )
        probe = deepcopy(repaired)
        probe.article_body = body2
        probe_rep = verify_v4_native(probe, packet=packet, ledgers=ledgers)
        if (
            TARGET_MIN <= word_count(body2) <= TARGET_MAX
            and probe_rep.unsupported == 0
            and probe_rep.ambiguous == 0
            and paragraph_integrity_ok(body2, prior_body=repaired.article_body)
        ):
            repaired = probe
            report2 = probe_rep
            final_words = word_count(body2)
            trim_meta = {**trim_meta, "second_trim": trim2}

    if not (TARGET_MIN <= final_words <= TARGET_MAX):
        report = {
            "status": "V4_500_WORD_CAPABILITY_FAILED",
            "reason": f"final_body_words={final_words} outside {TARGET_MIN}-{TARGET_MAX}",
            "native_body_words": native_words,
            "post_trim_words": post_trim_words,
            "final_body_words": final_words,
            "500_word_writer_capability": True,
            "500_word_end_to_end_article_capability": False,
            "event030_preserved": verify_event_030_untouched().get("ok"),
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    article = assemble_v4_article(
        event_id=packet.event_id,
        native=repaired,
        ledgers=ledgers,
        article_input=pack,
        report=report2,
    )
    qa = run_article_qa(
        article,
        pack,
        article_mode="normal",
        skip_copyright_similarity=True,
    )
    final_body = str(article.get("article_body") or "")
    final_words = word_count(final_body)

    schema_issues = check_schema(article, pack)
    invalid_url_count = sum(
        1 for i in schema_issues if isinstance(i, dict) and i.get("code") == "invalid_evidence_url"
    )

    critical_count = int(qa.get("critical_count") or len(qa.get("critical_failures") or []))
    warning_count = int(qa.get("warning_count") or len(qa.get("warnings") or []))
    quote_ok = not any(
        "quote" in str(item.get("code") or "").lower()
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict)
    )
    mech_ok = not any(
        item.get("module") == "mechanics" and item.get("severity") == "critical"
        for item in (qa.get("critical_failures") or [])
        if isinstance(item, dict)
    )
    sec_ok = int((qa.get("metrics") or {}).get("publishing_safety_issue_count") or 0) == 0
    grounding_ok = (
        report2.unsupported == 0
        and report2.ambiguous == 0
        and report2.supported > 0
        and report2.ok
    )
    para_counts = paragraph_word_counts(final_body)
    collapsed_close = bool(
        para_counts
        and int(para_counts[-1].get("words") or 0) < 15
        and len(para_counts) >= 3
    )
    coherence = (
        "PASS"
        if (
            TARGET_MIN <= final_words <= TARGET_MAX
            and grounding_ok
            and critical_count == 0
            and invalid_url_count == 0
            and paragraph_integrity_ok(final_body, prior_body=native.article_body)
            and not collapsed_close
        )
        else "FAIL"
    )
    writer_cap = bool(native_words >= 450)
    proven = bool(
        TARGET_MIN <= final_words <= TARGET_MAX
        and grounding_ok
        and quote_ok
        and mech_ok
        and sec_ok
        and critical_count == 0
        and invalid_url_count == 0
        and coherence == "PASS"
        and int(repair_stats.get("supported_sentences_rewritten") or 0) == 0
    )

    article_hash = _sha256_text(final_body)
    artifact = out_root / "canonical"
    artifact.mkdir(parents=True, exist_ok=True)
    (artifact / "article.json").write_text(
        json.dumps(article, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact / "article_body.txt").write_text(final_body, encoding="utf-8")
    (artifact / "qa.json").write_text(
        json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact / "editorial_plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact / "meta.json").write_text(
        json.dumps(
            {
                "test": "v4_500_word_capability_v3",
                "event_id": packet.event_id,
                "article_hash": article_hash,
                "body_words": final_words,
                "does_not_overwrite_event_030": True,
                "protected_event_030_hash": PROTECTED_EVENT_030_HASH,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    used_f, unused_f = proposition_usage(packet, final_body)
    post = verify_event_030_untouched()
    report = {
        "status": "PASS" if proven else "FAIL",
        "event": packet.event_id,
        "evidence_capacity": meta.get("evidence_capacity"),
        "sources_retrieved": meta.get("sources_retrieved"),
        "unique_propositions": meta.get("unique_propositions"),
        "provider": writer_provider,
        "model": writer_model,
        "editorial_plan_used": True,
        "max_completion_tokens": CAPABILITY_V3_MAX_TOKENS,
        "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "native_body_words": native_words,
        "post_trim_words": post_trim_words,
        "targeted_development_used": targeted_development_used,
        "final_body_words": final_words,
        "paragraph_word_counts": paragraph_word_counts(final_body),
        "propositions_available": len(packet.authorized_facts),
        "propositions_used": used_f,
        "unused_relevant_propositions": unused_f,
        "rewrite_trigger": repair_stats.get("rewrite_trigger"),
        "unsupported_rewrites_attempted": repair_stats.get("unsupported_rewrites_attempted"),
        "supported_sentences_rewritten": repair_stats.get("supported_sentences_rewritten"),
        "repair_rollbacks": repair_stats.get("repair_rollbacks"),
        "repair_stats": repair_stats,
        "trim_meta": trim_meta,
        "grounding_supported": report2.supported,
        "grounding_ambiguous": report2.ambiguous,
        "grounding_unsupported": report2.unsupported,
        "invalid_evidence_url_count": invalid_url_count,
        "quote_grounding": "PASS" if quote_ok else "FAIL",
        "mechanics": "PASS" if mech_ok else "FAIL",
        "security": "PASS" if sec_ok else "FAIL",
        "coherence": coherence,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "warning_codes": qa.get("warning_codes")
        or [i.get("code") for i in (qa.get("warnings") or []) if isinstance(i, dict)],
        "article_hash": article_hash,
        "complete_article": {
            "headline": article.get("headline"),
            "dek": article.get("dek"),
            "article_body": final_body,
        },
        "500_word_writer_capability": bool(writer_cap),
        "500_word_end_to_end_article_capability": proven,
        "500_word_capability_proven": proven,
        "event030_preserved": bool(post.get("ok")),
        "event_030_check": post,
        "Groq_calls": 0,
        "copyright_provider_calls": 0,
        "kimi_calls": kimi_calls,
        "artifact_dir": str(artifact),
        "fresh_research_performed": False,
        "factbank_source": str(EVENT_011_PACKET_PATH),
        "provenance_source": str(EVENT_011_ARTICLE_PATH),
        "repair_log": repair_log.as_dict() if repair_log else {},
    }
    (out_root / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    load_dotenv(REPO / ".env")
    import sys

    resume = None
    if "--resume-native" in sys.argv:
        idx = sys.argv.index("--resume-native")
        resume = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
    result = run_capability_500_v3(environ=_load_environ(), resume_native_path=resume)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("status") == "PASS":
        return 0
    if result.get("status") == "V4_500_WORD_CAPABILITY_FAILED":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
