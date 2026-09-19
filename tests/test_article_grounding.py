from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.prompt import SYSTEM_PROMPT
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import (
    ARTICLE_MODE_BRIEF,
    ARTICLE_MODE_FULL,
    ARTICLE_MODE_NORMAL,
    BRIEF_ARTICLE_POLICY,
    NORMAL_ARTICLE_POLICY,
)
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.benchmark.input import write_json_utf8
from tests.fixtures.article_qa import (
    ABSENCE_EVIDENCE_URL,
    CONTEXT_EVIDENCE_URL,
    EVIDENCE_URL,
    FILLER_CONTEXT_BODY,
    LONG_META,
    THIN_CANDIDATE,
    _append_claimed_sentence,
    article_input,
    clean_article,
    pack_with_extra_evidence,
    thin_article_input,
)
from tests.test_article_qa import _codes

REPO = Path(__file__).resolve().parents[1]
LIVE_RUN = (
    REPO
    / "output"
    / "benchmarks"
    / "article_runs"
    / "20260914T065731Z-3f72a160"
)
REPLAY_OUT = (
    REPO
    / "output"
    / "benchmarks"
    / "article_replays"
    / "20260914T065731Z-3f72a160"
    / "qa_p0_grounding_hardening.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BodyClaimCoverageTests(unittest.TestCase):
    def test_body_sentence_represented_by_claim_is_covered(self) -> None:
        article = clean_article()
        result = run_article_qa(article, article_input())
        self.assertTrue(result["qa_passed"])
        self.assertEqual(result["metrics"]["uncovered_assertive_sentences"], [])
        self.assertEqual(result["metrics"]["uncovered_assertive_sentence_count"], 0)
        self.assertEqual(result["metrics"]["body_claim_coverage"], 1.0)
        self.assertNotIn("body_assertion_not_in_claims", _codes(result, severity="critical"))

    def test_compound_supported_sentence_maps_to_multiple_claims(self) -> None:
        from newsagent_v2.article.qa.grounding import check_body_claim_coverage

        sentence = (
            "According to blockchain security firm Blockaid, the attack resulted "
            "in the minting of roughly 46.1 billion syBTC, but the attacker only "
            "realized about $336,000 in proceeds."
        )
        article = clean_article()
        article["article_body"] = article["article_body"] + " " + sentence
        article["claims"] = list(article["claims"]) + [
            {
                "claim_id": "c-sybtc",
                "text": "Blockaid reported that roughly 46.1 billion syBTC were minted during the exploit.",
                "claim_type": "number",
                "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
            },
            {
                "claim_id": "c-proceeds",
                "text": "Blockaid estimated the attacker realized about $336,000 in proceeds.",
                "claim_type": "number",
                "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
            },
        ]
        issues, metrics = check_body_claim_coverage(article)
        self.assertEqual(metrics["uncovered_assertive_sentence_count"], 0)
        self.assertFalse(any(item["code"] == "body_assertion_not_in_claims" for item in issues))

    def test_compound_sentence_with_unsupported_clause_still_fails(self) -> None:
        from newsagent_v2.article.qa.grounding import sentence_covered_by_claims

        sentence = (
            "According to blockchain security firm Blockaid, the attack resulted "
            "in the minting of roughly 46.1 billion syBTC, but the attacker only "
            "realized about $336,000 in proceeds, while the CEO resigned the same day."
        )
        claims = [
            "Blockaid reported that roughly 46.1 billion syBTC were minted during the exploit.",
            "Blockaid estimated the attacker realized about $336,000 in proceeds.",
        ]
        self.assertFalse(sentence_covered_by_claims(sentence, claims))

    def test_body_factual_sentence_absent_from_claims_is_flagged(self) -> None:
        article = clean_article()
        article["article_body"] += (
            " Regulators in three countries opened a criminal inquiry on Tuesday."
        )
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertIn("body_assertion_not_in_claims", _codes(result, severity="critical"))
        uncovered = result["metrics"]["uncovered_assertive_sentences"]
        self.assertTrue(any("criminal inquiry" in item for item in uncovered))
        self.assertGreaterEqual(result["metrics"]["uncovered_assertive_sentence_count"], 1)
        self.assertLess(result["metrics"]["body_claim_coverage"], 1.0)
        self.assertEqual(
            result["metrics"]["claims_with_evidence"],
            result["metrics"]["claim_count"],
        )

    def test_unsupported_body_assertion_cannot_hide_behind_valid_claims(self) -> None:
        article = clean_article()
        article["article_body"] += (
            " A separate offshore affiliate quietly moved customer deposits overnight."
        )
        result = run_article_qa(article, article_input())
        self.assertGreaterEqual(result["metrics"]["claim_count"], 1)
        self.assertEqual(
            result["metrics"]["claims_with_evidence"],
            result["metrics"]["claim_count"],
        )
        self.assertFalse(result["publishable"])
        self.assertIn("body_assertion_not_in_claims", _codes(result, severity="critical"))
        failures = [
            item
            for item in result["critical_failures"]
            if item["code"] == "body_assertion_not_in_claims"
        ]
        self.assertTrue(any("offshore affiliate" in item.get("sentence", "") for item in failures))


class AbsenceClaimTests(unittest.TestCase):
    def test_supported_explicit_negative_claim_accepted(self) -> None:
        sentence = (
            "The payments firm did not disclose names of people whose files were involved."
        )
        pack = pack_with_extra_evidence(
            ABSENCE_EVIDENCE_URL,
            "Northwind withholds named victim list",
            "Northwind said it did not disclose the names of affected individuals.",
        )
        article = _append_claimed_sentence(
            clean_article(),
            sentence,
            url=ABSENCE_EVIDENCE_URL,
        )
        article["evidence_used"].append(
            {"url": ABSENCE_EVIDENCE_URL, "source": "TestWire"}
        )
        result = run_article_qa(article, pack)
        self.assertNotIn("unsupported_absence_claim", _codes(result, severity="critical"))
        self.assertTrue(result["qa_passed"])

    def test_unsupported_did_not_disclose_is_critical(self) -> None:
        article = clean_article()
        article["article_body"] += (
            " Revolut did not disclose the total number of affected accounts."
        )
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertIn("unsupported_absence_claim", _codes(result, severity="critical"))

    def test_unsupported_has_not_announced_is_critical(self) -> None:
        article = clean_article()
        article["article_body"] += (
            " Northwind has not announced a recovery timeline for customers."
        )
        result = run_article_qa(article, article_input())
        self.assertIn("unsupported_absence_claim", _codes(result, severity="critical"))

    def test_unsupported_no_funds_were_lost_is_critical(self) -> None:
        article = clean_article()
        article["article_body"] += " The company said no funds were lost in the incident."
        result = run_article_qa(article, article_input())
        self.assertIn("unsupported_absence_claim", _codes(result, severity="critical"))

    def test_valid_evidence_ref_alone_is_insufficient_for_negative_semantics(self) -> None:
        sentence = "Northwind did not disclose the total number of affected accounts."
        article = _append_claimed_sentence(
            clean_article(),
            sentence,
            url=EVIDENCE_URL,
        )
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertIn("unsupported_absence_claim", _codes(result, severity="critical"))
        absence = [
            item
            for item in result["critical_failures"]
            if item["code"] == "unsupported_absence_claim"
        ]
        self.assertTrue(absence)
        self.assertIn("disclose", absence[0].get("absence_families", []))
        self.assertEqual(absence[0].get("evidence_urls"), [EVIDENCE_URL])
        self.assertEqual(absence[0].get("evidence_support_families"), [])


class ContextualAssertionTests(unittest.TestCase):
    def test_grounded_contextual_assertion_accepted(self) -> None:
        sentence = (
            "The Northwind case comes amid increasing government-domain phishing "
            "against payments firms."
        )
        pack = pack_with_extra_evidence(
            CONTEXT_EVIDENCE_URL,
            "Phishing pattern around payments firms",
            "The Northwind case comes amid increasing government-domain phishing "
            "against payments firms, TestWire reported.",
        )
        article = _append_claimed_sentence(
            clean_article(),
            sentence,
            claim_type="contextual",
            url=CONTEXT_EVIDENCE_URL,
        )
        article["evidence_used"].append(
            {"url": CONTEXT_EVIDENCE_URL, "source": "TestWire"}
        )
        result = run_article_qa(article, pack)
        self.assertNotIn(
            "ungrounded_contextual_assertion",
            _codes(result, severity="critical"),
        )
        self.assertTrue(result["qa_passed"])

    def test_ungrounded_generic_industry_conclusion_flagged(self) -> None:
        article = clean_article()
        article["article_body"] += (
            " The episode underscores ongoing security challenges for fintech platforms."
        )
        result = run_article_qa(article, article_input())
        self.assertFalse(result["publishable"])
        self.assertIn(
            "ungrounded_contextual_assertion",
            _codes(result, severity="critical"),
        )


class DepthPolicyTests(unittest.TestCase):
    def test_short_normal_article_fails_depth_policy(self) -> None:
        article = clean_article()
        article["article_body"] = (
            "Northwind Payments reported that 12,400 customer records were exposed. "
            "The firm said a spoofed government-domain email reached staff. "
            "Passports and transaction histories were obtained from customer files. "
            "Security staff are notifying affected users after the disclosure. "
            "The company is reviewing how the fake domain request was processed. "
            "TestWire carried the customer-data disclosure in its Monday report. "
            "Retail payments customer records were the dataset described. "
            "Identity papers and payment history were listed among record types. "
            "The 12,400 figure is the record count Northwind Payments gave TestWire. "
            "The spoofed-email incident is the event described to reporters."
        )
        self.assertLess(word_count(article["article_body"]), 350)
        result = run_article_qa(article, article_input())
        self.assertEqual(result["metrics"]["article_mode"], ARTICLE_MODE_NORMAL)
        self.assertIn("below_article_minimum_length", _codes(result, severity="critical"))
        self.assertFalse(result["publishable"])

    def test_grounded_article_at_least_350_words_passes_depth(self) -> None:
        result = run_article_qa(clean_article(), article_input())
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 350)
        self.assertNotIn("below_article_minimum_length", _codes(result, severity="critical"))
        self.assertNotIn(
            "insufficient_evidence_for_target_depth",
            _codes(result, severity="critical"),
        )
        self.assertTrue(result["qa_passed"])

    def test_short_article_is_not_silently_converted_to_brief(self) -> None:
        article = clean_article()
        article["article_body"] = (
            "Northwind Payments reported that 12,400 customer records were exposed."
        )
        result = run_article_qa(article, article_input())
        self.assertEqual(result["metrics"]["article_mode"], ARTICLE_MODE_NORMAL)
        self.assertNotEqual(result["metrics"]["article_mode"], ARTICLE_MODE_BRIEF)
        self.assertEqual(
            result["metrics"]["depth_hard_minimum_words"],
            NORMAL_ARTICLE_POLICY.hard_minimum_words,
        )
        self.assertIn("below_article_minimum_length", _codes(result, severity="critical"))

    def test_full_mode_alias_still_uses_full_article_policy(self) -> None:
        article = clean_article()
        article["article_body"] = (
            "Northwind Payments reported that 12,400 customer records were exposed."
        )
        result = run_article_qa(
            article,
            article_input(),
            article_mode=ARTICLE_MODE_FULL,
        )
        self.assertEqual(result["metrics"]["article_mode"], ARTICLE_MODE_NORMAL)
        self.assertNotEqual(result["metrics"]["article_mode"], ARTICLE_MODE_BRIEF)
        self.assertIn("below_article_minimum_length", _codes(result, severity="critical"))

    def test_explicit_brief_mode_uses_brief_policy_only_when_requested(self) -> None:
        article = clean_article()
        article["article_body"] = (
            "Northwind Payments reported that 12,400 customer records were exposed. "
            "The firm said a spoofed government-domain email reached staff. "
            "Passports and transaction histories were obtained from customer files. "
            "Security staff are notifying affected users after the disclosure."
        )
        brief = run_article_qa(
            article,
            article_input(),
            article_mode=ARTICLE_MODE_BRIEF,
        )
        self.assertEqual(brief["metrics"]["article_mode"], ARTICLE_MODE_BRIEF)
        self.assertEqual(
            brief["metrics"]["depth_hard_minimum_words"],
            BRIEF_ARTICLE_POLICY.hard_minimum_words,
        )
        normal = run_article_qa(article, article_input())
        self.assertIn("below_article_minimum_length", _codes(normal, severity="critical"))

    def test_insufficient_evidence_does_not_authorize_filler(self) -> None:
        thin = thin_article_input()
        thin_url = thin["evidence"][0]["url"]

        def retarget(article: dict) -> dict:
            article["event_id"] = "event-syn-thin"
            article["headline"] = "Northwind Payments notes a customer-file incident"
            article["seo_title"] = "Northwind Payments notes a customer-file incident"
            article["evidence_used"] = [{"url": thin_url, "source": "TestWire"}]
            for claim in article["claims"]:
                claim["evidence_refs"] = [{"url": thin_url, "source": "TestWire"}]
            for quote in article["quotes"]:
                quote["evidence_refs"] = [{"url": thin_url, "source": "TestWire"}]
            return article

        short = retarget(clean_article())
        short["article_body"] = "Northwind Payments reported a customer-file incident."
        short_result = run_article_qa(short, thin)
        self.assertIn(
            "insufficient_evidence_for_target_depth",
            _codes(short_result, severity="critical"),
        )

        padded = retarget(clean_article())
        padded["article_body"] = FILLER_CONTEXT_BODY
        self.assertGreaterEqual(word_count(FILLER_CONTEXT_BODY), 350)
        padded_result = run_article_qa(padded, thin)
        self.assertGreaterEqual(padded_result["metrics"]["article_word_count"], 350)
        self.assertIn(
            "insufficient_evidence_for_target_depth",
            _codes(padded_result, severity="critical"),
        )
        self.assertFalse(padded_result["publishable"])


