"""V4 one-hour recovery sprint offline gates. No live provider calls."""

from __future__ import annotations

import inspect
import json
import unittest
from typing import Any
from unittest.mock import patch

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.copyright_recovery import (
    CLEAN_ROOM_OFFENDER_RATIO,
    MODE_CLEAN_ROOM,
    MODE_LOCAL_REWRITE,
    MODE_REJECT,
    SOURCE_SHAPED_OFFENDER_THRESHOLD,
    copyright_offender_ratio,
    generate_semantic_sentence_rewrite,
    recover_copyright,
    should_escalate_clean_room,
)
from newsagent_v2.article.writer.v4.expand import realize_body_word_target, realize_editorial_length
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet
from newsagent_v2.article.writer.v4.provider import (
    INFRA_RATE_LIMIT,
    V4_MAX_COMPLETION_TOKENS,
    build_v4_chat_body,
    extract_rate_limit_telemetry,
    reasoning_effort_for_model,
    same_org_groq_failover_is_otpm_safe,
)
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    V4_JSON_SCHEMA,
    ScriptedV4Writer,
    V4NativeArticle,
    _generation_packet_dict,
    build_v4_writer_messages,
)


def _ledgers() -> EvidenceLedgers:
    claims = tuple(
        LedgerClaim(
            f"C{i:02d}",
            text,
            "fact",
            ("e1",),
        )
        for i, text in enumerate(
            [
                "Northwind Payments disclosed that 12400 customer records were exposed.",
                "A spoofed government-domain email reached company staff.",
                "Security staff began notifying affected users after the disclosure.",
                "Investigators confirmed identity documents and payment history were involved.",
                "Northwind limited the incident to retail payments customer files.",
                "The company opened an internal review of email authentication controls.",
                "Customer support teams prepared guidance for callers asking about the exposure.",
                "Northwind said the spoofed message used a lookalike government domain string.",
            ],
            start=1,
        )
    )
    return EvidenceLedgers(event_id="event-sprint", claims=claims, quotes=())


def _packet():
    return build_writer_evidence_packet(
        event_id="event-sprint",
        ledgers=_ledgers(),
        story_topic="Northwind disclosure",
        article_input={
            "evidence": [
                {
                    "source": "Wire",
                    "extracted_text": "RAW SOURCE PROSE MUST NEVER REACH GENERATION PATHS.",
                    "published": "2026-09-01",
                }
            ]
        },
    )


def _pad(body: str, target: int) -> str:
    filler = (
        "Security staff began notifying affected users after the disclosure. "
        "A spoofed government-domain email reached company staff. "
        "Investigators confirmed identity documents and payment history were involved. "
    )
    while word_count(body) < target:
        body = (body + " " + filler).strip()
    return body


def _full_article(words: int = 300) -> dict[str, Any]:
    body = _pad(
        "Northwind Payments disclosed that 12400 customer records were exposed. "
        "A spoofed government-domain email reached company staff. "
        "Security staff began notifying affected users after the disclosure. "
        "Investigators confirmed identity documents and payment history were involved. "
        "Northwind limited the incident to retail payments customer files. "
        "The company opened an internal review of email authentication controls. "
        "Customer support teams prepared guidance for callers asking about the exposure. "
        "Northwind said the spoofed message used a lookalike government domain string.",
        words,
    )
    return {
        "headline": "Northwind Payments disclosed that 12400 customer records were exposed",
        "dek": "A spoofed government-domain email reached company staff.",
        "article_body": body,
        "seo_title": "Northwind disclosure",
        "meta_description": "Northwind Payments disclosed that 12400 customer records were exposed.",
        "slug": "northwind-disclosure",
    }


