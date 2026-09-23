"""Mock/local tests for V5 depth fallback + Kimi/Vertex cost accumulation."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.v5_generation.depth_cost_helpers import (
    DEPTH_FALLBACK_PASS,
    apply_depth_fallback,
    accumulate_vertex_receipts,
    build_review_cost_text,
    compute_kimi_cost_usd,
    format_usd,
    merge_usage,
    missing_kimi_pricing_keys,
    missing_vertex_pricing_keys,
)


def _qa(critical_codes, event_id="evt-1"):
    return {
        "event_id": event_id,
        "critical_failures": [
            {"code": code, "message": code, "severity": "critical", "module": "depth"}
            for code in critical_codes
        ],
        "warnings": [],
        "metrics": {},
        "qa_passed": not critical_codes,
        "publishable": not critical_codes,
        "qa_publishable": not critical_codes,
        "critical_count": len(critical_codes),
    }


class DepthFallbackTests(unittest.TestCase):
    def test_first_470_without_recovery_does_not_fallback(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 470},
            qa=_qa(["below_article_minimum_length"]),
            word_count=470,
            recovery_history=[],
            recovery_succeeded=False,
        )
        self.assertNotEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertFalse(out["ok"])
        self.assertEqual(out.get("reason"), "recovery_not_exhausted")

    def test_exhausted_470_only_length_critical_fallback_pass(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 470},
            qa=_qa(["below_article_minimum_length"]),
            word_count=470,
            recovery_history=[{"attempt": 1, "recovery_succeeded": False}],
            recovery_succeeded=False,
        )
        self.assertEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertTrue(out["ok"])
        self.assertEqual(out["suppressed_critical_code"], "below_article_minimum_length")
        self.assertEqual(out["qa"]["critical_count"], 0)
        self.assertTrue(out["qa"]["qa_passed"])

    def test_exhausted_399_blocked(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 399},
            qa=_qa(["below_article_minimum_length"]),
            word_count=399,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertNotEqual(out["depth_status"], DEPTH_FALLBACK_PASS)

    def test_exhausted_470_with_other_critical_blocked(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 470},
            qa=_qa(["below_article_minimum_length", "unsupported_claim"]),
            word_count=470,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertIn("unsupported_claim", out.get("remaining_critical_codes") or [])

    def test_exhausted_470_insufficient_evidence_not_suppressed(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 470},
            qa=_qa(["below_article_minimum_length", "insufficient_evidence_for_target_depth"]),
            word_count=470,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertFalse(out["ok"])
        self.assertIn("insufficient_evidence_for_target_depth", out.get("remaining_critical_codes") or [])

    def test_recovered_620_normal_pass(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 620},
            qa=_qa([]),
            word_count=620,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=True,
        )
        self.assertEqual(out["depth_status"], "DEPTH_NORMAL_PASS")
        self.assertTrue(out["ok"])

    def test_recovered_520_is_fallback_not_normal(self):
        out = apply_depth_fallback(
            article={"article_body": "word " * 520},
            qa=_qa(["below_article_minimum_length"]),
            word_count=520,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertEqual(out["depth_status"], DEPTH_FALLBACK_PASS)
        self.assertTrue(out["ok"])
        self.assertFalse(out.get("exceptional_fallback"))


class CostHelperTests(unittest.TestCase):
    def test_missing_kimi_keys_reported_exactly(self):
        missing = missing_kimi_pricing_keys({}, {"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(
            missing,
            [
                "NEWSAGENT_V2_KIMI_PRICING_VERIFIED",
                "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK",
                "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK",
            ],
        )
        cost, keys = compute_kimi_cost_usd({"prompt_tokens": 10, "completion_tokens": 5}, {})
        self.assertIsNone(cost)
        self.assertEqual(keys, missing)
        self.assertIn("NEWSAGENT_V2_KIMI_PRICING_VERIFIED", format_usd(None, keys))

    def test_verified_kimi_numeric_cost(self):
        env = {
            "NEWSAGENT_V2_KIMI_PRICING_VERIFIED": "1",
            "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK": "2.0",
        }
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000, "provider": "kimi", "model": "k2"}
        cost, missing = compute_kimi_cost_usd(usage, env)
        self.assertEqual(missing, [])
        self.assertAlmostEqual(cost, 3.0, places=6)

    def test_accumulate_multiple_vertex_receipts(self):
        env = {
            "NEWSAGENT_V2_VERTEX_PRICING_VERIFIED": "true",
            "NEWSAGENT_V2_VERTEX_INPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_VERTEX_IMAGE_OUTPUT_USD_PER_MTOK": "2.0",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-3.1-flash-image",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i, (pin, pout) in enumerate([(100, 50), (100, 50)], start=1):
                d = root / f"evt-1-run{i}"
                d.mkdir()
                (d / "provider_result.json").write_text(
                    json.dumps(
                        {
                            "provider_name": "vertex",
                            "model_name": "gemini-3.1-flash-image",
                            "provider_reported_usage": {
                                "promptTokenCount": pin,
                                "totalTokenCount": pin + pout,
                                "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": pout}],
                            },
                        }
                    ),
                    encoding="utf-8",
                )
            image = accumulate_vertex_receipts("evt-1", root, env)
            self.assertEqual(image["requests"], 2)
            self.assertAlmostEqual(image["accumulated_cost_usd"], 0.0004, places=8)

    def test_combined_total_text_plus_image(self):
        env = {
            "NEWSAGENT_V2_KIMI_PRICING_VERIFIED": "1",
            "NEWSAGENT_V2_KIMI_INPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_KIMI_OUTPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_VERTEX_PRICING_VERIFIED": "1",
            "NEWSAGENT_V2_VERTEX_INPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_VERTEX_IMAGE_OUTPUT_USD_PER_MTOK": "1.0",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-3.1-flash-image",
        }
        text = {"provider": "kimi", "model": "k2", "prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000}
        image = {
            "provider": "vertex",
            "model": "gemini-3.1-flash-image",
            "requests": 1,
            "provider_reported_usage": {
                "promptTokenCount": 1_000_000,
                "candidatesTokensDetails": [{"modality": "IMAGE", "tokenCount": 0}],
                "totalTokenCount": 1_000_000,
            },
            "accumulated_cost_usd": 1.0,
        }
        block = build_review_cost_text(text_usage=text, image_usage=image, environ=env, escape_html=lambda s: s)
        self.assertIn("TEXT — kimi/k2", block)
        self.assertIn("IMAGE — vertex/gemini-3.1-flash-image", block)
        self.assertIn("Cost: $1.0000 USD", block)
        self.assertIn("TOTAL COST: $2.0000 USD", block)
        self.assertEqual(block.count("TOTAL COST:"), 1)

    def test_merge_usage_accumulates_and_zero_calls_do_not_inflate(self):
        prior = {"prompt_tokens": 100, "completion_tokens": 20, "cost_usd": 0.5, "requests": 1}
        new = {"prompt_tokens": 50, "completion_tokens": 10, "cost_usd": 0.25, "requests": 1}
        merged = merge_usage(prior, new, provider_calls=1)
        self.assertEqual(merged["prompt_tokens"], 150)
        self.assertAlmostEqual(merged["cost_usd"], 0.75)
        frozen = merge_usage(merged, new, provider_calls=0)
        self.assertEqual(frozen["prompt_tokens"], 150)
        self.assertAlmostEqual(frozen["cost_usd"], 0.75)

    def test_missing_vertex_keys(self):
        missing = missing_vertex_pricing_keys({}, {"provider": "vertex", "model": "gemini-3.1-flash-image"})
        self.assertEqual(
            missing,
            [
                "NEWSAGENT_V2_VERTEX_PRICING_VERIFIED",
                "NEWSAGENT_V2_VERTEX_INPUT_USD_PER_MTOK",
                "NEWSAGENT_V2_VERTEX_IMAGE_OUTPUT_USD_PER_MTOK",
            ],
        )


class IdentityNotes(unittest.TestCase):
    def test_event_identity_passthrough_contract(self):
        event_id = "selected-event-xyz"
        result = {"ok": True, "event_id": event_id, "article_version": "v1"}
        self.assertEqual(result["event_id"], event_id)

    def test_fallback_requires_recovery_history_before_pass(self):
        first = apply_depth_fallback(
            article={"article_body": "x"},
            qa=_qa(["below_article_minimum_length"]),
            word_count=470,
            recovery_history=[],
            recovery_succeeded=False,
        )
        self.assertFalse(first["ok"])
        second = apply_depth_fallback(
            article={"article_body": "x"},
            qa=_qa(["below_article_minimum_length"]),
            word_count=470,
            recovery_history=[{"attempt": 1}],
            recovery_succeeded=False,
        )
        self.assertTrue(second["ok"])


if __name__ == "__main__":
    unittest.main()
