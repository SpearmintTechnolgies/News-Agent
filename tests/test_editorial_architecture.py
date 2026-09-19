from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.controlled.editorial_compile import (
    compile_editorial_article,
    unused_authorized_fact_ids,
)
from newsagent_v2.article.writer.controlled.editorial_dossier import (
    EDITORIAL_SYSTEM_PROMPT,
    build_editorial_dossier,
)
from newsagent_v2.article.writer.controlled.kimi_k25_renderer import (
    KimiK25ProseRenderer,
    kimi_fallback_permitted,
    should_fallback_to_kimi,
)
from newsagent_v2.article.writer.controlled.paid_qwen_renderer import PaidQwenProseRenderer
from newsagent_v2.article.writer.controlled.paragraph import validate_paragraph
from newsagent_v2.article.writer.controlled.plan import plan_article
from newsagent_v2.article.writer.controlled.renderer import FakeProseRenderer, RenderedParagraph, RendererResult
from newsagent_v2.article.writer.controlled.research import research_story
from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim, verbalize_semantic_fact
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.qwen_vllm import BASE_ENV, KEY_ENV, QwenVLLMConfig, credential_presence
from newsagent_v2.bench.writer_bakeoff.credentials import load_environ
from newsagent_v2.control import live as live_mod
from tests.test_controlled_writer_v3 import _rich_story
import inspect

REPO = Path(__file__).resolve().parents[1]


class ShortThenFullRenderer:
    renderer_name = "short_then_full"

    def __init__(self) -> None:
        self.calls = 0

    def render(self, plan, ledgers) -> RendererResult:
        self.calls += 1
        claims = ledgers.claim_by_id()
        if self.calls == 1:
            paragraphs = []
            for para in plan.paragraph_plans:
                cid = para.required_claim_ids[0] if para.required_claim_ids else ""
                claim = claims.get(cid)
                text = verbalize_semantic_fact(semantic_fact_from_claim(claim)) if claim else "Northwind disclosed a review."
                paragraphs.append(RenderedParagraph(paragraph_id=para.paragraph_id, text=text))
            return RendererResult(
                ok=True,
                paragraphs=paragraphs,
                headline_text="Northwind Payments disclosed a review",
                dek_text="The firm said it is reviewing affected records.",
            )
        return FakeProseRenderer().render(plan, ledgers)


