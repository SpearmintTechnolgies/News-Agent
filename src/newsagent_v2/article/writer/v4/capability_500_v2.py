"""Isolated V4 500-word capability test V2 — editorial development plan.

Reuses frozen event-011 FactBank. No fresh research inflation.
Does NOT modify production defaults or event-030.
NO image / Telegram / WordPress.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.result import build_qa_result
from newsagent_v2.article.qa.textutil import split_paragraphs, word_count, words
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.packet import (
    AuthorizedFact,
    AuthorizedQuote,
    WriterEvidencePacket,
)
from newsagent_v2.article.writer.v4.provider import (
    ENV_ALLOW_KIMI,
    ENV_ALLOW_PAID_QWEN,
    ENV_MAX_PROVIDER_ATTEMPTS,
    ENV_MODEL,
    ENV_PROVIDER,
    PROVIDER_KIMI,
    V4_MAX_COMPLETION_TOKENS,
)
from newsagent_v2.article.writer.v4.repair import run_targeted_repairs
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_JSON_SCHEMA,
    V4_KIMI_MODEL,
    V4NativeArticle,
    V4NaturalProseWriter,
    V4WriterResult,
    _generation_packet_dict,
    assert_v4_writer_is_free,
    build_v4_writer,
    parse_v4_native,
)
from newsagent_v2.control.__main__ import _load_environ
from newsagent_v2.control.make_recovery import MAKE_RUNS_ROOT

REPO = Path(__file__).resolve().parents[5]
PROTECTED_EVENT_030_HASH = "66a948580b2d1f5bce2518061a83c6130358437f6c3b4a329597185bbb22213a"
PROTECTED_EVENT_030_PATH = (
    REPO / "output" / "approval" / "v4-20260917T071905Z" / "stories" / "event-030.json"
)
# Known-good RICH FactBank from prior live architecture run (not regenerated).
EVENT_011_PACKET_PATH = (
    REPO
    / "output"
    / "make_runs"
    / "v4-20260917T064316Z"
    / "attempts"
    / "event-011"
    / "evidence_packet.json"
)
EVENT_011_ATTEMPT_PATH = EVENT_011_PACKET_PATH.parent / "attempt.json"

TARGET_MIN = 450
TARGET_MAX = 550
PREFER_MIN = 480
PREFER_MAX = 520
# Comfortable OUTPUT budget for ~550 body words + headline/dek/SEO JSON.
# Production V4_MAX_COMPLETION_TOKENS remains unchanged (reported separately).
CAPABILITY_V2_MAX_TOKENS = 2200

V2_SYSTEM_PROMPT = """You are a professional crypto/news journalist for CoinNetwork.
You are writing a complete professional news article, NOT a summary.

Your previous tendency is excessive compression.
Develop the supplied verified material into approximately 500 BODY words.
Preferred length: 480–520.
Acceptable capability range: 450–550.

Follow the supplied editorial paragraph plan.
Use complete developed paragraphs.
Do not summarize several distinct facts into a single compressed sentence when they can naturally be explained separately.
Use natural attribution and transitions.
Vary sentence length and structure.
Give verified context adequate explanation.
Do not end early merely because every fact has been mentioned once.
A fact may be DEVELOPED linguistically without introducing new factual claims.

However, DO NOT invent:
facts, numbers, dates, causality, market reaction, importance, motives,
predictions, historical context, comparisons, quotes, or implications
unless explicitly supported by the FactBank.

