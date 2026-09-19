"""V4 transactional copyright recovery. Rewrite/clean-room; no destructive salvage."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.article.qa.similarity import check_similarity
from newsagent_v2.article.qa.textutil import split_sentences, word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers
from newsagent_v2.article.writer.v4.expand import EDITORIAL_TARGET_MIN_WORDS
from newsagent_v2.article.writer.v4.packet import WriterEvidencePacket
from newsagent_v2.article.writer.v4.provider import V4_MAX_COMPLETION_TOKENS
from newsagent_v2.article.writer.v4.repair import (
    MAX_STRUCTURAL_REWRITE_ATTEMPTS,
    REPAIR_LENGTH_RETENTION_ADVISORY,
    RepairAction,
    find_copyright_offending_sentences,
    independent_semantic_rewrite_attempts,
    _authorized_atomic_cores,
    _sentence_similarity,
    _shares_exact_ngram,
)
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_WRITER_MODEL,
    V4NativeArticle,
    assert_v4_writer_is_free,
    parse_v4_native,
)

SOURCE_SHAPED_OFFENDER_THRESHOLD = 4
CLEAN_ROOM_OFFENDER_RATIO = 0.35
COPYRIGHT_DEPTH_RETENTION_ADVISORY = 0.80
MAX_CLEAN_ROOM_REWRITES = 1
MAX_SEMANTIC_SENTENCE_REWRITES = 1

DEPTH_NATIVE_UNDERPRODUCTION = "NATIVE_UNDERPRODUCTION"
DEPTH_COPYRIGHT_REPAIR = "COPYRIGHT_REPAIR"
DEPTH_FACTUAL_REPAIR = "FACTUAL_REPAIR"
DEPTH_OTHER = "OTHER"

MODE_LOCAL_REWRITE = "LOCAL_SEMANTIC_REWRITE"
MODE_CLEAN_ROOM = "CLEAN_ROOM_SEMANTIC_REWRITE"
MODE_DROP_ALLOWED = "DROP_ALLOWED"
MODE_REJECT = "REJECT_CANDIDATE"
MODE_NONE = "NONE"

CLEAN_ROOM_SYSTEM_PROMPT = """You write a completely fresh professional news article from structured facts only.
Target approximately 280–330 words. Allowed range 250–400 when evidence supports it.
This is an ARTICLE, not a brief.
Use developed newsroom prose, varied syntax, natural transitions and professional vocabulary.
Fully realize the supplied facts.
You may use grammatical/editorial connective language that adds no new factual proposition.
Do not copy or imitate any source wording.
Do not invent facts, numbers, quotes, motives, causality, predictions, market reaction, or historical context.
Preserve numbers, attribution, polarity, and modality exactly.
Return exactly one JSON object with keys:
headline, dek, article_body, seo_title, meta_description, slug
Do NOT include fact_ids_used, relationships, paragraph plans, or proof metadata.
""".strip()


@dataclass
class CopyrightRecoveryResult:
    article: V4NativeArticle
    actions: list[RepairAction] = field(default_factory=list)
    mode: str = MODE_NONE
    pre_copyright_words: int = 0
    post_copyright_words: int = 0
    copyright_depth_retention_ratio: float = 1.0
    offenders: list[str] = field(default_factory=list)
    clean_room_calls: int = 0
    rejected: bool = False
    reject_reason: str | None = None
    depth_loss_origin: str | None = None
    escalated_to_clean_room: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "pre_copyright_words": self.pre_copyright_words,
            "post_copyright_words": self.post_copyright_words,
            "copyright_depth_retention_ratio": self.copyright_depth_retention_ratio,
            "offenders": [row[:160] for row in self.offenders],
            "offender_count": len(self.offenders),
            "clean_room_calls": self.clean_room_calls,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "depth_loss_origin": self.depth_loss_origin,
            "escalated_to_clean_room": self.escalated_to_clean_room,
            "actions": [row.as_dict() for row in self.actions],
        }


def _body_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _semantic_only_packet(packet: WriterEvidencePacket) -> dict[str, Any]:
    """Facts-only payload. Never includes source prose or prior article body."""
    return {
        "event_id": packet.event_id,
        "story_topic": packet.story_topic,
        "authorized_facts": [
            {
                "id": row.id,
                "proposition": row.proposition,
                "attribution": row.attribution,
                "numbers": list(row.numbers),
                "polarity": row.polarity,
                "modal": row.modal,
                "entities": list(row.entities),
            }
            for row in packet.authorized_facts
        ],
        "authorized_quotes": [row.as_dict() for row in packet.authorized_quotes],
        "authorized_entities": list(packet.authorized_entities),
        "forbidden": list(packet.forbidden),
        # Explicitly omit source_context bodies / prior article.
    }


def copyright_offender_ratio(body: str, offenders: list[str]) -> float:
    sentences = [s for s in split_sentences(body or "") if s.strip()]
    return round(len(offenders) / max(1, len(sentences)), 4)


def should_escalate_clean_room(body: str, offenders: list[str]) -> bool:
    """Last-resort only: substantial source-shaped fraction, not 1–3 local sentences."""
    if not offenders:
        return False
    ratio = copyright_offender_ratio(body, offenders)
    return (
        len(offenders) >= SOURCE_SHAPED_OFFENDER_THRESHOLD
        and ratio >= CLEAN_ROOM_OFFENDER_RATIO
    )


def _complete_via_writer(
    writer: Any,
    *,
    messages: list[dict[str, str]],
    max_completion_tokens: int,
    temperature: float,
) -> tuple[Any, int, str | None]:
    """Prefer the writer's configured transport (Kimi/Groq). Never hardcode Groq when Kimi is primary."""
    transport = getattr(writer, "transport", None)
    if transport is not None and hasattr(transport, "complete"):
        result = transport.complete(
            messages=messages,
            max_completion_tokens=max_completion_tokens,
            temperature=temperature,
        )
        if not getattr(result, "ok", False):
            err = str(getattr(result, "error", None) or "provider_error")
            status = getattr(result, "status_code", None)
            if status == 429 or "429" in err or "rate limit" in err.lower():
                return None, 1, "RATE_LIMITED"
            return None, 1, err[:300]
        content = getattr(result, "content", None)
        if content is None and isinstance(getattr(result, "payload", None), dict):
            from newsagent_v2.providers.groq_editorial import parse_message_content

            content = parse_message_content(result.payload)
        return content, 1, None

    from newsagent_v2.providers.groq_editorial import (
        GROQ_CHAT_COMPLETIONS_URL,
        parse_message_content,
        post_chat_completion,
        request_headers,
    )
    from newsagent_v2.article.writer.v4.provider import build_v4_chat_body

    model = str(getattr(writer, "model", V4_WRITER_MODEL) or V4_WRITER_MODEL)
    body = build_v4_chat_body(
        model=model,
        messages=messages,
        max_completion_tokens=max_completion_tokens,
        temperature=temperature,
    )
    http_post = getattr(writer, "http_post", None)
    api_key = getattr(writer, "api_key", None)
    timeout_seconds = int(getattr(writer, "timeout_seconds", 120) or 120)
    if http_post is not None:
        response = http_post(
            GROQ_CHAT_COMPLETIONS_URL,
            headers=request_headers(api_key or "test-key"),
            json=body,
            timeout=timeout_seconds,
        )
        status = getattr(response, "status_code", None)
        payload = response.json() if hasattr(response, "json") else response
        if status and int(status) == 429:
            return None, 1, "RATE_LIMITED"
        if status and int(status) >= 400:
            return None, 1, f"HTTP {status}"
        content = (
            parse_message_content(payload)
            if isinstance(payload, dict) and "choices" in payload
            else payload
        )
        return content, 1, None
    result = post_chat_completion(
        body,
        api_key=str(api_key or ""),
        url=GROQ_CHAT_COMPLETIONS_URL,
        timeout_seconds=timeout_seconds,
        max_attempts=1,
    )
    if not result.get("ok"):
        if result.get("status_code") == 429:
            return None, 1, "RATE_LIMITED"
        return None, 1, str(result.get("error") or "provider_failed")[:300]
    payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
    choices = payload.get("choices") if isinstance(payload, dict) else None
    if isinstance(choices, list) and choices:
        message = (choices[0] or {}).get("message") if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
    else:
        content = None
    return content, 1, None


