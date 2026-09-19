"""Isolated V4 ~500-word writer capability test.

Does NOT modify production defaults permanently.
Does NOT touch frozen event-030 artifacts.
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
from urllib.parse import urlparse

from dotenv import load_dotenv

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.textutil import word_count, words
from newsagent_v2.article.writer.controlled.capacity import article_input_for_ledgers
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.evidence_depth import (
    ARTICLE_FULL,
    CAPACITY_RICH,
    DepthDecision,
    assess_evidence_capacity,
)
from newsagent_v2.article.writer.v4.event_research import research_event
from newsagent_v2.article.writer.v4.expand import (
    build_unused_evidence_set,
    generate_expansion_text,
    _filter_expansion_sentences,
)
from newsagent_v2.article.writer.v4.factbank import build_fact_bank, fact_bank_to_writer_packet
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
from newsagent_v2.batch.viability import cluster_to_story
from newsagent_v2.control.__main__ import _load_environ
from newsagent_v2.control.discover import discover_ranked_top5
from newsagent_v2.control.make_recovery import MAKE_RUNS_ROOT

REPO = Path(__file__).resolve().parents[5]
PROTECTED_EVENT_030_HASH = "66a948580b2d1f5bce2518061a83c6130358437f6c3b4a329597185bbb22213a"
PROTECTED_EVENT_030_PATH = (
    REPO / "output" / "approval" / "v4-20260917T071905Z" / "stories" / "event-030.json"
)

TARGET_MIN = 450
TARGET_MAX = 550
PREFER_MIN = 480
PREFER_MAX = 520
CAPABILITY_MAX_TOKENS = 1800
MAX_CANDIDATES = 5
MAX_FACTS = 40

CAPABILITY_SYSTEM_PROMPT = """You are a professional crypto/news journalist for CoinNetwork.
Write a developed newsroom ARTICLE, not a summary, abstract, alert, or brief.
Target approximately 500 BODY words.
Preferred 480–520 BODY words.
Acceptable 450–550 BODY words.
Use developed newsroom prose with varied sentence structure and natural transitions.
Fully realize the supplied authorized facts across multiple coherent paragraphs.
Explain verified context contained in the FactBank.
You may use stylistic and grammatical connective language that introduces no new factual proposition.
RULES:
- use only authorized facts
- preserve numbers exactly
- preserve attribution and modality/uncertainty
- do not invent motives, causality, predictions, market reaction, or background
- do not invent comparisons, importance claims, or unsupported temporal relationships
- paraphrase independently; avoid copying source phrasing
- quotes only from authorized quotes, reproduced exactly
- prefer complete developed sentences over telegraphic fragments
- do not mention evidence IDs, prompts, system, environment, or configuration
OUTPUT: exactly one JSON object with keys:
headline, dek, article_body, seo_title, meta_description, slug
Do NOT include fact_ids_used, relationships, paragraph plans, or proof metadata.
""".strip()


def _sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _sha256_payload(payload: Any) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(blob).hexdigest()


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


def _source_domains(pack: dict[str, Any]) -> list[str]:
    domains: list[str] = []
    for row in pack.get("evidence") or []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        host = urlparse(url).netloc.lower().removeprefix("www.") if url else ""
        if not host:
            host = str(row.get("source") or row.get("source_name") or "").strip().lower()
        if host and host not in domains:
            domains.append(host)
    return domains


def _proposition_usage(
    packet: WriterEvidencePacket, body: str
) -> tuple[list[str], list[str]]:
    body_l = (body or "").lower()
    body_tokens = set(words(body_l))
    used: list[str] = []
    unused: list[str] = []
    for fact in packet.authorized_facts:
        prop = str(fact.proposition or "")
        prop_tokens = [t for t in words(prop.lower()) if len(t) > 3]
        # Used if majority of distinctive tokens appear, or a long contiguous fragment.
        hit = 0
        for token in prop_tokens:
            if token in body_tokens:
                hit += 1
        ratio = hit / max(1, len(prop_tokens))
        numbers_ok = all(str(n).lower() in body_l for n in (fact.numbers or []) if str(n).strip())
        fragment = " ".join(prop_tokens[:8])
        contiguous = bool(fragment) and fragment in body_l
        if ratio >= 0.45 or contiguous or (fact.numbers and numbers_ok and ratio >= 0.3):
            used.append(fact.id)
        else:
            unused.append(fact.id)
    return used, unused


def _trim_to_max(body: str, max_words: int) -> str:
    """Deterministic trim from the end by sentence without inventing text."""
    from newsagent_v2.article.qa.textutil import split_sentences

    sentences = [s for s in split_sentences(body or "") if s.strip()]
    if word_count(body) <= max_words or not sentences:
        return (body or "").strip()
    kept = list(sentences)
    while len(kept) > 1 and word_count(" ".join(kept)) > max_words:
        kept.pop()
    return " ".join(kept).strip()


def _build_capability_messages(
    packet: WriterEvidencePacket, *, regeneration: bool
) -> list[dict[str, str]]:
    evidence = _generation_packet_dict(packet)
    if regeneration:
        instruction = (
            "Regenerate a COMPLETE fresh newsroom ARTICLE from authorized_facts only. "
            "Do not reuse any prior draft. "
            f"Target {PREFER_MIN}–{PREFER_MAX} BODY words "
            f"(hard band {TARGET_MIN}–{TARGET_MAX} BODY words). "
            "Develop authorized facts naturally across multiple paragraphs. "
            "No unsupported significance, comparison, predictions, or invented causality."
        )
        system = (
            CAPABILITY_SYSTEM_PROMPT
            + "\nThis is a FRESH regeneration. Prior article text is unavailable and must not be invented."
        )
    else:
        instruction = (
            "Write a developed newsroom ARTICLE, not a summary, abstract, alert, or brief. "
            f"Target approximately 500 BODY words (preferred {PREFER_MIN}–{PREFER_MAX}; "
            f"acceptable {TARGET_MIN}–{TARGET_MAX}). "
            "Use relevant authorized propositions throughout the article. "
            "Develop the story across multiple coherent paragraphs with natural transitions "
            "and attribution. Explain verified context contained in the FactBank. "
            "Do not invent facts, causality, significance, reaction, history, predictions, "
            "or analysis that is not authorized."
        )
        system = CAPABILITY_SYSTEM_PROMPT
    user = {
        "instruction": instruction,
        "evidence_packet": evidence,
        "EvidenceCapacity": CAPACITY_RICH,
        "article_type": ARTICLE_FULL,
        "recommended_word_range": [TARGET_MIN, TARGET_MAX],
        "prefer_word_range": [PREFER_MIN, PREFER_MAX],
        "output_schema": list(V4_JSON_SCHEMA["required"]),
        "body_word_target": {
            "min": TARGET_MIN,
            "prefer_min": PREFER_MIN,
            "prefer_max": PREFER_MAX,
            "max": TARGET_MAX,
        },
        "regeneration": bool(regeneration),
    }
    banned = {
        "extracted_text",
        "source_article_body",
        "source_sentences",
        "previous_article",
        "article_body",
        "matched_source_fragment",
        "offending_sentence",
    }
    assert not (banned & set(evidence.keys())), "generation packet must stay semantic-only"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


class Capability500Writer(V4NaturalProseWriter):
    """Kimi writer with temporary 500-word prompt + higher completion budget."""

    renderer_name = "v4_capability_500_writer"

    def render(
        self,
        packet: WriterEvidencePacket,
        *,
        regeneration: bool = False,
    ) -> V4WriterResult:
        if self.generation_calls >= self.max_calls:
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error="V4 writer call budget exceeded",
                provider=self.provider,
                model=self.model,
            )
            self.last_result = result
            return result
        if not self._configured():
            result = V4WriterResult(
                ok=False,
                provider_error=True,
                error=f"{self.provider} credentials missing",
                provider=self.provider,
                model=self.model,
            )
            self.last_result = result
            return result
        messages = _build_capability_messages(packet, regeneration=regeneration)
        body_extra: dict[str, Any] = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "v4_capability_500_article",
                    "strict": True,
                    "schema": V4_JSON_SCHEMA,
                },
            },
        }
        self.generation_calls += 1
        started = perf_counter()
        response = self.transport.complete(
            messages=messages,
            body_extra=body_extra,
            max_completion_tokens=CAPABILITY_MAX_TOKENS,
            temperature=0.3,
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
                max_completion_tokens=CAPABILITY_MAX_TOKENS,
                temperature=0.3,
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
                    "max_completion_tokens": CAPABILITY_MAX_TOKENS,
                    "capability_500": True,
                    "regeneration": regeneration,
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
                    "max_completion_tokens": CAPABILITY_MAX_TOKENS,
                    "capability_500": True,
                    "regeneration": regeneration,
                },
                raw_payload={
                    "content_type": type(content).__name__,
                    "finish_reason": response.finish_reason,
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


def _rich_depth_override(base: DepthDecision) -> DepthDecision:
    return DepthDecision(
        evidence_capacity=CAPACITY_RICH,
        article_type=ARTICLE_FULL,
        recommended_word_min=TARGET_MIN,
        recommended_word_max=TARGET_MAX,
        prefer_min=PREFER_MIN,
        prefer_max=PREFER_MAX,
        unique_propositions=base.unique_propositions,
        independent_sources=base.independent_sources,
        primary_sources=base.primary_sources,
        numeric_fact_count=base.numeric_fact_count,
        attribution_count=base.attribution_count,
        evidence_limited=False,
        qa_article_mode="normal",
        reason="capability_500_rich_override",
        research=dict(base.research),
    )


def run_capability_500(*, environ: dict[str, str]) -> dict[str, Any]:
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
    env["ARTICLE_MIN_WORDS"] = "120"
    env["NEWSAGENT_V2_DEMO_ARTICLE_MIN_WORDS"] = "120"
    for key, value in env.items():
        if key.startswith("NEWSAGENT") or key in {"ARTICLE_MIN_WORDS"}:
            os.environ[key] = str(value)

    assert_v4_writer_is_free(V4_KIMI_MODEL, environ=env)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_root = MAKE_RUNS_ROOT.parent / "capability_tests" / f"v4_500word_{stamp}"
    out_root.mkdir(parents=True, exist_ok=True)

    base_writer = build_v4_writer(environ=env, enable_failover=False, max_calls=12)
    writer = Capability500Writer(
        transport=base_writer.transport,
        api_key=getattr(base_writer, "api_key", None),
        environ=env,
        max_calls=12,
        timeout_seconds=240,
    )

    discovered = discover_ranked_top5()
    ranked = list(discovered.get("ranked_clusters") or [])
    stories = [cluster_to_story(cluster, original_rank=i) for i, cluster in enumerate(ranked, start=1)]

    inspected: list[dict[str, Any]] = []
    rich_story = None
    rich_research = None
    rich_bank = None
    rich_depth = None
    rich_pack = None

    for index, story in enumerate(stories[:MAX_CANDIDATES], start=1):
        researched = research_event(story)
        pack = researched.pack
        event_id = str(story.get("event_id") or pack.get("event_id") or f"rank-{index}")
        bank = build_fact_bank(event_id=event_id, pack=pack)
        depth = assess_evidence_capacity(bank, research=researched)
        row = {
            "rank": index,
            "event_id": event_id,
            "evidence_capacity": depth.evidence_capacity,
            "article_type": depth.article_type,
            "unique_propositions": depth.unique_propositions,
            "independent_sources": depth.independent_sources,
            "sources_retrieved": researched.sources_retrieved,
            "raw_research_words": researched.raw_research_words,
        }
        inspected.append(row)
        if depth.evidence_capacity == CAPACITY_RICH:
            rich_story = story
            rich_research = researched
            rich_bank = bank
            rich_depth = _rich_depth_override(depth)
            rich_pack = pack
            break

    if rich_story is None or rich_research is None or rich_bank is None or rich_depth is None:
        post = verify_event_030_untouched()
        report = {
            "status": "NO_RICH_EVIDENCE_CANDIDATE",
            "inspected": inspected,
            "event_030_protected": post,
            "out_root": str(out_root),
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
            "500_word_capability_proven": False,
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    # Pre-generation research report (required before writing).
    source_domains = _source_domains(rich_pack or {})
    research_pre = {
        "event": rich_story.get("event_id"),
        "sources_discovered": rich_research.sources_discovered,
        "sources_retrieved": rich_research.sources_retrieved,
        "independent_sources": rich_research.independent_sources,
        "primary_sources": rich_research.primary_sources,
        "source_domains": source_domains,
        "raw_research_words": rich_research.raw_research_words,
        "raw_propositions": len(rich_bank.propositions) + int(rich_bank.dedup_merged_count or 0),
        "unique_propositions": rich_bank.unique_proposition_count,
        "duplicates_removed": rich_bank.dedup_merged_count,
        "conflicts": len(rich_bank.conflicts),
        "evidence_capacity": rich_depth.evidence_capacity,
    }
    (out_root / "research_before_generation.json").write_text(
        json.dumps(research_pre, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    topic = str(
        rich_story.get("representative_title")
        or (rich_pack or {}).get("representative_title")
        or ""
    )
    packet = fact_bank_to_writer_packet(
        rich_bank,
        story_topic=topic,
        source_names=source_domains,
        max_facts=MAX_FACTS,
    )
    ledgers = build_evidence_ledgers(rich_pack or {})
    event_id = str(packet.event_id or rich_story.get("event_id"))

    rendered = writer.render(packet, regeneration=False)
    if not rendered.ok or rendered.native is None:
        report = {
            "status": "FAIL",
            "reason": f"writer_error:{rendered.error}",
            "research": research_pre,
            "provider": writer.provider,
            "model": writer.model,
            "Groq_calls": 0,
            "copyright_provider_calls": 0,
            "500_word_capability_proven": False,
        }
        (out_root / "report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report

    native = rendered.native
    native_words = word_count(native.article_body)
    diag = rendered.request_diagnostic or {}
    finish_reason = diag.get("finish_reason")
    completion_tokens = (rendered.usage or {}).get("completion_tokens")
    enrichment_used = False
    regeneration_used = False

    used0, unused0 = _proposition_usage(packet, native.article_body)
    underproduction_note = None

    if native_words < TARGET_MIN:
        underproduction_note = {
            "native_words": native_words,
            "finish_reason": finish_reason,
            "completion_tokens": completion_tokens,
            "propositions_used": used0,
            "unused_relevant_propositions": unused0,
        }
        report_pre = verify_v4_native(native, packet=packet, ledgers=ledgers)
        unused_set = build_unused_evidence_set(
            packet=packet, report=report_pre, article_body=native.article_body
        )
        if unused_set.facts:
            enrichment_used = True
            text, _calls, err = generate_expansion_text(
                writer=writer,
                native=native,
                unused=unused_set,
                current_words=native_words,
            )
            if text and not err:
                filtered, _rep, filter_err = _filter_expansion_sentences(
                    text,
                    native=native,
                    packet=packet,
                    ledgers=ledgers,
                    article_input=rich_pack or {},
                )
                if filtered and not filter_err:
                    native = deepcopy(native)
                    native.article_body = (native.article_body + " " + filtered).strip()

        if word_count(native.article_body) < TARGET_MIN:
            regeneration_used = True
            # Fresh regeneration — FactBank only; no prior draft.
            regen = writer.render(packet, regeneration=True)
            if regen.ok and regen.native is not None:
                native = regen.native
                finish_reason = (regen.request_diagnostic or {}).get("finish_reason")
                completion_tokens = (regen.usage or {}).get("completion_tokens")

        if word_count(native.article_body) < TARGET_MIN:
            used_f, unused_f = _proposition_usage(packet, native.article_body)
            report = {
                "status": "WRITER_500_CAPABILITY_FAILED",
                "research": research_pre,
                "provider": writer.provider,
                "model": writer.model,
                "native_words": native_words,
                "final_body_words": word_count(native.article_body),
                "enrichment_used": enrichment_used,
                "regeneration_used": regeneration_used,
                "finish_reason": finish_reason,
                "completion_tokens": completion_tokens,
                "propositions_available": len(packet.authorized_facts),
                "propositions_used": used_f,
                "unused_relevant_propositions": unused_f,
                "underproduction": underproduction_note,
                "Groq_calls": 0,
                "copyright_provider_calls": 0,
                "500_word_capability_proven": False,
                "event_030_protected": verify_event_030_untouched(),
            }
            (out_root / "report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            (out_root / "failed_native.json").write_text(
                json.dumps(native.as_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return report

    # Overproduction trim.
    body_words = word_count(native.article_body)
    if body_words > TARGET_MAX:
        native = deepcopy(native)
        native.article_body = _trim_to_max(native.article_body, TARGET_MAX)
        body_words = word_count(native.article_body)

    # Factual repair only (no copyright recovery).
    repaired, report2, repair_log = run_targeted_repairs(
        native,
        packet=packet,
        ledgers=ledgers,
        article_input=rich_pack or {},
        writer=writer,
        min_words=TARGET_MIN,
        allow_destructive_length_pad=False,
    )
    body_words = word_count(repaired.article_body)
    if body_words > TARGET_MAX:
        repaired = deepcopy(repaired)
        repaired.article_body = _trim_to_max(repaired.article_body, TARGET_MAX)
        body_words = word_count(repaired.article_body)
        report2 = verify_v4_native(repaired, packet=packet, ledgers=ledgers)

    article = assemble_v4_article(
        event_id=event_id,
        native=repaired,
        ledgers=ledgers,
        article_input=rich_pack or {},
        report=report2,
    )
    qa = run_article_qa(
        article,
        rich_pack or {},
        article_mode="normal",
        skip_copyright_similarity=True,
    )
    # Capability depth gate: 450–550 body words required.
    final_body = str(article.get("article_body") or "")
    final_words = word_count(final_body)
    depth_ok = TARGET_MIN <= final_words <= TARGET_MAX
    if not depth_ok:
        crit = list(qa.get("critical_failures") or [])
        crit.append(
            {
                "code": "capability_500_out_of_band",
                "message": f"body_words={final_words}; required {TARGET_MIN}-{TARGET_MAX}",
                "severity": "critical",
                "module": "depth",
            }
        )
        from newsagent_v2.article.qa.result import build_qa_result

        qa = build_qa_result(
            event_id=event_id,
            issues=crit + list(qa.get("warnings") or []),
            metrics=dict(qa.get("metrics") or {}),
        )

    used, unused = _proposition_usage(packet, final_body)
    article_hash = _sha256_text(final_body)
    publishable = bool(qa.get("publishable")) and depth_ok and report2.ok
    coherence = "PASS" if publishable and final_words >= TARGET_MIN else "FAIL"

    # Persist SEPARATE artifact — never under the protected event-030 approval path.
    artifact_dir = out_root / "canonical"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "article.json").write_text(
        json.dumps(article, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact_dir / "article_body.txt").write_text(final_body, encoding="utf-8")
    (artifact_dir / "qa.json").write_text(
        json.dumps(qa, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact_dir / "evidence_packet.json").write_text(
        json.dumps(packet.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (artifact_dir / "verification.json").write_text(
        json.dumps(report2.as_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    meta = {
        "test": "v4_500_word_capability",
        "event_id": event_id,
        "article_hash": article_hash,
        "body_words": final_words,
        "protected_event_030_hash": PROTECTED_EVENT_030_HASH,
        "does_not_overwrite_event_030": True,
    }
    (artifact_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
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

    proven = bool(
        rich_depth.evidence_capacity == CAPACITY_RICH
        and TARGET_MIN <= final_words <= TARGET_MAX
        and grounding_ok
        and quote_ok
        and mech_ok
        and sec_ok
        and critical_count == 0
        and publishable
    )

    post = verify_event_030_untouched()
    report = {
        "status": "PASS" if proven else "FAIL",
        "event": event_id,
        "headline": article.get("headline"),
        "dek": article.get("dek"),
        "sources_retrieved": rich_research.sources_retrieved,
        "independent_sources": rich_research.independent_sources,
        "primary_sources": rich_research.primary_sources,
        "source_domains": source_domains,
        "raw_research_words": rich_research.raw_research_words,
        "unique_propositions": rich_bank.unique_proposition_count,
        "duplicates_removed": rich_bank.dedup_merged_count,
        "conflicts": len(rich_bank.conflicts),
        "evidence_capacity": rich_depth.evidence_capacity,
        "provider": writer.provider,
        "model": writer.model,
        "native_words": native_words,
        "enrichment_used": enrichment_used,
        "regeneration_used": regeneration_used,
        "final_body_words": final_words,
        "propositions_available": len(packet.authorized_facts),
        "propositions_used": used,
        "unused_relevant_propositions": unused,
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
        "artifact_dir": str(artifact_dir),
        "500_word_capability_proven": proven,
        "Groq_calls": 0,
        "copyright_provider_calls": 0,
        "kimi_calls": int(getattr(writer, "generation_calls", 0) or 0),
        "finish_reason": finish_reason,
        "completion_tokens": completion_tokens,
        "underproduction": underproduction_note,
        "inspected_candidates": inspected,
        "event_030_protected_before": pre,
        "event_030_protected_after": post,
        "research_before_generation": research_pre,
        "V4_MAX_COMPLETION_TOKENS_production": V4_MAX_COMPLETION_TOKENS,
        "capability_max_tokens": CAPABILITY_MAX_TOKENS,
    }
    (out_root / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return report


def main() -> int:
    load_dotenv(REPO / ".env")
    environ = _load_environ()
    result = run_capability_500(environ=environ)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    status = str(result.get("status") or "")
    if status == "PASS":
        return 0
    if status == "NO_RICH_EVIDENCE_CANDIDATE":
        return 2
    if status == "WRITER_500_CAPABILITY_FAILED":
        return 3
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
