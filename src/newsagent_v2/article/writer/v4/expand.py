"""V4 editorial length realization: unused evidence → one targeted expansion."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from newsagent_v2.article.qa.textutil import split_sentences, word_count, words
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.v4.packet import AuthorizedFact, WriterEvidencePacket
from newsagent_v2.article.writer.v4.verify import (
    STATUS_AMBIGUOUS,
    STATUS_QUOTE_BAD,
    STATUS_UNSUPPORTED,
    VerificationReport,
    verify_v4_native,
)
from newsagent_v2.article.writer.v4.writer import (
    V4_WRITER_MODEL,
    V4_WRITER_PROVIDER,
    V4NativeArticle,
    assert_v4_writer_is_free,
)
from newsagent_v2.article.writer.v4.provider import V4_MAX_COMPLETION_TOKENS

EDITORIAL_TARGET_MIN_WORDS = 250
EDITORIAL_TARGET_MAX_WORDS = 400
BELOW_EDITORIAL_TARGET_WARNING = "below_editorial_target_minimum"
MAX_EXPANSION_CALLS_PER_ARTICLE = 1
_REDUNDANCY_OVERLAP = 0.62


@dataclass(frozen=True)
class UnusedEvidenceSet:
    facts: tuple[AuthorizedFact, ...]
    represented_ids: tuple[str, ...]
    excluded_redundant_ids: tuple[str, ...] = ()

    @property
    def fact_ids(self) -> list[str]:
        return [row.id for row in self.facts]

    def as_dict(self) -> dict[str, Any]:
        return {
            "unused_fact_ids": self.fact_ids,
            "unused_count": len(self.facts),
            "represented_ids": list(self.represented_ids),
            "excluded_redundant_ids": list(self.excluded_redundant_ids),
            "facts": [row.as_dict() for row in self.facts],
        }


@dataclass
class ExpansionResult:
    triggered: bool = False
    attempted: bool = False
    model_calls: int = 0
    unused_before: list[str] = field(default_factory=list)
    generated_words: int = 0
    validated_words: int = 0
    appended_text: str = ""
    rejected: bool = False
    reject_reason: str | None = None
    editorial_target_met: bool = False
    warning: str | None = None
    depth_loss_origin: str | None = None
    blocked: bool = False
    mode: str = "none"  # none | enrichment | regeneration | accept_limited
    provider_infrastructure_error: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "triggered": self.triggered,
            "attempted": self.attempted,
            "model_calls": self.model_calls,
            "unused_facts_before_expansion": list(self.unused_before),
            "expansion_generated_words": self.generated_words,
            "expansion_validated_words": self.validated_words,
            "appended_text": self.appended_text[:500],
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "editorial_target_met": self.editorial_target_met,
            "warning": self.warning,
            "depth_loss_origin": self.depth_loss_origin,
            "blocked": self.blocked,
            "mode": self.mode,
            "provider_infrastructure_error": self.provider_infrastructure_error,
        }


def _token_overlap_ratio(left: str, right: str) -> float:
    a = set(words(left))
    b = set(words(right))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, min(len(a), len(b)))


def _proposition_redundant_with_article(proposition: str, article_body: str) -> bool:
    body = article_body or ""
    if _token_overlap_ratio(proposition, body) >= _REDUNDANCY_OVERLAP:
        return True
    for sentence in split_sentences(body):
        if _token_overlap_ratio(proposition, sentence) >= 0.72:
            return True
    return False


def build_unused_evidence_set(
    *,
    packet: WriterEvidencePacket,
    report: VerificationReport,
    article_body: str,
) -> UnusedEvidenceSet:
    """Deterministic unused authorized facts not already represented or redundant."""
    represented: list[str] = []
    for row in report.rows:
        for cid in row.claim_ids:
            if cid not in represented:
                represented.append(cid)
    # Also treat high-overlap facts as represented even without claim_id match.
    for fact in packet.authorized_facts:
        if fact.id in represented:
            continue
        if _proposition_redundant_with_article(fact.proposition, article_body):
            if fact.id not in represented:
                represented.append(fact.id)

    excluded_redundant: list[str] = []
    unused: list[AuthorizedFact] = []
    seen_props: set[str] = set()
    for fact in packet.authorized_facts:
        if fact.id in represented:
            continue
        if not fact.provenance:
            continue
        prop_key = re.sub(r"\s+", " ", fact.proposition).strip().lower()
        if len(prop_key.split()) < 4:
            excluded_redundant.append(fact.id)
            continue
        if prop_key in seen_props:
            excluded_redundant.append(fact.id)
            continue
        # Drop near-duplicate of another unused candidate already kept.
        if any(_token_overlap_ratio(fact.proposition, kept.proposition) >= 0.8 for kept in unused):
            excluded_redundant.append(fact.id)
            continue
        if _proposition_redundant_with_article(fact.proposition, article_body):
            excluded_redundant.append(fact.id)
            continue
        seen_props.add(prop_key)
        unused.append(fact)
    return UnusedEvidenceSet(
        facts=tuple(unused),
        represented_ids=tuple(represented),
        excluded_redundant_ids=tuple(excluded_redundant),
    )


def _enrichment_messages(
    *,
    native: V4NativeArticle,
    unused: UnusedEvidenceSet,
    current_words: int,
    target_min: int,
    target_max: int,
) -> list[dict[str, str]]:
    """Bounded enrichment from unused authorized propositions only — no source prose."""
    system = (
        "You enrich an existing CoinNetwork news article using ONLY unused authorized evidence.\n"
        "Return ONLY the new paragraph(s) as plain text — no JSON, no markdown.\n"
        "Do not invent motives, causality, predictions, significance, or comparisons.\n"
        "Preserve numbers, attribution, and modality exactly.\n"
        "Do not copy source wording. Do not repeat existing article content."
    )
    user = {
        "current_word_count": current_words,
        "target_band": f"{target_min}-{target_max}",
        "existing_article_body": native.article_body,
        "unused_authorized_facts": [row.as_dict() for row in unused.facts],
        "instruction": (
            "Add developed paragraph(s) from unused authorized facts only. "
            "Return ONLY the new paragraph(s)."
        ),
    }
    # Contaminate guard: unused facts are semantic; never attach extracted_text.
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def enrichment_messages_exclude_source_prose(messages: list[dict[str, str]]) -> bool:
    blob = json.dumps(messages).lower()
    banned = (
        "extracted_text",
        "source_article_body",
        "matched_source_fragment",
        "factual_snippets",
    )
    return not any(token in blob for token in banned)


def _parse_expansion_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("expansion", "new_paragraphs", "article_body", "text", "content"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        fence = re.search(r"```(?:\w+)?\s*(.*?)\s*```", text, re.DOTALL)
        if fence:
            text = fence.group(1).strip()
        # If model returned JSON anyway, peel it.
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                return _parse_expansion_text(parsed)
            except json.JSONDecodeError:
                pass
        return text
    return ""


def _filter_expansion_sentences(
    expansion_text: str,
    *,
    native: V4NativeArticle,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
) -> tuple[str, VerificationReport | None, str | None]:
    """Verify expansion-only; drop unsupported/duplicate/copyright-bad sentences."""
    text = (expansion_text or "").strip()
    if not text:
        return "", None, "empty_expansion"
    probe = deepcopy(native)
    probe.article_body = text
    probe.headline = ""
    probe.dek = ""
    report = verify_v4_native(probe, packet=packet, ledgers=ledgers)
    if report.number_issues or report.quote_issues:
        return "", report, "expansion_number_or_quote_failure"
    drop = {
        row.text
        for row in report.rows
        if row.status in {STATUS_UNSUPPORTED, STATUS_AMBIGUOUS, STATUS_QUOTE_BAD}
    }
    kept: list[str] = []
    for sentence in split_sentences(text):
        if sentence in drop:
            continue
        if _proposition_redundant_with_article(sentence, native.article_body):
            continue
        kept.append(sentence)
    if not kept:
        return "", report, "expansion_unsupported_or_duplicate"
    candidate = " ".join(kept).strip()
    # Copyright similarity is not a gate — keep grounding-validated sentences.
    return candidate, report, None


def generate_expansion_text(
    *,
    writer: Any,
    native: V4NativeArticle,
    unused: UnusedEvidenceSet,
    current_words: int,
) -> tuple[str | None, int, str | None]:
    """One free-provider expansion call. Returns (text, model_calls, error)."""
    model = getattr(writer, "model", V4_WRITER_MODEL)
    assert_v4_writer_is_free(model)
    # Scripted / offline double.
    if hasattr(writer, "expand"):
        text = writer.expand(native=native, unused=unused, current_words=current_words)
        return (str(text).strip() if text else None), 1, None
    from newsagent_v2.providers.groq_editorial import (
        GROQ_CHAT_COMPLETIONS_URL,
        extract_usage,
        parse_message_content,
        post_chat_completion,
        request_headers,
    )

    messages = _enrichment_messages(
        native=native,
        unused=unused,
        current_words=current_words,
        target_min=EDITORIAL_TARGET_MIN_WORDS,
        target_max=EDITORIAL_TARGET_MAX_WORDS,
    )
    body = {
        "model": model,
        "messages": messages,
        "temperature": 0.3,
        "max_completion_tokens": V4_MAX_COMPLETION_TOKENS,
    }
    http_post: Callable[..., Any] | None = getattr(writer, "http_post", None)
    api_key = getattr(writer, "api_key", None)
    timeout_seconds = int(getattr(writer, "timeout_seconds", 120) or 120)
    try:
        if http_post is not None:
            response = http_post(
                GROQ_CHAT_COMPLETIONS_URL,
                headers=request_headers(api_key or "test-key"),
                json=body,
                timeout=timeout_seconds,
            )
            status = getattr(response, "status_code", None)
            if status and int(status) == 429:
                return None, 1, "RATE_LIMITED"
            payload = response.json() if hasattr(response, "json") else response
            content = parse_message_content(payload) if isinstance(payload, dict) and "choices" in payload else payload
        else:
            result = post_chat_completion(
                body,
                api_key=str(api_key or ""),
                url=GROQ_CHAT_COMPLETIONS_URL,
                timeout_seconds=timeout_seconds,
                max_attempts=1,
            )
            if not result.get("ok"):
                status = result.get("status_code")
                if status == 429:
                    return None, 1, "RATE_LIMITED"
                message = str(result.get("error") or f"HTTP {status}")
                if "429" in message or "rate limit" in message.lower():
                    return None, 1, "RATE_LIMITED"
                return None, 1, message[:300]
            payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
            # parse_message_content expects chat payload; content may be plain string.
            choices = payload.get("choices") if isinstance(payload, dict) else None
            if isinstance(choices, list) and choices:
                message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}
                content = message.get("content") if isinstance(message, dict) else None
            else:
                content = None
            del extract_usage  # usage recorded by caller if needed
        text = _parse_expansion_text(content)
        if not text:
            return None, 1, "empty_expansion_response"
        return text, 1, None
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "429" in text or "rate limit" in text.lower():
            return None, 1, "RATE_LIMITED"
        return None, 1, text[:300]


def realize_body_word_target(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    report: VerificationReport,
    writer: Any | None = None,
    target_min: int = EDITORIAL_TARGET_MIN_WORDS,
    target_max: int = EDITORIAL_TARGET_MAX_WORDS,
    depth: Any | None = None,
) -> tuple[V4NativeArticle, VerificationReport, ExpansionResult]:
    """
    Capacity-aware length path.

    - LIMITED coherent drafts are accepted (not WRITER_UNDERPRODUCED).
    - MEDIUM slightly below band → ONE unused-proposition enrichment.
    - RICH severe shortfall → ONE fresh packet-only regeneration.
    - Provider rate limits are infrastructure errors, not editorial failure.
    """
    from newsagent_v2.article.writer.v4.evidence_depth import (
        DepthDecision,
        in_recommended_band,
        is_writer_underproduced,
        provider_error_is_infrastructure,
        slightly_below_recommended,
    )

    current = deepcopy(native)
    current_words = word_count(current.article_body)
    # Depth-aware targets override legacy fixed 250 when provided.
    if isinstance(depth, DepthDecision):
        target_min = depth.recommended_word_min
        target_max = depth.recommended_word_max

    result = ExpansionResult(
        editorial_target_met=target_min <= current_words <= target_max,
        depth_loss_origin=None,
    )
    if in_recommended_band(current_words, depth) if isinstance(depth, DepthDecision) else (
        current_words >= target_min
    ):
        if current_words > target_max:
            result.warning = "above_editorial_target_maximum"
        result.mode = "none"
        result.editorial_target_met = True
        return current, report, result

    # LIMITED: short is not failure when evidence capacity is limited.
    if isinstance(depth, DepthDecision) and depth.evidence_limited:
        result.mode = "accept_limited"
        result.editorial_target_met = current_words >= target_min
        result.warning = None if result.editorial_target_met else "below_limited_depth_floor"
        return current, report, result

    # MEDIUM enrichment: slightly below + unused props.
    if isinstance(depth, DepthDecision) and slightly_below_recommended(current_words, depth):
        unused = build_unused_evidence_set(
            packet=packet, report=report, article_body=current.article_body
        )
        result.unused_before = unused.fact_ids
        if unused.facts and writer is not None:
            result.triggered = True
            result.attempted = True
            result.mode = "enrichment"
            text, calls, err = generate_expansion_text(
                writer=writer,
                native=current,
                unused=unused,
                current_words=current_words,
            )
            result.model_calls = calls
            if err and provider_error_is_infrastructure(err):
                result.rejected = True
                result.provider_infrastructure_error = True
                result.reject_reason = f"PROVIDER_RATE_LIMITED:{err}"
                return current, report, result
            if text:
                filtered, _rep, filter_err = _filter_expansion_sentences(
                    text,
                    native=current,
                    packet=packet,
                    ledgers=ledgers,
                    article_input=article_input,
                )
                if filtered and not filter_err:
                    current.article_body = (current.article_body + " " + filtered).strip()
                    result.appended_text = filtered
                    result.generated_words = word_count(filtered)
                    report2 = verify_v4_native(current, packet=packet, ledgers=ledgers)
                    words2 = word_count(current.article_body)
                    result.validated_words = words2
                    result.editorial_target_met = target_min <= words2 <= target_max
                    return current, report2, result
        # Enrichment unavailable/failed — continue without inventing depth.
        result.editorial_target_met = current_words >= target_min
        return current, report, result

    # Underproduction relative to capacity (RICH / severe MEDIUM).
    underproduced = (
        is_writer_underproduced(current_words, depth)
        if isinstance(depth, DepthDecision)
        else current_words < target_min
    )
    if not underproduced:
        result.editorial_target_met = current_words >= target_min
        return current, report, result

    result.triggered = True
    result.mode = "regeneration"
    result.depth_loss_origin = "NATIVE_UNDERPRODUCTION"
    if writer is None or not hasattr(writer, "render"):
        result.rejected = True
        result.reject_reason = "WRITER_UNDERPRODUCED:no_writer_for_regeneration"
        result.warning = BELOW_EDITORIAL_TARGET_WARNING
        return current, report, result

    result.attempted = True
    rendered = writer.render(packet, regeneration=True)
    result.model_calls = 1
    if not getattr(rendered, "ok", False) or rendered.native is None:
        err = str(getattr(rendered, "error", None) or "regeneration_failed")
        if provider_error_is_infrastructure(err) or bool(
            getattr(rendered, "provider_error", False)
        ):
            result.rejected = True
            result.provider_infrastructure_error = True
            result.reject_reason = f"PROVIDER_RATE_LIMITED:{err}"
            return current, report, result
        result.rejected = True
        result.reject_reason = f"WRITER_UNDERPRODUCED:{err}"
        result.warning = BELOW_EDITORIAL_TARGET_WARNING
        return current, report, result

    fresh = rendered.native
    fresh_words = word_count(fresh.article_body)
    result.generated_words = fresh_words
    result.appended_text = ""
    report2 = verify_v4_native(fresh, packet=packet, ledgers=ledgers)
    if not report2.ok:
        result.rejected = True
        result.reject_reason = "WRITER_UNDERPRODUCED:regeneration_failed_grounding"
        result.warning = BELOW_EDITORIAL_TARGET_WARNING
        return current, report, result
    if isinstance(depth, DepthDecision):
        still_short = is_writer_underproduced(fresh_words, depth)
    else:
        still_short = fresh_words < target_min
    if still_short:
        result.rejected = True
        result.reject_reason = "WRITER_UNDERPRODUCED"
        result.validated_words = fresh_words
        result.warning = BELOW_EDITORIAL_TARGET_WARNING
        result.editorial_target_met = False
        return fresh, report2, result

    result.validated_words = fresh_words
    result.editorial_target_met = target_min <= fresh_words <= target_max
    return fresh, report2, result


def realize_editorial_length(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    report: VerificationReport,
    writer: Any | None = None,
    target_min: int = EDITORIAL_TARGET_MIN_WORDS,
    depth_loss_origin: str | None = None,
    native_words: int | None = None,
    depth: Any | None = None,
) -> tuple[V4NativeArticle, VerificationReport, ExpansionResult]:
    """Active V4 length wrapper — capacity-aware regeneration/enrichment."""
    del depth_loss_origin, native_words
    return realize_body_word_target(
        native,
        packet=packet,
        ledgers=ledgers,
        article_input=article_input,
        report=report,
        writer=writer,
        target_min=target_min,
        depth=depth,
    )