def generate_semantic_sentence_rewrite(
    *,
    writer: Any,
    packet: WriterEvidencePacket,
    proposition_texts: list[str],
) -> tuple[str | None, int, str | None]:
    """One model call: rewrite from authorized props only. No source/offender text."""
    model = getattr(writer, "model", V4_WRITER_MODEL)
    assert_v4_writer_is_free(model, environ=getattr(writer, "environ", None))
    messages = [
        {
            "role": "system",
            "content": (
                "Rewrite ONE newsroom sentence from authorized factual propositions only. "
                "Use a materially different structure from typical wire copy. "
                "Preserve names, numbers, dates, attribution, polarity, and modality exactly. "
                "Do not invent facts. Return ONLY the replacement sentence as plain text."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "authorized_atomic_propositions": proposition_texts,
                    "protected_entities": list(packet.authorized_entities),
                    "required_numbers": sorted(
                        {
                            str(n)
                            for fact in packet.authorized_facts
                            for n in (fact.numbers or [])
                        }
                    ),
                    "attribution": [
                        str(fact.attribution)
                        for fact in packet.authorized_facts
                        if fact.attribution
                    ][:12],
                    "modality": [
                        str(getattr(fact, "modality", None) or fact.modal or "")
                        for fact in packet.authorized_facts
                        if (getattr(fact, "modality", None) or fact.modal)
                    ][:12],
                    "instruction": "Produce one independently expressed sentence.",
                },
                ensure_ascii=False,
            ),
        },
    ]
    # Contaminate check: user payload must not include source/offender keys.
    user_payload = json.loads(messages[1]["content"])
    banned = {
        "extracted_text",
        "source_article_body",
        "source_sentences",
        "offending_sentence",
        "matched_source_fragment",
        "previous_article",
        "article_body",
    }
    if banned & set(user_payload.keys()):
        return None, 0, "semantic_rewrite_input_contaminated"

    if hasattr(writer, "semantic_rewrite"):
        text = writer.semantic_rewrite(proposition_texts)
        return (str(text).strip() if text else None), 1, None

    try:
        content, calls, err = _complete_via_writer(
            writer,
            messages=messages,
            max_completion_tokens=min(220, V4_MAX_COMPLETION_TOKENS),
            temperature=0.35,
        )
        if err:
            return None, calls, err
        text = str(content or "").strip()
        if not text:
            return None, calls, "empty_semantic_rewrite"
        # Strip accidental quotes/fences.
        text = text.strip().strip("`").strip()
        if text.lower().startswith("sentence:"):
            text = text.split(":", 1)[1].strip()
        # If model returned JSON accidentally, take first string value.
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    for key in ("sentence", "replacement", "article_body", "text"):
                        if isinstance(parsed.get(key), str) and parsed[key].strip():
                            text = parsed[key].strip()
                            break
            except json.JSONDecodeError:
                pass
        return text, calls, None
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "429" in msg or "rate limit" in msg.lower():
            return None, 1, "RATE_LIMITED"
        return None, 1, msg[:300]