class MetaAndPromptTests(unittest.TestCase):
    def test_meta_description_over_160_remains_warning(self) -> None:
        article = clean_article()
        article["meta_description"] = LONG_META
        self.assertGreater(len(LONG_META), 160)
        result = run_article_qa(article, article_input())
        self.assertIn("meta_description_length", _codes(result, severity="warning"))
        self.assertNotIn("meta_description_length", _codes(result, severity="critical"))

    def test_prompt_hardens_claims_ledger_and_forbids_absence_inference(self) -> None:
        folded = " ".join(SYSTEM_PROMPT.split())
        self.assertIn("evidence ledger for body units", folded)
        self.assertIn("HARD REQUIREMENT: the Python-rendered article_body must be at least 350 words", folded)
        self.assertIn("TARGET: 450-800 words", folded)
        self.assertIn("Missing evidence is not evidence of absence.", folded)
        self.assertIn("was not disclosed, was not announced, was not confirmed", folded)
        self.assertIn("Do not invent industry trends.", folded)
        self.assertIn("Do not invent causal interpretation.", folded)
        self.assertIn("Do not fabricate quotes.", folded)


class FrozenReplayTests(unittest.TestCase):
    def test_original_parsed_article_artifact_remains_unchanged(self) -> None:
        parsed = LIVE_RUN / "parsed_article_output.json"
        generation = LIVE_RUN / "generation_input.json"
        originals = {
            "parsed_article_output.json": parsed,
            "generation_input.json": generation,
            "qa_result.json": LIVE_RUN / "qa_result.json",
            "telemetry.json": LIVE_RUN / "telemetry.json",
            "request_metadata.json": LIVE_RUN / "request_metadata.json",
            "raw_provider_response.json": LIVE_RUN / "raw_provider_response.json",
        }
        before = {name: _sha256(path) for name, path in originals.items()}
        article = json.loads(parsed.read_text(encoding="utf-8"))
        pack = json.loads(generation.read_text(encoding="utf-8"))
        result = run_article_qa(article, pack)
        after = {name: _sha256(path) for name, path in originals.items()}
        self.assertEqual(before, after)
        self.assertEqual(article["event_id"], "event-021")

        payload = {
            "purpose": (
                "P0 offline QA replay after grounding hardening. "
                "Original live artifacts not modified."
            ),
            "source_run_id": "20260914T065731Z-3f72a160",
            "source_parsed_article": str(parsed),
            "source_generation_input": str(generation),
            "source_sha256": before,
            "qa_result": result,
        }
        write_json_utf8(REPLAY_OUT, payload)
        self.assertTrue(REPLAY_OUT.is_file())
        self.assertEqual({name: _sha256(path) for name, path in originals.items()}, before)
        self.assertFalse(result["publishable"])
        self.assertFalse(result["qa_passed"])
        self.assertEqual(result["metrics"]["article_mode"], ARTICLE_MODE_NORMAL)
        self.assertLess(result["metrics"]["article_word_count"], 350)


