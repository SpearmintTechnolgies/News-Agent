from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.article.contract import REQUIRED_CLAIM_FIELDS
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.writer.canonical import CANONICAL_SCHEMA_VERSION
from newsagent_v2.article.writer.normalize import (
    NORMALIZATION_FAILED,
    CODE_FOREIGN_EVIDENCE,
    CODE_MALFORMED,
    CODE_MISSING_PARAGRAPH_MAP,
    CODE_UNKNOWN_EVIDENCE,
    normalize_provider_result,
)
from newsagent_v2.article.writer.sample import sample_article_first_native
from newsagent_v2.article.writer.schema import (
    failing_gemini_schema_from_groq_article_contract,
    gemini_article_first_schema,
    groq_article_first_json_schema,
    schema_contains_additional_properties,
)
from newsagent_v2.article.writer.validate import validate_gemini_response_schema
from newsagent_v2.batch.contract import MAKE_STORY_COUNT
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.bench.writer_bakeoff import __main__ as bakeoff_main
from newsagent_v2.bench.writer_bakeoff.contract import (
    CANDIDATE_GEMINI_36_ARTICLE_FIRST,
    EVENT_ID,
    GEMINI_NEXT_TEXT_MODEL,
    SOURCE_BATCH_ID,
)
from newsagent_v2.bench.writer_bakeoff.credentials import GEMINI_KEY_ENV
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.freeze import freeze_top1_batch, sha256_payload
from newsagent_v2.bench.writer_bakeoff.providers import next_writer_candidate
from newsagent_v2.bench.writer_bakeoff.runner import dry_run_one, run_one_writer
from newsagent_v2.control import live as live_mod
from newsagent_v2.image.providers.gemini import generate_content_url

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID


def _story(other=None):
    fixture = load_fixture(FIXTURE)
    pack = {
        "event_id": EVENT_ID,
        "article_input": fixture["article_input"],
    }
    if other is not None:
        pack["other_article_inputs"] = [other]
    return pack


def _native(**overrides):
    payload = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
    payload.update(overrides)
    return payload