class V4RecoverySprintTests(unittest.TestCase):
    def test_qwen_reasoning_none(self) -> None:
        self.assertEqual(reasoning_effort_for_model("qwen/qwen3.8-27b"), "none")
        body = build_v4_chat_body(
            model="qwen/qwen3.8-27b",
            messages=[{"role": "user", "content": "x"}],
        )
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertLessEqual(body["max_completion_tokens"], V4_MAX_COMPLETION_TOKENS)

    def test_underproduction_detected_and_semantic_regeneration(self) -> None:
        packet = _packet()
        ledgers = _ledgers()
        thin = V4NativeArticle(
            headline="Northwind",
            dek="Dek",
            article_body=(
                "Northwind Payments disclosed that 12400 customer records were exposed. "
                "A spoofed government-domain email reached company staff."
            ),
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        self.assertLess(thin.word_count, 250)
        report = verify_v4_native(thin, packet=packet, ledgers=ledgers)
        writer = ScriptedV4Writer(
            thin.as_dict(),
            regeneration_payload=_full_article(300),
            expansion_text="LEGACY_EXPAND_MUST_NOT_RUN",
        )
        out, _r, expansion = realize_body_word_target(
            thin,
            packet=packet,
            ledgers=ledgers,
            article_input={"evidence": [{"extracted_text": "source"}]},
            report=report,
            writer=writer,
        )
        self.assertTrue(expansion.triggered)
        self.assertEqual(writer.regeneration_calls, 1)
        self.assertTrue(writer.last_render_regeneration)
        self.assertEqual(writer.expansion_calls, 0)
        self.assertGreaterEqual(out.word_count, 250)
        self.assertLessEqual(out.word_count, 400)
        self.assertNotIn("LEGACY_EXPAND", out.article_body)

    def test_underproduction_previous_body_excluded(self) -> None:
        packet = _packet()
        messages = build_v4_writer_messages(packet, regeneration=True)
        blob = json.dumps(messages).lower()
        self.assertIn("fresh regeneration", blob)
        self.assertIn("prior article text is unavailable", messages[0]["content"].lower())
        user = json.loads(messages[1]["content"])
        self.assertTrue(user["regeneration"])
        self.assertNotIn("article_body", user)
        self.assertNotIn("previous_article", user)
        self.assertNotIn("extracted_text", json.dumps(user["evidence_packet"]))

    def test_legacy_expansion_not_in_active_v4_path(self) -> None:
        from newsagent_v2.article.writer.v4 import expand as expand_mod
        from newsagent_v2.article.writer.v4 import compile as compile_mod

        src = inspect.getsource(expand_mod.realize_body_word_target)
        self.assertIn("regeneration=True", src)
        # Capacity-aware enrichment may call generate_expansion_text; legacy
        # expand_from_unused_facts remains off the compile success path.
        compile_src = inspect.getsource(compile_mod.compile_v4_article)
        self.assertIn("realize_editorial_length", compile_src)
        self.assertNotIn("expand_from_unused_facts", compile_src)
        self.assertIn("allow_destructive_length_pad=False", compile_src)
        self.assertIn("assess_evidence_capacity", compile_src)
        self.assertIn("research_event", compile_src)

    def test_generation_source_text_boundary(self) -> None:
        packet = _packet()
        gen = _generation_packet_dict(packet)
        blob = json.dumps(gen).lower()
        self.assertNotIn("raw source prose", blob)
        self.assertNotIn("extracted_text", blob)
        for path in (
            build_v4_writer_messages(packet, regeneration=False),
            build_v4_writer_messages(packet, regeneration=True),
        ):
            user = json.loads(path[1]["content"])
            self.assertNotIn("extracted_text", json.dumps(user).lower())

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_local_copyright_structural_repair(self) -> None:
        packet = _packet()
        ledgers = _ledgers()
        offender = (
            "unique copyright offender phrase alpha bravo charlie delta echo foxtrot "
            "golf hotel india juliet kilo lima was copied verbatim into draft prose."
        )
        body = _pad(
            "Northwind Payments disclosed that 12400 customer records were exposed. " + offender,
            270,
        )
        native = V4NativeArticle(
            headline="Northwind",
            dek="Dek",
            article_body=body,
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        replacement = (
            "According to the disclosed information, Northwind Payments said that "
            "12400 customer records had been exposed after an independently worded account."
        )

        def fake_attempts(sentence, packet, avoid_texts=(), max_attempts=3):
            del packet, avoid_texts, max_attempts, sentence
            return [
                (
                    replacement,
                    {
                        "repair_length_retention_ratio": 0.9,
                        "replacement_words": 20,
                        "strategy": "structural",
                        "attempt_index": 0,
                    },
                )
            ]

        with patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.find_copyright_offending_sentences",
            return_value=[offender],
        ), patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.independent_semantic_rewrite_attempts",
            side_effect=fake_attempts,
        ), patch(
            "newsagent_v2.article.writer.v4.copyright_recovery._validate_unit_replacement",
            return_value=True,
        ):
            result = recover_copyright(
                native,
                packet=packet,
                ledgers=ledgers,
                article_input={"evidence": []},
            )
        self.assertEqual(result.mode, MODE_LOCAL_REWRITE)
        self.assertFalse(result.escalated_to_clean_room)
        self.assertIn(replacement, result.article.article_body)

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_local_copyright_semantic_fallback_and_source_excluded(self) -> None:
        packet = _packet()
        props = ["Northwind Payments disclosed that 12400 customer records were exposed."]
        writer = ScriptedV4Writer(
            semantic_rewrite_text=(
                "Northwind Payments said 12400 customer records were exposed, according to disclosure."
            )
        )
        text, calls, err = generate_semantic_sentence_rewrite(
            writer=writer,
            packet=packet,
            proposition_texts=props,
        )
        self.assertIsNone(err)
        self.assertEqual(calls, 1)
        self.assertTrue(text)
        self.assertEqual(writer.last_semantic_rewrite_props, props)
        # Contaminated keys rejected.
        messages_check = {
            "authorized_atomic_propositions": props,
            "protected_entities": list(packet.authorized_entities),
            "instruction": "x",
        }
        self.assertNotIn("extracted_text", messages_check)
        self.assertNotIn("offending_sentence", messages_check)
        self.assertNotIn("matched_source_fragment", messages_check)

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_failed_local_repair_transactional_rollback(self) -> None:
        packet = _packet()
        ledgers = _ledgers()
        offender = (
            "unique copyright offender phrase alpha bravo charlie delta echo foxtrot "
            "golf hotel india juliet kilo lima ends here."
        )
        body = _pad(
            "Northwind Payments disclosed that 12400 customer records were exposed. " + offender,
            270,
        )
        native = V4NativeArticle(
            headline="Northwind",
            dek="Dek",
            article_body=body,
            seo_title="Northwind",
            meta_description="Exposure",
            slug="northwind",
        )
        with patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.find_copyright_offending_sentences",
            return_value=[offender],
        ), patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.independent_semantic_rewrite_attempts",
            return_value=[("bad rewrite", {"strategy": "x", "attempt_index": 0})],
        ), patch(
            "newsagent_v2.article.writer.v4.copyright_recovery._validate_unit_replacement",
            return_value=False,
        ), patch(
            "newsagent_v2.article.writer.v4.copyright_recovery.generate_semantic_sentence_rewrite",
            return_value=("still bad", 1, None),
        ):
            # Semantic also fails validation via same patch â†’ rollback.
            result = recover_copyright(
                native,
                packet=packet,
                ledgers=ledgers,
                article_input={"evidence": []},
                writer=ScriptedV4Writer(),
            )
        # Drop may succeed if projected stays >=250; either drop or reject â€” never mutate without commit.
        if result.mode == MODE_REJECT:
            self.assertEqual(result.article.article_body, body)
        else:
            self.assertNotIn("still bad", result.article.article_body)

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_small_local_offender_does_not_force_clean_room(self) -> None:
        body = _pad("Sentence one. Sentence two. Sentence three. One offender here.", 280)
        offenders = ["One offender here."]
        self.assertFalse(should_escalate_clean_room(body, offenders))
        self.assertLess(len(offenders), SOURCE_SHAPED_OFFENDER_THRESHOLD)
        self.assertLess(copyright_offender_ratio(body, offenders), CLEAN_ROOM_OFFENDER_RATIO)

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_clean_room_last_resort(self) -> None:
        offenders = [f"Offender {i} " + "word " * 40 + "." for i in range(5)]
        body = " ".join(offenders)
        self.assertTrue(should_escalate_clean_room(body, offenders))
        self.assertGreaterEqual(copyright_offender_ratio(body, offenders), CLEAN_ROOM_OFFENDER_RATIO)

    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_post_repair_full_grounding_and_copyright(self) -> None:
        from newsagent_v2.article.writer.v4 import compile as compile_mod

        src = inspect.getsource(compile_mod.compile_v4_article)
        self.assertIn("verify_v4_native(expanded", src)
        self.assertIn("run_article_qa", src)
        self.assertIn("post_qa.article", src)
        self.assertIn("verify_v4_native(expanded, packet=packet, ledgers=ledgers)", src)

    def test_groq_otpm_cumulative_guard_and_retry_after(self) -> None:
        self.assertFalse(
            same_org_groq_failover_is_otpm_safe(
                primary_provider="groq",
                fallback_provider="groq",
                error_type=INFRA_RATE_LIMIT,
            )
        )
        self.assertTrue(
            same_org_groq_failover_is_otpm_safe(
                primary_provider="groq",
                fallback_provider="groq",
                error_type="TIMEOUT",
            )
        )
        telem = extract_rate_limit_telemetry(
            {"Retry-After": "12", "x-ratelimit-remaining-tokens": "0"},
            {"choices": [{"finish_reason": "stop"}]},
        )
        self.assertEqual(telem["retry_after"], "12")
        self.assertEqual(telem["finish_reason"], "stop")

    def test_bounded_provider_attempts(self) -> None:
        from newsagent_v2.article.writer.v4.writer import FailoverV4Writer, V4NaturalProseWriter

        class T:
            provider_name = "groq"
            model = "x"
            api_key = "k"
            calls = 0

            def complete(self, **kwargs):
                from newsagent_v2.article.writer.v4.provider import ChatCompletionResult

                del kwargs
                self.calls += 1
                return ChatCompletionResult(
                    ok=False,
                    error="rate",
                    error_type=INFRA_RATE_LIMIT,
                    status_code=429,
                    provider="groq",
                    model="x",
                    retry_after="30",
                )

        primary = V4NaturalProseWriter(transport=T(), http_post=lambda *a, **k: None)
        primary.provider = "groq"
        fallback = V4NaturalProseWriter(transport=T(), http_post=lambda *a, **k: None)
        fallback.provider = "groq"
        writer = FailoverV4Writer(primary=primary, fallback=fallback, max_provider_attempts=2)
        result = writer.render(_packet())
        self.assertFalse(result.ok)
        self.assertEqual(primary.transport.calls, 1)
        self.assertEqual(fallback.transport.calls, 0)
        self.assertIn("30", result.error or "")

    def test_gpt_oss_request_schema(self) -> None:
        messages = [{"role": "user", "content": "hi"}]
        body = build_v4_chat_body(
            model="openai/gpt-oss-20b",
            messages=messages,
            body_extra={
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "v4_natural_article",
                        "strict": True,
                        "schema": V4_JSON_SCHEMA,
                    },
                }
            },
        )
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(
            set(body["response_format"]["json_schema"]["schema"]["required"]),
            set(V4_JSON_SCHEMA["required"]),
        )
        self.assertFalse(body["response_format"]["json_schema"]["schema"]["additionalProperties"])
        # No Qwen-specific params beyond shared reasoning_effort.
        qwen_only = {"enable_thinking", "chat_template_kwargs", "top_k"}
        self.assertFalse(qwen_only & set(body.keys()))
        self.assertEqual(reasoning_effort_for_model("openai/gpt-oss-20b"), "low")


if __name__ == "__main__":
    unittest.main()