RULES:
- use only authorized facts
- preserve numbers exactly
- preserve attribution and modality/uncertainty
- paraphrase independently
- quotes only from authorized quotes, reproduced exactly
OUTPUT: exactly one JSON object with keys:
headline, dek, article_body, seo_title, meta_description, slug
Do NOT include fact_ids_used, relationships, paragraph plans, or proof metadata.
""".strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def verify_event_030_untouched() -> dict[str, Any]:
    if not PROTECTED_EVENT_030_PATH.exists():
        return {"present": False, "ok": True, "note": "protected path absent"}
    story = json.loads(PROTECTED_EVENT_030_PATH.read_text(encoding="utf-8"))
    article = story.get("article") if isinstance(story.get("article"), dict) else {}
    body = str(article.get("article_body") or "")
    body_hash = _sha256_text(body)
    store_hash = str(story.get("article_sha256") or "")
    ok = body_hash == PROTECTED_EVENT_030_HASH and (
        not store_hash or store_hash == PROTECTED_EVENT_030_HASH
    )
    return {
        "present": True,
        "ok": ok,
        "body_words": word_count(body),
        "body_hash": body_hash,
        "expected": PROTECTED_EVENT_030_HASH,
    }


def load_event_011_packet() -> tuple[WriterEvidencePacket, dict[str, Any]]:
    raw = json.loads(EVENT_011_PACKET_PATH.read_text(encoding="utf-8"))
    attempt = {}
    if EVENT_011_ATTEMPT_PATH.exists():
        attempt = json.loads(EVENT_011_ATTEMPT_PATH.read_text(encoding="utf-8"))
    facts = tuple(
        AuthorizedFact(
            id=str(row.get("id") or f"P{i:02d}"),
            proposition=str(row.get("proposition") or "").strip(),
            attribution=str(row.get("attribution") or ""),
            numbers=tuple(row.get("numbers") or ()),
            polarity=str(row.get("polarity") or "affirmed"),
            modal=str(row.get("modal") or row.get("modality") or ""),
            modality=str(row.get("modality") or row.get("modal") or ""),
            provenance=tuple(row.get("provenance") or ()),
            subject=str(row.get("subject") or ""),
            predicate=str(row.get("predicate") or ""),
            object=str(row.get("object") or ""),
            status=str(row.get("status") or ""),
            time=str(row.get("time") or ""),
            location=str(row.get("location") or ""),
            entities=tuple(row.get("entities") or ()),
        )
        for i, row in enumerate(raw.get("authorized_facts") or [], start=1)
        if isinstance(row, dict) and str(row.get("proposition") or "").strip()
    )
    quotes = tuple(
        AuthorizedQuote(
            id=str(row.get("id") or f"Q{i:02d}"),
            exact_quote=str(row.get("exact_quote") or row.get("text") or ""),
            speaker=str(row.get("speaker") or ""),
            provenance=str(row.get("provenance") or ""),
        )
        for i, row in enumerate(raw.get("authorized_quotes") or [], start=1)
        if isinstance(row, dict)
    )
    packet = WriterEvidencePacket(
        event_id=str(raw.get("event_id") or "event-011"),
        story_topic=str(raw.get("story_topic") or ""),
        authorized_facts=facts,
        authorized_quotes=quotes,
        authorized_entities=tuple(raw.get("authorized_entities") or ()),
        source_context=dict(raw.get("source_context") or {}),
        forbidden=tuple(raw.get("forbidden") or ()),
    )
    meta = {
        "sources_retrieved": (attempt.get("research") or {}).get("sources_retrieved")
        or attempt.get("independent_sources")
        or 2,
        "independent_sources": attempt.get("independent_sources") or 2,
        "raw_research_words": (attempt.get("research") or {}).get("raw_research_words") or 593,
        "unique_propositions": attempt.get("unique_propositions") or len(facts),
        "evidence_capacity": attempt.get("evidence_capacity") or "RICH",
        "packet_path": str(EVENT_011_PACKET_PATH),
    }
    return packet, meta


def ledgers_from_packet(packet: WriterEvidencePacket) -> EvidenceLedgers:
    claims = tuple(
        LedgerClaim(
            claim_id=fact.id,
            text=fact.proposition,
            claim_type="fact",
            evidence_ids=fact.provenance or ("reused-event-011",),
        )
        for fact in packet.authorized_facts
    )
    return EvidenceLedgers(event_id=packet.event_id, claims=claims, quotes=())


def article_input_from_packet(packet: WriterEvidencePacket) -> dict[str, Any]:
    """Minimal pack for QA. No raw source prose fed to the writer."""
    evidence = []
    for fact in packet.authorized_facts:
        evidence.append(
            {
                "id": fact.id,
                "title": packet.story_topic,
                "source_name": "FactBank",
                "extracted_text": fact.proposition,
                "url": f"factbank://{packet.event_id}/{fact.id}",
            }
        )
    return {
        "event_id": packet.event_id,
        "representative_title": packet.story_topic,
        "evidence": evidence,
        "browse": False,
        "fetch_fulltext": False,
    }


def build_editorial_plan(packet: WriterEvidencePacket) -> dict[str, Any]:
    """Soft paragraph development plan. Not a V3 compiler schema."""
    facts = list(packet.authorized_facts)
    n = len(facts)
    # Distribute facts across 5 soft paragraphs.
    cuts = [0]
    for frac in (0.18, 0.40, 0.58, 0.80, 1.0):
        cuts.append(max(cuts[-1] + 1, min(n, int(round(n * frac)))))
    cuts[-1] = n
    buckets = [facts[cuts[i] : cuts[i + 1]] for i in range(5)]
    roles = [
        (
            "LEAD",
            "70-90",
            "What happened, who is involved, essential development.",
        ),
        (
            "CORE DEVELOPMENT",
            "100-120",
            "Develop the principal facts, numbers, timing, scope and attribution.",
        ),
        (
            "VERIFIED CONTEXT",
            "90-110",
            "Explain relevant background/context that exists in the FactBank.",
        ),
        (
            "SECONDARY DEVELOPMENT",
            "100-120",
            "Develop additional authorized details, operational details, participants, timeline.",
        ),
        (
            "CLOSING DEVELOPMENT",
            "70-90",
            "Remaining verified facts, next steps, regulatory/process/timeline where available.",
        ),
    ]
    paragraphs = []
    for idx, ((role, budget, guidance), chunk) in enumerate(zip(roles, buckets), start=1):
        paragraphs.append(
            {
                "paragraph": idx,
                "role": role,
                "soft_word_budget": budget,
                "guidance": guidance,
                "assigned_proposition_ids": [f.id for f in chunk],
                "assigned_propositions": [f.proposition for f in chunk],
            }
        )
    return {
        "plan_type": "lightweight_editorial_development",
        "target_total_words": f"{PREFER_MIN}-{PREFER_MAX}",
        "acceptable_range": f"{TARGET_MIN}-{TARGET_MAX}",
        "note": (
            "Soft paragraph budgets only. Do not invent unsupported material "
            "to satisfy a budget. Linguistic/editorial development is allowed; "
            "factual expansion is forbidden."
        ),
        "paragraphs": paragraphs,
    }


def proposition_usage(
    packet: WriterEvidencePacket, body: str
) -> tuple[list[str], list[str]]:
    body_l = (body or "").lower()
    body_tokens = set(words(body_l))
    used: list[str] = []
    unused: list[str] = []
    for fact in packet.authorized_facts:
        prop_tokens = [t for t in words(str(fact.proposition or "").lower()) if len(t) > 3]
        hit = sum(1 for token in prop_tokens if token in body_tokens)
        ratio = hit / max(1, len(prop_tokens))
        fragment = " ".join(prop_tokens[:8])
        contiguous = bool(fragment) and fragment in body_l
        numbers_ok = all(
            str(n).lower() in body_l for n in (fact.numbers or []) if str(n).strip()
        )
        if ratio >= 0.45 or contiguous or (fact.numbers and numbers_ok and ratio >= 0.3):
            used.append(fact.id)
        else:
            unused.append(fact.id)
    return used, unused


def paragraph_word_counts(body: str) -> list[dict[str, Any]]:
    paras = [p for p in split_paragraphs(body or "") if p.strip()]
    if len(paras) <= 1:
        # Fall back to sentence-group heuristic if model returned single block.
        from newsagent_v2.article.qa.textutil import split_sentences

        sentences = [s for s in split_sentences(body or "") if s.strip()]
        if not sentences:
            return []
        # Chunk roughly into 5 groups for inspection.
        size = max(1, (len(sentences) + 4) // 5)
        paras = []
        for i in range(0, len(sentences), size):
            paras.append(" ".join(sentences[i : i + size]))
    return [
        {"index": i + 1, "words": word_count(p), "preview": p[:120]}
        for i, p in enumerate(paras)
    ]


def underdeveloped_paragraphs(
    plan: dict[str, Any], counts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    floors = {1: 55, 2: 80, 3: 70, 4: 80, 5: 55}
    out = []
    for row in counts:
        idx = int(row.get("index") or 0)
        floor = floors.get(idx, 60)
        if int(row.get("words") or 0) < floor:
            plan_row = next(
                (p for p in plan.get("paragraphs") or [] if p.get("paragraph") == idx),
                {},
            )
            out.append(
                {
                    "paragraph": idx,
                    "words": row.get("words"),
                    "soft_floor": floor,
                    "role": plan_row.get("role"),
                    "assigned_proposition_ids": plan_row.get("assigned_proposition_ids") or [],
                }
            )
    return out


def build_v2_messages(
    packet: WriterEvidencePacket,
    plan: dict[str, Any],
    *,
    development: dict[str, Any] | None = None,
    current_article: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    evidence = _generation_packet_dict(packet)
    if development and current_article:
        system = (
            V2_SYSTEM_PROMPT
            + "\nYou are performing a TARGETED DEVELOPMENT pass on underdeveloped paragraphs only."
            "\nPreserve all existing supported facts. Expand using FactBank semantics only."
            "\nDo not invent facts. Target final article 480–520 BODY words."
        )
        user = {
            "instruction": (
                "Develop ONLY the underdeveloped paragraphs identified below. "
                "Keep all other supported content. Use FactBank and the editorial plan. "
                "Linguistic/editorial development only — no new factual claims. "
                f"Final BODY target {PREFER_MIN}–{PREFER_MAX} (acceptable {TARGET_MIN}–{TARGET_MAX})."
            ),
            "evidence_packet": evidence,
            "editorial_plan": plan,
            "underdeveloped_paragraphs": development.get("underdeveloped") or [],
            "current_article_semantic_fields": {
                "headline": current_article.get("headline"),
                "dek": current_article.get("dek"),
                "article_body": current_article.get("article_body"),
                "seo_title": current_article.get("seo_title"),
                "meta_description": current_article.get("meta_description"),
                "slug": current_article.get("slug"),
            },
            "output_schema": list(V4_JSON_SCHEMA["required"]),
            "body_word_target": {
                "min": TARGET_MIN,
                "prefer_min": PREFER_MIN,
                "prefer_max": PREFER_MAX,
                "max": TARGET_MAX,
            },
        }
    else:
        system = V2_SYSTEM_PROMPT
        user = {
            "instruction": (
                "Write a complete professional news article, NOT a summary. "
                "Follow the editorial paragraph plan. "
                "Develop verified material to approximately 500 BODY words "
                f"(preferred {PREFER_MIN}–{PREFER_MAX}; acceptable {TARGET_MIN}–{TARGET_MAX}). "
                "Do not compress distinct facts into one short sentence when they can be explained separately. "
                "Do not end early merely because every fact was mentioned once."
            ),
            "evidence_packet": evidence,
            "editorial_plan": plan,
            "EvidenceCapacity": "RICH",
            "article_type": "FULL_ARTICLE",
            "output_schema": list(V4_JSON_SCHEMA["required"]),
            "body_word_target": {
                "min": TARGET_MIN,
                "prefer_min": PREFER_MIN,
                "prefer_max": PREFER_MAX,
                "max": TARGET_MAX,
            },
        }
    banned = {
        "extracted_text",
        "source_article_body",
        "source_sentences",
        "previous_article",
        "matched_source_fragment",
        "offending_sentence",
    }
    assert not (banned & set(evidence.keys()))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


class Capability500V2Writer(V4NaturalProseWriter):
    renderer_name = "v4_capability_500_v2_writer"

    def render_with_plan(
        self,
        packet: WriterEvidencePacket,
        plan: dict[str, Any],
        *,
        development: dict[str, Any] | None = None,
        current_article: dict[str, Any] | None = None,
    ) -> V4WriterResult:
        if self.generation_calls >= self.max_calls:
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error="call budget exceeded",
                provider=self.provider,
                model=self.model,
            )
            self.last_result = result
            return result
        if not self._configured():
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error="credentials missing",
                provider=self.provider,
                model=self.model,
            )
            self.last_result = result
            return result
        messages = build_v2_messages(
            packet,
            plan,
            development=development,
            current_article=current_article,
        )
        body_extra = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "v4_capability_500_v2",
                    "strict": True,
                    "schema": V4_JSON_SCHEMA,
                },
            }
        }
        self.generation_calls += 1
        started = perf_counter()
        response = self.transport.complete(
            messages=messages,
            body_extra=body_extra,
            max_completion_tokens=CAPABILITY_V2_MAX_TOKENS,
            temperature=0.35,
        )
        if (not response.ok) and (
            "json_schema" in str(response.error or "").lower()
            or "response_format" in str(response.error or "").lower()
            or "failed to generate json" in str(response.error or "").lower()
        ):
            soft = list(messages) + [
                {"role": "user", "content": "Return ONLY the JSON object. No markdown."}
            ]
            response = self.transport.complete(
                messages=soft,
                body_extra=None,
                max_completion_tokens=CAPABILITY_V2_MAX_TOKENS,
                temperature=0.35,
            )
        latency_ms = int((perf_counter() - started) * 1000)
        if not response.ok:
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error=str(response.error or "provider_error")[:500],
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                usage=dict(response.usage or {}),
                request_diagnostic={
                    "finish_reason": response.finish_reason,
                    "usage": dict(response.usage or {}),
                    "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
                    "capability_500_v2": True,
                },
                error_type=response.error_type,
            )
            self.last_result = result
            return result
        try:
            content = response.content
            if content is None and isinstance(response.payload, dict):
                from newsagent_v2.providers.groq_editorial import parse_message_content

                content = parse_message_content(response.payload)
            native = parse_v4_native(content)
            if not native.slug:
                native.slug = re.sub(r"[^a-z0-9]+", "-", native.headline.lower()).strip("-")[:80]
            if not native.seo_title:
                native.seo_title = native.headline[:70]
            if not native.meta_description:
                native.meta_description = (native.dek or native.article_body)[:155]
            result = V4WriterResult(
                ok=True,
                native=native,
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
                usage=dict(response.usage or {}),
                request_diagnostic={
                    "finish_reason": response.finish_reason,
                    "usage": dict(response.usage or {}),
                    "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
                    "capability_500_v2": True,
                },
            )
            self.last_result = result
            return result
        except Exception as exc:  # noqa: BLE001
            result = V4WriterResult(
                ok=False,
                invalid_output=True,
                error=str(exc)[:400],
                provider=self.provider,
                model=self.model,
                latency_ms=latency_ms,
            )
            self.last_result = result
            return result


def run_capability_500_v2(*, environ: dict[str, str]) -> dict[str, Any]:
    pre = verify_event_030_untouched()
    if pre.get("present") and not pre.get("ok"):
        return {
            "status": "FAIL",
            "reason": "protected_event_030_hash_mismatch_before_test",
            "event_030": pre,
        }

    env = dict(environ)
    env[ENV_ALLOW_KIMI] = "true"
    env[ENV_ALLOW_PAID_QWEN] = "0"
    env[ENV_PROVIDER] = PROVIDER_KIMI
    env[ENV_MODEL] = V4_KIMI_MODEL
    env[ENV_MAX_PROVIDER_ATTEMPTS] = "1"
    for key, value in list(env.items()):
        if key.startswith("NEWSAGENT") or key in {"ARTICLE_MIN_WORDS"}:
            os.environ[key] = str(value)
    assert_v4_writer_is_free(V4_KIMI_MODEL, environ=env)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = MAKE_RUNS_ROOT.parent / "capability_tests" / f"v4_500word_v2_{stamp}"
    out_root.mkdir(parents=True, exist_ok=True)

    packet, meta = load_event_011_packet()
    if len(packet.authorized_facts) < 10:
        return {
            "status": "FAIL",
            "reason": "event_011_factbank_too_thin_or_missing",
            "meta": meta,
        }

    plan = build_editorial_plan(packet)
    (out_root / "editorial_plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_root / "evidence_packet.json").write_text(
        json.dumps(packet.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    base = build_v4_writer(environ=env, enable_failover=False, max_calls=4)
    writer = Capability500V2Writer(
        transport=base.transport,
        api_key=getattr(base, "api_key", None),
        environ=env,
        max_calls=4,
        timeout_seconds=240,
    )
    ledgers = ledgers_from_packet(packet)
    pack = article_input_from_packet(packet)

    # --- ONE initial generation ---
    first = writer.render_with_plan(packet, plan)
    if not first.ok or first.native is None:
        report = {
            "status": "FAIL",
            "reason": f"writer_error:{first.error}",
            "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
            "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
            "event": packet.event_id,
            "meta": meta,
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
            "500_word_capability_proven": False,
            "event030_preserved": verify_event_030_untouched().get("ok"),
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    native = first.native
    native_words = word_count(native.article_body)
    finish_reason = (first.request_diagnostic or {}).get("finish_reason")
    completion_tokens = (first.usage or {}).get("completion_tokens")
    counts = paragraph_word_counts(native.article_body)
    used, unused = proposition_usage(packet, native.article_body)
    targeted_development_used = False

    working = deepcopy(native)

    if native_words < TARGET_MIN:
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
        # ONE targeted development pass — not a full clean-room regen.
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

        final_words = word_count(working.article_body)
        if final_words < TARGET_MIN:
            report = {
                "status": "V4_500_WORD_CAPABILITY_FAILED",
                "event": packet.event_id,
                "evidence_capacity": meta.get("evidence_capacity"),
                "sources_retrieved": meta.get("sources_retrieved"),
                "unique_propositions": meta.get("unique_propositions"),
                "provider": writer.provider,
                "model": writer.model,
                "editorial_plan_used": True,
                "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
                "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
                "finish_reason": finish_reason,
                "completion_tokens": completion_tokens,
                "native_body_words": native_words,
                "targeted_development_used": targeted_development_used,
                "final_body_words": final_words,
                "paragraph_word_counts": paragraph_word_counts(working.article_body),
                "propositions_available": len(packet.authorized_facts),
                "propositions_used": proposition_usage(packet, working.article_body)[0],
                "unused_relevant_propositions": proposition_usage(packet, working.article_body)[1],
                "under_450_inspection": inspection,
                "500_word_capability_proven": False,
                "event030_preserved": verify_event_030_untouched().get("ok"),
                "Groq_calls": 0,
                "copyright_provider_calls": 0,
                "out_root": str(out_root),
            }
            (out_root / "failed_native.json").write_text(
                json.dumps(working.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            (out_root / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            return report

    final_words = word_count(working.article_body)
    if final_words > TARGET_MAX:
        # Soft trim by trailing sentences only.
        from newsagent_v2.article.qa.textutil import split_sentences

        kept = [s for s in split_sentences(working.article_body) if s.strip()]
        while len(kept) > 2 and word_count(" ".join(kept)) > TARGET_MAX:
            kept.pop()
        working = deepcopy(working)
        working.article_body = " ".join(kept).strip()
        final_words = word_count(working.article_body)

    if not (TARGET_MIN <= final_words <= TARGET_MAX):
        report = {
            "status": "V4_500_WORD_CAPABILITY_FAILED",
            "reason": f"final_body_words={final_words} outside {TARGET_MIN}-{TARGET_MAX}",
            "native_body_words": native_words,
            "final_body_words": final_words,
            "targeted_development_used": targeted_development_used,
            "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "500_word_capability_proven": False,
            "event030_preserved": verify_event_030_untouched().get("ok"),
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    # Length gate passed — factual repair + QA (copyright still skipped).
    repaired, report2, repair_log = run_targeted_repairs(
        working,
        packet=packet,
        ledgers=ledgers,
        article_input=pack,
        writer=writer,
        min_words=TARGET_MIN,
        allow_destructive_length_pad=False,
    )
    # If repair collapsed length below band, fail capability (no third writer attempt).
    if word_count(repaired.article_body) < TARGET_MIN:
        report = {
            "status": "V4_500_WORD_CAPABILITY_FAILED",
            "reason": "post_repair_below_450",
            "final_body_words": word_count(repaired.article_body),
            "native_body_words": native_words,
            "targeted_development_used": targeted_development_used,
            "500_word_capability_proven": False,
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
    if not (TARGET_MIN <= final_words <= TARGET_MAX):
        crit = list(qa.get("critical_failures") or []) + list(qa.get("warnings") or [])
        crit.append(
            {
                "code": "capability_500_out_of_band",
                "message": f"body_words={final_words}",
                "severity": "critical",
                "module": "depth",
            }
        )
        qa = build_qa_result(
            event_id=packet.event_id,
            issues=crit,
            metrics=dict(qa.get("metrics") or {}),
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
    coherence = (
        "PASS"
        if TARGET_MIN <= final_words <= TARGET_MAX and grounding_ok and critical_count == 0
        else "FAIL"
    )
    proven = bool(
        TARGET_MIN <= final_words <= TARGET_MAX
        and grounding_ok
        and quote_ok
        and mech_ok
        and sec_ok
        and critical_count == 0
        and coherence == "PASS"
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
                "test": "v4_500_word_capability_v2",
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
        "provider": writer.provider,
        "model": writer.model,
        "editorial_plan_used": True,
        "max_completion_tokens": CAPABILITY_V2_MAX_TOKENS,
        "production_V4_MAX_COMPLETION_TOKENS": V4_MAX_COMPLETION_TOKENS,
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "native_body_words": native_words,
        "targeted_development_used": targeted_development_used,
        "final_body_words": final_words,
        "paragraph_word_counts": paragraph_word_counts(final_body),
        "propositions_available": len(packet.authorized_facts),
        "propositions_used": used_f,
        "unused_relevant_propositions": unused_f,
        "grounding_supported": report2.supported,
        "grounding_ambiguous": report2.ambiguous,
        "grounding_unsupported": report2.unsupported,
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
        "500_word_capability_proven": proven,
        "event030_preserved": bool(post.get("ok")),
        "event_030_check": post,
        "Groq_calls": 0,
        "copyright_provider_calls": 0,
        "kimi_calls": int(getattr(writer, "generation_calls", 0) or 0),
        "artifact_dir": str(artifact),
        "fresh_research_performed": False,
        "factbank_source": str(EVENT_011_PACKET_PATH),
        "repair_log": repair_log.as_dict(),
    }
    (out_root / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    load_dotenv(REPO / ".env")
    result = run_capability_500_v2(environ=_load_environ())
    # Print full report including complete article for manual inspection.
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result.get("status") == "PASS":
        return 0
    if result.get("status") == "V4_500_WORD_CAPABILITY_FAILED":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