class WriterAdapterTests(unittest.TestCase):
    def test_gemini_schema_has_no_additional_properties(self) -> None:
        current = gemini_article_first_schema()
        previous = failing_gemini_schema_from_groq_article_contract()
        self.assertTrue(schema_contains_additional_properties(previous))
        self.assertFalse(schema_contains_additional_properties(current))
        self.assertEqual(validate_gemini_response_schema(current)["violations"], [])
        self.assertTrue(validate_gemini_response_schema(current)["ok"])
        dumped = json.dumps(current)
        self.assertNotIn("additionalProperties", dumped)

    def test_gemini_native_fixture_parses_and_normalizes(self) -> None:
        native = _native()
        result = normalize_provider_result(native, _story())
        self.assertTrue(result["ok"])
        article = result["article"]
        self.assertEqual(article["schema_version"], CANONICAL_SCHEMA_VERSION)
        self.assertEqual(article["claims"][0]["claim_id"], "c1")
        self.assertNotIn("id", article["claims"][0])
        self.assertEqual(len(article["article_sections"]), 2)

    def test_groq_id_becomes_canonical_claim_id(self) -> None:
        native = _native()
        self.assertEqual(native["claims"][0]["id"], "c1")
        self.assertNotIn("claim_id", native["claims"][0])
        article = normalize_provider_result(native, _story())["article"]
        self.assertEqual(article["claims"][0]["claim_id"], "c1")
        self.assertEqual([claim["claim_id"] for claim in article["claims"]], ["c1", "c2"])

    def test_malformed_provider_result_fails(self) -> None:
        result = normalize_provider_result(["not", "an", "object"], _story())
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure"]["code"], NORMALIZATION_FAILED)
        self.assertEqual(result["failure"]["detail_code"], CODE_MALFORMED)

    def test_unknown_evidence_id_fails(self) -> None:
        native = _native()
        native["claims"][0]["evidence_ids"] = ["e99"]
        result = normalize_provider_result(native, _story())
        self.assertEqual(result["failure"]["code"], NORMALIZATION_FAILED)
        self.assertEqual(result["failure"]["detail_code"], CODE_UNKNOWN_EVIDENCE)

    def test_foreign_evidence_id_fails(self) -> None:
        other = {
            "event_id": "event-039",
            "evidence_units": [
                {
                    "evidence_id": "event-039-e01",
                    "url": "https://example.com/other",
                    "source": "Other",
                    "text": "other story",
                }
            ],
        }
        native = _native()
        native["claims"][0]["evidence_ids"] = ["event-039-e01"]
        result = normalize_provider_result(native, _story(other))
        self.assertEqual(result["failure"]["detail_code"], CODE_FOREIGN_EVIDENCE)

    def test_missing_paragraph_mapping_fails(self) -> None:
        native = _native()
        native["paragraph_maps"] = [native["paragraph_maps"][0]]
        result = normalize_provider_result(native, _story())
        self.assertEqual(result["failure"]["detail_code"], CODE_MISSING_PARAGRAPH_MAP)

    def test_normalization_does_not_invent_claims_or_evidence(self) -> None:
        native = _native()
        result = normalize_provider_result(native, _story())
        article = result["article"]
        self.assertEqual(len(article["claims"]), len(native["claims"]))
        self.assertEqual(
            {claim["text"] for claim in article["claims"]},
            {claim["text"] for claim in native["claims"]},
        )
        used_ids = []
        for claim in article["claims"]:
            used_ids.extend(claim["evidence_ids"])
        self.assertEqual(set(used_ids), {"event-005-e01"})

    def test_canonical_article_feeds_existing_qa_unchanged(self) -> None:
        article = normalize_provider_result(_native(), _story())["article"]
        qa = run_article_qa(article, _story()["article_input"], article_mode="normal")
        self.assertIn("publishable", qa)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        codes = [item.get("code") for item in qa.get("critical_failures") or []]
        self.assertIn("below_article_minimum_length", codes)

    def test_qa_failure_blocks_image_and_pass_would_allow(self) -> None:
        src = inspect.getsource(run_top5_batch)
        self.assertIn('if row.get("qa_publishable")', src)
        self.assertIn("image_jobs", src)
        fixture = load_fixture(FIXTURE)
        qa = run_article_qa(
            fixture["baseline_article"],
            fixture["article_input"],
            article_mode="normal",
        )
        self.assertFalse(qa["publishable"])

        def image_jobs_would_run(qa_publishable: bool) -> bool:
            return bool(qa_publishable)

        self.assertFalse(image_jobs_would_run(qa["publishable"]))
        self.assertTrue(image_jobs_would_run(True))

    def test_qa_does_not_require_provider_specific_fields(self) -> None:
        self.assertEqual(REQUIRED_CLAIM_FIELDS[0], "claim_id")
        self.assertNotIn("id", REQUIRED_CLAIM_FIELDS)
        self.assertNotIn("additionalProperties", inspect.getsource(run_article_qa))
        article = normalize_provider_result(_native(), _story())["article"]
        for claim in article["claims"]:
            self.assertIn("claim_id", claim)
            self.assertIn("evidence_refs", claim)

    def test_live_make_unchanged(self) -> None:
        self.assertEqual(MAKE_STORY_COUNT, 1)
        live_src = inspect.getsource(live_mod)
        self.assertNotIn("writer_bakeoff", live_src)
        self.assertIn("run_v4_final_pipeline", live_src)
        self.assertNotIn("generate_make_articles", live_src)
        fixture = load_fixture(FIXTURE)
        self.assertEqual(fixture["manifest"]["source_batch_id"], SOURCE_BATCH_ID)
        self.assertEqual(
            sha256_payload(fixture["article_input"]),
            fixture["manifest"]["hashes"]["article_input_sha256"],
        )

    def test_groq_schema_keeps_strict_additional_properties(self) -> None:
        groq = groq_article_first_json_schema()
        self.assertTrue(schema_contains_additional_properties(groq))
        self.assertNotIn("article_sections", groq["properties"])
        self.assertIn("article_body", groq["properties"])
        self.assertIn("id", groq["properties"]["claims"]["items"]["properties"])

    def test_next_writer_is_gemini_3_6_flash(self) -> None:
        self.assertEqual(GEMINI_NEXT_TEXT_MODEL, "gemini-3.6-flash")
        candidate = next_writer_candidate()
        self.assertEqual(candidate["model"], "gemini-3.6-flash")
        self.assertEqual(candidate["candidate_id"], CANDIDATE_GEMINI_36_ARTICLE_FIRST)
        self.assertEqual(candidate["live_calls"], 1)
        self.assertEqual(candidate["repair_calls"], 0)
        url = generate_content_url(GEMINI_NEXT_TEXT_MODEL)
        self.assertIn("/models/gemini-3.6-flash:generateContent", url)
        self.assertNotIn("gemini-2.5-flash", url)

    def test_dry_run_one_validates_schema_without_http(self) -> None:
        result = dry_run_one(FIXTURE, environ={})
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["live_http_calls"], 0)
        self.assertEqual(result["selected_model"], "gemini-3.6-flash")
        self.assertTrue(result["schema_validation"]["ok"])
        self.assertFalse(result["schema_validation"]["has_additionalProperties"])
        self.assertTrue(result["fixture_confirmation"]["hashes_match"])
        self.assertEqual(result["fixture_confirmation"]["source_batch_id"], SOURCE_BATCH_ID)
        self.assertEqual(result["qa_configuration"]["hard_minimum_words"], 350)
        self.assertTrue(result["qa_configuration"]["unchanged"])
        self.assertFalse(result["make_invoked"])
        self.assertEqual(result["credentials_configured"][GEMINI_KEY_ENV], False)

    def test_run_one_writer_without_key_makes_zero_http(self) -> None:
        result = run_one_writer(FIXTURE, environ={})
        self.assertTrue(result["refused"])
        self.assertEqual(result["live_http_calls"], 0)
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])

    def test_run_one_writer_mocked_transport_hits_gemini_3_6_once(self) -> None:
        native = sample_article_first_native(event_id=EVENT_ID, evidence_id="event-005-e01")
        calls: list[str] = []

        class Dummy:
            status_code = 200

            def json(self) -> dict:
                return {
                    "candidates": [{"content": {"parts": [{"text": json.dumps(native)}]}}],
                    "usageMetadata": {
                        "promptTokenCount": 11,
                        "candidatesTokenCount": 22,
                        "totalTokenCount": 33,
                    },
                }

        def transport(url: str, headers: dict, json_body: dict, timeout: int):
            del headers, json_body, timeout
            calls.append(url)
            return Dummy()

        batch = REPO / "output" / "approval" / SOURCE_BATCH_ID / "batch.json"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / EVENT_ID
            freeze_top1_batch(batch_path=batch, dest_dir=dest, repo_root=REPO)
            result = run_one_writer(
                dest,
                environ={GEMINI_KEY_ENV: "test-not-a-real-key"},
                gemini_transport=transport,
            )
        self.assertEqual(len(calls), 1)
        self.assertIn("/models/gemini-3.6-flash:generateContent", calls[0])
        self.assertEqual(result["live_http_calls"], 1)
        self.assertEqual(result["score"]["model"], "gemini-3.6-flash")
        self.assertEqual(result["score"]["native_parse"], "PASS")
        self.assertEqual(result["image_calls"], 0)
        self.assertFalse(result["make_invoked"])
        self.assertFalse(result["groq_called"])

    def test_cli_live_refuses_groq_retest(self) -> None:
        code = bakeoff_main.main(["--live"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()