def build_clean_room_messages(packet: WriterEvidencePacket) -> list[dict[str, str]]:
    user = {
        "instruction": (
            "Write a completely fresh professional news article from the supplied "
            "structured facts only. Do not reuse earlier draft wording."
        ),
        "semantic_facts": _semantic_only_packet(packet),
        "target_band": f"{EDITORIAL_TARGET_MIN_WORDS}-400",
        "preferred_words": "280-330",
        "output_schema": [
            "headline",
            "dek",
            "article_body",
            "seo_title",
            "meta_description",
            "slug",
        ],
    }
    blob = json.dumps(user, ensure_ascii=False)
    return [
        {"role": "system", "content": CLEAN_ROOM_SYSTEM_PROMPT},
        {"role": "user", "content": blob},
    ]


def clean_room_input_is_semantic_only(messages: list[dict[str, str]]) -> bool:
    """Ensure clean-room user payload carries facts only — no source/prior prose fields."""
    if not messages:
        return False
    user = next((m for m in messages if m.get("role") == "user"), None)
    if not user:
        return False
    try:
        payload = json.loads(user.get("content") or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(payload, dict):
        return False
    # Structural contamination: these keys must never appear.
    banned_keys = {
        "extracted_text",
        "source_article_body",
        "source_sentences",
        "previous_article",
        "prior_article",
        "offending_sentence",
        "copyright_matched",
        "existing_article_body",
        "article_pre_verification",
        "article_body",
        "native_article",
    }

    def _walk(obj: Any, path: str = "") -> bool:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if key_l in banned_keys:
                    return False
                if not _walk(value, f"{path}.{key}"):
                    return False
        elif isinstance(obj, list):
            for item in obj:
                if not _walk(item, path):
                    return False
        return True

    if not _walk(payload):
        return False
    semantic = payload.get("semantic_facts")
    if not isinstance(semantic, dict):
        return False
    if "authorized_facts" not in semantic:
        return False
    # Must not embed a full prior body string field.
    if isinstance(semantic.get("article_body"), str) and semantic.get("article_body"):
        return False
    return True


def generate_clean_room_article(
    *,
    writer: Any,
    packet: WriterEvidencePacket,
) -> tuple[V4NativeArticle | None, int, str | None]:
    """One clean-room generation from semantic facts only (writer transport)."""
    model = getattr(writer, "model", V4_WRITER_MODEL)
    assert_v4_writer_is_free(model, environ=getattr(writer, "environ", None))
    if hasattr(writer, "clean_room_render"):
        result = writer.clean_room_render(packet)
        if isinstance(result, V4NativeArticle):
            return result, 1, None
        if isinstance(result, dict):
            if result.get("error"):
                return None, 1, str(result.get("error"))
            return parse_v4_native(result), 1, None
        return None, 1, "clean_room_scripted_empty"

    messages = build_clean_room_messages(packet)
    if not clean_room_input_is_semantic_only(messages):
        return None, 0, "clean_room_input_contaminated"
    try:
        content, calls, err = _complete_via_writer(
            writer,
            messages=messages,
            max_completion_tokens=V4_MAX_COMPLETION_TOKENS,
            temperature=0.35,
        )
        if err:
            return None, calls, err
        native = parse_v4_native(content)
        return native, calls, None
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "429" in text or "rate limit" in text.lower():
            return None, 1, "RATE_LIMITED"
        return None, 1, text[:300]


def _validate_unit_replacement(
    *,
    original: V4NativeArticle,
    replacement_sentence: str,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    old_sentence: str,
) -> bool:
    """Require grounding + strict facts + exact overlap + copyright similarity + mechanics."""
    from newsagent_v2.article.qa.mechanics import check_mechanics
    from newsagent_v2.article.qa.similarity import HIGH_SENTENCE_SIMILARITY

    if not replacement_sentence.strip():
        return False
    if replacement_sentence.strip() == old_sentence.strip():
        return False
    if _shares_exact_ngram(old_sentence, replacement_sentence):
        return False
    if _sentence_similarity(old_sentence, replacement_sentence) >= HIGH_SENTENCE_SIMILARITY:
        return False

    probe = deepcopy(original)
    if old_sentence not in probe.article_body:
        return False
    probe.article_body = probe.article_body.replace(old_sentence, replacement_sentence, 1)
    report = verify_v4_native(probe, packet=packet, ledgers=ledgers)
    if not report.ok:
        return False

    article = {
        "event_id": packet.event_id,
        "headline": probe.headline,
        "dek": probe.dek,
        "article_body": replacement_sentence,
        "claims": [],
        "quotes": [],
    }
    issues, _metrics = check_similarity(article, article_input)
    codes = {item.get("code") for item in issues if isinstance(item, dict)}
    if {"exact_phrase_overlap", "high_sentence_similarity"} & codes:
        return False

    full_article = {
        "event_id": packet.event_id,
        "headline": probe.headline,
        "dek": probe.dek,
        "article_body": probe.article_body,
        "claims": [],
        "quotes": [],
    }
    mech_issues, _mech = check_mechanics(full_article)
    if any(
        isinstance(item, dict) and str(item.get("severity") or "").lower() == "critical"
        for item in mech_issues
    ):
        return False
    return True


def _projected_words_after_drops(body: str, drop: list[str]) -> int:
    staged = body
    for sentence in drop:
        if sentence in staged:
            staged = staged.replace(sentence, "", 1)
    return word_count(" ".join(staged.split()))


def recover_copyright(
    native: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    writer: Any | None = None,
    target_min: int = EDITORIAL_TARGET_MIN_WORDS,
    clean_room_budget: int = MAX_CLEAN_ROOM_REWRITES,
) -> CopyrightRecoveryResult:
    """Transactional copyright recovery. Never destructively salvage a target-sized article."""
    pre = deepcopy(native)
    pre_words = word_count(pre.article_body)
    pre_hash = _body_hash(pre.article_body)
    result = CopyrightRecoveryResult(
        article=deepcopy(native),
        pre_copyright_words=pre_words,
        post_copyright_words=pre_words,
    )
    offenders = find_copyright_offending_sentences(pre, article_input)
    result.offenders = list(offenders)
    if not offenders:
        result.mode = MODE_NONE
        return result

    article_sentence_count = len([s for s in split_sentences(pre.article_body or "") if s.strip()])
    offender_ratio = copyright_offender_ratio(pre.article_body, offenders)
    result.actions.append(
        RepairAction(
            kind="copyright_offender_census",
            detail=(
                f"copyright_offender_sentence_count={len(offenders)}; "
                f"article_sentence_count={article_sentence_count}; "
                f"copyright_offender_ratio={offender_ratio}"
            ),
        )
    )

    # Substantial source-shaped draft only → clean room. Small local counts stay surgical.
    if should_escalate_clean_room(pre.article_body, offenders):
        return _escalate_clean_room(
            pre,
            packet=packet,
            ledgers=ledgers,
            article_input=article_input,
            writer=writer,
            result=result,
            reason="SOURCE_SHAPED_DRAFT",
            clean_room_budget=clean_room_budget,
            pre_hash=pre_hash,
        )

    working = deepcopy(pre)
    source_avoid: list[str] = []
    from newsagent_v2.article.input import evidence_text_blobs

    for blob in evidence_text_blobs(article_input):
        if blob and blob.strip():
            source_avoid.append(blob)
    avoid = tuple(list(offenders) + source_avoid[:8])
    unresolved: list[str] = []

    for sentence in offenders:
        attempts = independent_semantic_rewrite_attempts(
            sentence,
            packet,
            avoid_texts=avoid,
            max_attempts=MAX_STRUCTURAL_REWRITE_ATTEMPTS,
        )
        result.actions.append(
            RepairAction(
                kind="copyright_structural_attempts_executed",
                detail=(
                    f"structural_attempt_count={len(attempts)}; "
                    f"max_attempts={MAX_STRUCTURAL_REWRITE_ATTEMPTS}"
                ),
                before=sentence,
                after="",
            )
        )
        committed = False
        if not attempts:
            result.actions.append(
                RepairAction(
                    kind="copyright_structural_attempts_empty",
                    detail="no distinct structural realizations produced; escalate to semantic",
                    before=sentence,
                    after="",
                )
            )
        for rewritten, meta in attempts:
            ok = _validate_unit_replacement(
                original=working,
                replacement_sentence=rewritten,
                packet=packet,
                ledgers=ledgers,
                article_input=article_input,
                old_sentence=sentence,
            )
            if ok and rewritten.strip() and rewritten != sentence:
                working.article_body = working.article_body.replace(sentence, rewritten, 1)
                result.actions.append(
                    RepairAction(
                        kind="copyright_sentence_rewrite",
                        detail=(
                            "committed independent structural realization; "
                            f"strategy={meta.get('strategy')}; "
                            f"attempt={meta.get('attempt_index')}; "
                            f"retention={meta.get('repair_length_retention_ratio')}"
                        ),
                        before=sentence,
                        after=rewritten,
                    )
                )
                committed = True
                break
            result.actions.append(
                RepairAction(
                    kind="copyright_rewrite_attempt_rolled_back",
                    detail=(
                        "structural attempt failed validation; "
                        f"strategy={meta.get('strategy')}; trying alternate"
                    ),
                    before=sentence,
                    after=rewritten[:300],
                )
            )

        if not committed and writer is not None and MAX_SEMANTIC_SENTENCE_REWRITES >= 1:
            props = _authorized_atomic_cores(sentence, packet, avoid_texts=(sentence,))
            if not props:
                semantic, calls, err = None, 0, "no_authorized_props_for_semantic_rewrite"
            else:
                semantic, calls, err = generate_semantic_sentence_rewrite(
                    writer=writer,
                    packet=packet,
                    proposition_texts=props,
                )
            if err or not semantic:
                result.actions.append(
                    RepairAction(
                        kind="copyright_semantic_rewrite_failed",
                        detail=str(err or "empty"),
                        before=sentence,
                        after="",
                    )
                )
            else:
                ok = _validate_unit_replacement(
                    original=working,
                    replacement_sentence=semantic,
                    packet=packet,
                    ledgers=ledgers,
                    article_input=article_input,
                    old_sentence=sentence,
                )
                if ok:
                    working.article_body = working.article_body.replace(sentence, semantic, 1)
                    result.actions.append(
                        RepairAction(
                            kind="copyright_semantic_sentence_rewrite",
                            detail=f"committed semantic sentence rewrite; model_calls={calls}",
                            before=sentence,
                            after=semantic,
                        )
                    )
                    committed = True
                else:
                    result.actions.append(
                        RepairAction(
                            kind="copyright_semantic_rewrite_rolled_back",
                            detail="semantic rewrite failed validation; unit preserved",
                            before=sentence,
                            after=semantic[:300],
                        )
                    )

        if committed:
            continue
        result.actions.append(
            RepairAction(
                kind="copyright_rewrite_rolled_back",
                detail=(
                    "local structural+semantic realization failed validation; "
                    "unit preserved pending escalation"
                ),
                before=sentence,
                after="",
            )
        )
        unresolved.append(sentence)

    if not unresolved:
        result.article = working
        result.mode = MODE_LOCAL_REWRITE
        result.post_copyright_words = word_count(working.article_body)
        result.copyright_depth_retention_ratio = round(
            result.post_copyright_words / max(1, pre_words), 4
        )
        return result

    projected = _projected_words_after_drops(working.article_body, unresolved)
    drop_forbidden = pre_words >= target_min and projected < target_min

    # Clean-room last resort only when unresolved set is still substantial.
    if should_escalate_clean_room(working.article_body, unresolved):
        return _escalate_clean_room(
            pre,
            packet=packet,
            ledgers=ledgers,
            article_input=article_input,
            writer=writer,
            result=result,
            reason="SUBSTANTIAL_UNRESOLVED_AFTER_LOCAL_REPAIR",
            clean_room_budget=clean_room_budget,
            pre_hash=pre_hash,
            working_actions=result.actions,
        )

    if drop_forbidden:
        # Small local unresolved on a target-sized article: reject without destructive drop
        # and without whole-article clean-room.
        result.article = deepcopy(pre)
        result.rejected = True
        result.reject_reason = "copyright_local_unresolved_drop_forbidden"
        result.mode = MODE_REJECT
        result.post_copyright_words = pre_words
        result.copyright_depth_retention_ratio = 1.0
        result.depth_loss_origin = DEPTH_COPYRIGHT_REPAIR
        return result

    if projected >= target_min:
        staged = working.article_body
        for sentence in unresolved:
            if sentence in staged:
                staged = " ".join(staged.replace(sentence, "", 1).split())
                result.actions.append(
                    RepairAction(
                        kind="copyright_sentence_drop",
                        detail="drop allowed; projected article remains >= editorial target",
                        before=sentence,
                        after="",
                    )
                )
        working.article_body = staged
        result.article = working
        result.mode = MODE_DROP_ALLOWED
        result.post_copyright_words = word_count(working.article_body)
        result.copyright_depth_retention_ratio = round(
            result.post_copyright_words / max(1, pre_words), 4
        )
        return result

    if clean_room_budget > 0 and writer is not None and should_escalate_clean_room(
        working.article_body, unresolved
    ):
        return _escalate_clean_room(
            pre,
            packet=packet,
            ledgers=ledgers,
            article_input=article_input,
            writer=writer,
            result=result,
            reason="UNSAFE_DROP_ESCALATE",
            clean_room_budget=clean_room_budget,
            pre_hash=pre_hash,
            working_actions=result.actions,
        )

    result.article = deepcopy(pre)
    result.rejected = True
    result.reject_reason = "copyright_unresolved_no_safe_drop"
    result.mode = MODE_REJECT
    result.post_copyright_words = pre_words
    result.copyright_depth_retention_ratio = 1.0
    result.depth_loss_origin = DEPTH_COPYRIGHT_REPAIR
    return result


def _escalate_clean_room(
    pre: V4NativeArticle,
    *,
    packet: WriterEvidencePacket,
    ledgers: EvidenceLedgers,
    article_input: dict[str, Any],
    writer: Any | None,
    result: CopyrightRecoveryResult,
    reason: str,
    clean_room_budget: int,
    pre_hash: str,
    working_actions: list[RepairAction] | None = None,
) -> CopyrightRecoveryResult:
    del pre_hash  # reserved for diagnostics
    result.escalated_to_clean_room = True
    result.mode = MODE_CLEAN_ROOM
    if working_actions:
        result.actions = list(working_actions)
    result.actions.append(
        RepairAction(
            kind="clean_room_escalation",
            detail=reason,
            before="",
            after="",
        )
    )
    if writer is None or clean_room_budget < 1:
        result.article = deepcopy(pre)
        result.rejected = True
        result.reject_reason = f"clean_room_unavailable:{reason}"
        result.post_copyright_words = result.pre_copyright_words
        result.copyright_depth_retention_ratio = 1.0
        result.depth_loss_origin = DEPTH_COPYRIGHT_REPAIR
        result.mode = MODE_REJECT
        return result

    fresh, calls, error = generate_clean_room_article(writer=writer, packet=packet)
    result.clean_room_calls = calls
    if error or fresh is None:
        result.article = deepcopy(pre)
        result.rejected = True
        result.reject_reason = error or "clean_room_failed"
        result.post_copyright_words = result.pre_copyright_words
        result.copyright_depth_retention_ratio = 1.0
        result.depth_loss_origin = DEPTH_COPYRIGHT_REPAIR
        result.mode = MODE_REJECT
        result.actions.append(
            RepairAction(
                kind="clean_room_failed",
                detail=str(error or "clean_room_failed"),
            )
        )
        return result

    report = verify_v4_native(fresh, packet=packet, ledgers=ledgers)
    article = {
        "event_id": packet.event_id,
        "headline": fresh.headline,
        "dek": fresh.dek,
        "article_body": fresh.article_body,
        "claims": [],
        "quotes": [],
    }
    issues, _metrics = check_similarity(article, article_input)
    codes = {item.get("code") for item in issues if isinstance(item, dict)}
    copyright_bad = bool({"exact_phrase_overlap", "high_sentence_similarity"} & codes)
    if (not report.ok) or copyright_bad or word_count(fresh.article_body) < 1:
        result.article = deepcopy(pre)
        result.rejected = True
        result.reject_reason = "clean_room_failed_verification"
        result.post_copyright_words = result.pre_copyright_words
        result.copyright_depth_retention_ratio = 1.0
        result.depth_loss_origin = DEPTH_COPYRIGHT_REPAIR
        result.mode = MODE_REJECT
        result.actions.append(
            RepairAction(
                kind="clean_room_rejected",
                detail="clean-room output failed verification; candidate rejected without destructive salvage",
            )
        )
        return result

    # Atomic commit of entire clean-room article.
    result.article = fresh
    result.post_copyright_words = word_count(fresh.article_body)
    result.copyright_depth_retention_ratio = round(
        result.post_copyright_words / max(1, result.pre_copyright_words), 4
    )
    result.actions.append(
        RepairAction(
            kind="clean_room_committed",
            detail=f"atomic clean-room commit; words={result.post_copyright_words}",
            before="",
            after=fresh.article_body[:300],
        )
    )
    result.depth_loss_origin = None
    return result


def classify_depth_loss_origin(
    *,
    native_words: int,
    post_repair_words: int,
    target_min: int = EDITORIAL_TARGET_MIN_WORDS,
    copyright_recovery: CopyrightRecoveryResult | None = None,
) -> str:
    if native_words < target_min:
        return DEPTH_NATIVE_UNDERPRODUCTION
    if copyright_recovery and copyright_recovery.rejected:
        return DEPTH_COPYRIGHT_REPAIR
    if copyright_recovery and post_repair_words < target_min <= native_words:
        return DEPTH_COPYRIGHT_REPAIR
    if post_repair_words + 20 < native_words and post_repair_words < target_min:
        return DEPTH_FACTUAL_REPAIR
    if post_repair_words < target_min:
        return DEPTH_OTHER
    return DEPTH_NATIVE_UNDERPRODUCTION if native_words < target_min else DEPTH_OTHER