class IsolationTests(unittest.TestCase):
    def test_article_qa_has_no_telegram_wordpress_or_image_hooks(self) -> None:
        article_root = REPO / "src" / "newsagent_v2" / "article"
        blocked = (
            "import telegram",
            "from telegram",
            "wordpress",
            "GenerateImage",
            "stable-diffusion",
            "huggingface.co",
        )
        for path in article_root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            self.assertNotIn("import telegram", lowered)
            self.assertNotIn("from telegram", lowered)
            self.assertNotIn("wordpress", lowered)
            self.assertNotIn("generateimage", lowered)
            self.assertNotIn("stable-diffusion", lowered)
            self.assertNotIn("huggingface.co", lowered)
            self.assertNotIn("requests.post", lowered)
        self.assertTrue(blocked)

    def test_replay_does_not_use_network_or_model(self) -> None:
        self.assertNotIn("https://api.groq.com", SYSTEM_PROMPT)
        runner = (REPO / "src" / "newsagent_v2" / "article" / "qa" / "runner.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("post_chat_completion", runner)
        self.assertNotIn("GROQ_API_KEY", runner)


class ThinPackConstructionTests(unittest.TestCase):
    def test_thin_pack_is_not_the_default_full_fixture(self) -> None:
        full = article_input()
        thin = thin_article_input()
        self.assertNotEqual(full["event_id"], thin["event_id"])
        self.assertEqual(THIN_CANDIDATE["event_id"], "event-syn-thin")
        rebuilt = build_article_input(THIN_CANDIDATE, {"event_id": "event-syn-thin"})
        self.assertLess(
            word_count(" ".join(row["summary"] for row in rebuilt["evidence"])),
            NORMAL_ARTICLE_POLICY.min_evidence_words_for_target_depth,
        )


if __name__ == "__main__":
    unittest.main()


