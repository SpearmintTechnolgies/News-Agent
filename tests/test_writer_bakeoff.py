from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GEMINI_ARTICLE_FIRST,
    CANDIDATE_GROQ_ARTICLE_FIRST,
    EVENT_ID,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.freeze import freeze_top1_batch
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.normalize import normalize_article_first
from newsagent_v2.bench.writer_bakeoff.providers import estimated_live_calls, planned_candidates
from newsagent_v2.bench.writer_bakeoff.runner import dry_run, refuse_live, run_live
from newsagent_v2.bench.writer_bakeoff.score import pick_winner, score_result
from newsagent_v2.control import live as live_mod
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.image.providers.gemini import KEY_ENV as GEMINI_KEY_ENV

REPO = Path(__file__).resolve().parents[1]
BATCH = REPO / "output" / "approval" / SOURCE_BATCH_ID / "batch.json"


class WriterBakeoffTests(unittest.TestCase):
    def test_freeze_and_dry_run_use_same_top1_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / EVENT_ID
            manifest = freeze_top1_batch(batch_path=BATCH, dest_dir=dest, repo_root=REPO)
            self.assertEqual(manifest["source_batch_id"], SOURCE_BATCH_ID)
            self.assertEqual(manifest["event_id"], EVENT_ID)
            self.assertEqual(manifest["original_rank"], 2)
            self.assertTrue(manifest["backfill"])
            self.assertEqual(manifest["top1_proof"]["expected_count"], 1)
            self.assertEqual(manifest["top1_proof"]["viability_target"], 1)
            self.assertEqual(manifest["top1_proof"]["selected_event_ids"], [EVENT_ID])
            self.assertEqual(manifest["top1_proof"]["image_request_count"], 0)
            self.assertFalse(manifest["refetch"])
            fixture = load_fixture(dest)
            self.assertEqual(
                fixture["article_input"]["evidence_sufficiency"]["extracted_evidence_words"],
                348,
            )
            self.assertEqual(
                fixture["article_input"]["evidence_sufficiency"]["distinct_fact_count"],
                13,
            )
            result = dry_run(dest, environ={})
            self.assertTrue(result["dry_run"])
            self.assertEqual(result["live_http_calls"], 0)
            self.assertEqual(result["image_calls"], 0)
            self.assertEqual(result["telegram_calls"], 0)
            self.assertFalse(result["make_invoked"])
            self.assertEqual(result["historical_groq_structured_baseline"]["rendered_word_count"], 283)
            self.assertEqual(result["historical_groq_structured_baseline"]["hard_length"], "FAIL")
            self.assertEqual(result["historical_groq_structured_baseline"]["structure"], "PASS")
            self.assertEqual(result["historical_groq_structured_baseline"]["depth_criticals"], ["below_article_minimum_length"])
            self.assertIn("body_assertion_not_in_claims", result["historical_groq_structured_baseline"]["grounding_criticals"])
            self.assertEqual(result["winner_if_only_baseline"]["winner"], None)
            self.assertEqual(result["credentials_configured"][GEMINI_KEY_ENV], False)
            self.assertNotIn("gsk_", json.dumps(result))
            self.assertEqual(result["request_count_if_live"], 2)
            self.assertEqual(result["expected_next_live_calls"], 2)
            self.assertTrue(result["gemini_request_validation"]["ok"])
            self.assertFalse(result["gemini_request_validation"]["has_additionalProperties"])
            self.assertTrue(result["gemini_request_validation"]["previous_400_schema_had_additionalProperties"])
            self.assertTrue(result["groq_normalization_validation"]["ok"])
            self.assertEqual(result["qa_configuration"]["hard_minimum_words"], 350)
            self.assertTrue(result["qa_configuration"]["unchanged"])
            self.assertFalse(result["structured_current_rerun"])
            self.assertEqual(result["repair_calls"], 0)

    def test_gemini_configured_adds_two_live_calls(self) -> None:
        rows = planned_candidates()
        self.assertEqual(estimated_live_calls(), 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["candidate_id"], CANDIDATE_GEMINI_ARTICLE_FIRST)
        self.assertEqual(rows[1]["candidate_id"], CANDIDATE_GROQ_ARTICLE_FIRST)
        self.assertEqual(rows[0]["mode"], "article_first_grounding_map")
        self.assertEqual(rows[1]["mode"], "article_first_grounding_map")
        self.assertEqual(rows[0]["repair_calls"], 0)

    def test_article_first_normalize_builds_sections(self) -> None:
        parsed = {
            "event_id": EVENT_ID,
            "headline": "Senate Republicans release revised CLARITY Act text",
            "dek": "A 635-page proposal is offered before a procedural vote.",
            "article_body": (
                "Senate Republicans released revised CLARITY Act text as a final offer to Democrats.\n\n"
                "The 635-page proposal includes ethics provisions agreed by President Donald Trump."
            ),
            "category": "regulatory",
            "seo_title": "Senate Republicans release revised CLARITY Act text",
            "meta_description": "Republicans offered a 635-page CLARITY Act revision before a procedural vote.",
            "slug": "senate-republicans-revised-clarity-act",
            "entities": [{"name": "Cynthia Lummis", "type": "person"}],
            "keywords": ["CLARITY Act"],
            "claims": [
                {
                    "id": "c1",
                    "text": "Senate Republicans released revised CLARITY Act text as a final offer to Democrats.",
                    "claim_type": "fact",
                    "evidence_ids": ["event-005-e01"],
                }
            ],
            "quotes": [],
            "paragraph_maps": [
                {"paragraph_index": 0, "claim_ids": ["c1"]},
                {"paragraph_index": 1, "claim_ids": ["c1"]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / EVENT_ID
            freeze_top1_batch(batch_path=BATCH, dest_dir=dest, repo_root=REPO)
            fixture = load_fixture(dest)
            out = normalize_article_first(
                parsed,
                event_id=EVENT_ID,
                story={"event_id": EVENT_ID, "article_input": fixture["article_input"]},
            )
            self.assertEqual(len(out["article"]["article_sections"]), 2)
            self.assertTrue(out["article"]["article_body"])

    def test_live_flag_is_refused_here(self) -> None:
        payload = refuse_live()
        self.assertTrue(payload["refused"])
        self.assertEqual(payload["live_http_calls"], 0)

    def test_run_live_without_keys_makes_zero_http(self) -> None:
        result = run_live(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID, environ={})
        self.assertTrue(result.get("refused"))
        self.assertEqual(result["live_http_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])

    def test_missing_article_is_structure_fail(self) -> None:
        fixture = load_fixture(REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID)
        score = score_result(
            provider="groq",
            model="openai/gpt-oss-120b",
            mode="article_first_grounding_map",
            qa=None,
            article=None,
            article_input=fixture["article_input"],
            http_status=400,
            latency_ms=10,
            retries=0,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            estimated_list_price_usd=None,
            provider_reported_cost_usd=None,
            candidate_id="missing_article",
        )
        self.assertEqual(score["structure"], "FAIL")
        self.assertFalse(score["winner_eligible"])

    def test_winner_requires_qa_pass_not_merely_length(self) -> None:
        scores = [
            {
                "candidate_id": "long_but_ungrounded",
                "winner_eligible": False,
                "rendered_word_count": 900,
                "claim_coverage": 1.0,
            }
        ]
        self.assertEqual(pick_winner(scores)["winner"], None)

    def test_live_make_and_isolation_untouched(self) -> None:
        self.assertEqual(MAKE_STORY_COUNT, 1)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        live_src = inspect.getsource(live_mod)
        self.assertIn("target=MAKE_STORY_COUNT", live_src)
        self.assertNotIn("writer_bakeoff", live_src)
        self.assertTrue(wordpress_disabled_publish()["wordpress_disabled"])
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/bench/writer_bakeoff/runner.py",
            "src/newsagent_v2/bench/writer_bakeoff/providers.py",
            "src/newsagent_v2/bench/writer_bakeoff/execute.py",
            "src/newsagent_v2/bench/writer_bakeoff/llama_33_audition.py",
            "src/newsagent_v2/bench/writer_bakeoff/groq_qwen_38_audition.py",
            "src/newsagent_v2/bench/writer_bakeoff/kimi_safety.py",
            "src/newsagent_v2/bench/writer_bakeoff/kimi_k25_audition.py",
            "src/newsagent_v2/bench/writer_bakeoff/ledger_replay.py",
            "src/newsagent_v2/bench/writer_bakeoff/gemini_ledger_first_audition.py",
            "src/newsagent_v2/bench/writer_bakeoff/groq_qwen_38_ledger_first_audition.py",
            "src/newsagent_v2/article/writer/bedrock_mantle.py",
            "src/newsagent_v2/article/writer/normalize.py",
            "src/newsagent_v2/article/writer/grounding_resolve.py",
            "src/newsagent_v2/control/live.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