class DummyQwenResponse:
    status_code = 200

    def __init__(self, native: dict) -> None:
        self._native = native

    def json(self) -> dict:
        return {
            "choices": [{"message": {"content": json.dumps(self._native)}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        }


class EditorialArchitectureTests(unittest.TestCase):
    def test_dossier_omits_raw_claim_text(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        dossier = build_editorial_dossier(plan, ledgers)
        blob = json.dumps(dossier.as_dict())
        self.assertTrue(dossier.facts)
        self.assertIn("400â€“550", EDITORIAL_SYSTEM_PROMPT)
        for claim in ledgers.claims:
            if len(claim.text.split()) >= 18:
                self.assertNotIn(claim.text, blob)
        self.assertFalse(dossier.as_dict()["invent_facts"])

    def test_research_uses_retrieved_text_only(self) -> None:
        story = _rich_story()

        def enrich(raw, **_kwargs):
            return raw

        researched = research_story(story, enrich_fn=enrich)
        self.assertFalse(researched["model_generated_evidence"])
        self.assertTrue(researched["discovery_is_not_writing_evidence"])
        self.assertGreaterEqual(researched["claim_count"], 1)
        self.assertEqual(
            [row.claim_id for row in researched["ledgers"].claims],
            [row.claim_id for row in build_evidence_ledgers(story["article_input"]).claims],
        )

    def test_paid_qwen_mocked_http_not_groq(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        native = {
            "event_id": plan.event_id,
            "headline": "Northwind Payments disclosed spoofed-email customer-file review",
            "dek": "The firm said it is reviewing affected retail customer records.",
            "paragraphs": [
                {
                    "paragraph_id": para.paragraph_id,
                    "sentences": [
                        {
                            "sentence_id": f"{para.paragraph_id}-s1",
                            "text": "Northwind Payments disclosed the records after the spoofed email review.",
                            "fact_ids_used": [para.allowed_claim_ids[0]],
                            "quote_ids_used": [],
                        }
                    ],
                }
                for para in plan.paragraph_plans
            ],
            "seo_title": "Northwind Payments disclosed spoofed-email customer-file review",
            "meta_description": "Northwind Payments disclosed a review of spoofed-email customer records.",
            "slug": "northwind-payments-disclosed-review",
            "entities": [{"name": "Northwind Payments", "type": "org"}],
            "keywords": ["Northwind"],
        }
        calls: list[str] = []

        def http_post(url, *, method, headers, json_body, timeout):
            del headers, timeout
            calls.append(url)
            self.assertEqual(method, "POST")
            self.assertNotIn("api.groq.com", url)
            self.assertIn("chat/completions", url)
            self.assertIn("CoinNetwork staff writer", json_body["messages"][0]["content"])
            return DummyQwenResponse(native)

        cfg = QwenVLLMConfig(api_key="test-not-a-real-key", base_url="https://example.invalid/v1")
        renderer = PaidQwenProseRenderer(config=cfg, http_post=http_post)
        result = renderer.render(plan, ledgers)
        self.assertTrue(result.ok)
        self.assertEqual(renderer.generation_calls, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(renderer.telemetry["provider"], "paid_qwen")

    def test_kimi_default_makes_zero_http(self) -> None:
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        renderer = KimiK25ProseRenderer(environ={})
        result = renderer.render(plan, ledgers)
        self.assertFalse(result.ok)
        self.assertEqual(renderer.generation_calls, 0)
        self.assertFalse(renderer.telemetry["real_http_attempted"])
        self.assertFalse(should_fallback_to_kimi(failure_class="COPYRIGHT_SIMILARITY_FAILED", environ={"NEWSAGENT_V2_KIMI_WRITER_FALLBACK": "1"}))
        self.assertFalse(should_fallback_to_kimi(failure_class="WRITER_PROVIDER_ERROR", environ={}))
        self.assertTrue(should_fallback_to_kimi(failure_class="WRITER_PROVIDER_ERROR", environ={"NEWSAGENT_V2_KIMI_WRITER_FALLBACK": "1"}))
        self.assertFalse(kimi_fallback_permitted({}))

    def test_assertions_map_and_quarantine(self) -> None:
        story = _rich_story()
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        para = plan.paragraph_plans[0]
        good = ledgers.claim_by_id()[para.required_claim_ids[0]].text
        validated = validate_paragraph(
            RenderedParagraph(paragraph_id=para.paragraph_id, text=good),
            para,
            ledgers,
        )
        self.assertTrue(validated.units)
        self.assertTrue(any(unit.claim_ids for unit in validated.units if unit.retained) or validated.mapped_claim_ids)

    def test_supplemental_uses_unused_facts_once(self) -> None:
        story = _rich_story()
        compiled = compile_editorial_article(story, renderer=FakeProseRenderer())
        if compiled.ok:
            self.assertIn("supplemental=0", compiled.notes)
        ledgers = build_evidence_ledgers(story["article_input"])
        plan = plan_article(ledgers, story["article_input"])
        from newsagent_v2.article.writer.controlled.paragraph import ParagraphValidation

        partial = [
            ParagraphValidation(
                paragraph_id=plan.paragraph_plans[0].paragraph_id,
                ok=True,
                text="ok",
                mapped_claim_ids=[plan.selected_claim_ids[0]],
                outcome="PARTIALLY_RETAINED",
            )
        ]
        unused = unused_authorized_fact_ids(plan, partial)
        self.assertTrue(set(unused).isdisjoint({plan.selected_claim_ids[0]}))
        self.assertTrue(set(unused) <= set(plan.selected_claim_ids))

    def test_make_uses_paid_qwen_not_groq_oss(self) -> None:
        src = inspect.getsource(live_mod.build_live_pipeline)
        recovery = (REPO / "src/newsagent_v2/control/make_recovery.py").read_text(encoding="utf-8")
        self.assertIn("run_v4_final_pipeline", src)
        self.assertIn("PaidQwenProseRenderer", recovery)
        self.assertNotIn("GroqGptOss20bProseRenderer", recovery)
        self.assertNotIn("openai/gpt-oss-20b", recovery)

    def test_qwen_credentials_inspected_without_secrets(self) -> None:
        env = load_environ(REPO)
        flags = credential_presence(env)
        self.assertIn("qwen_api_key_present", flags)
        self.assertIn("qwen_base_url_present", flags)
        blob = json.dumps(flags)
        secret = str(env.get(KEY_ENV) or "")
        base = str(env.get(BASE_ENV) or "")
        if secret:
            self.assertNotIn(secret, blob)
        if base:
            self.assertNotIn(base, blob)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)


if __name__ == "__main__":
    unittest.main()


