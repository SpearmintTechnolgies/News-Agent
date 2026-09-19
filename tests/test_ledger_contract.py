from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.grounding import sentence_matches_claim
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import split_sentences
from newsagent_v2.article.writer.bedrock_mantle import REAL_INFERENCE_AUTHORIZED
from newsagent_v2.article.writer.evidence_ledger import build_evidence_ledgers
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.article.writer.ledger_resolve import apply_evidence_ledgers, matching_ledger_claims
from newsagent_v2.article.writer.prompts import ARTICLE_FIRST_SYSTEM_PROMPT, LEDGER_FIRST_SYSTEM_PROMPT
from newsagent_v2.article.writer.schema import ARTICLE_FIRST_REQUIRED, LEDGER_FIRST_REQUIRED
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.gemini_ledger_first_audition import run_audition as run_gemini_ledger_first
from newsagent_v2.bench.writer_bakeoff.ledger_replay import run_offline_ledger_study
from newsagent_v2.bench.writer_bakeoff.providers import (
    gemini_article_first_request,
    gemini_ledger_first_request,
    next_writer_candidate,
)

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "benchmarks" / "writer_bakeoff" / EVENT_ID
GEMINI_ARTICLE = (
    FIXTURE / "live_runs" / "20260915T092107Z" / "gemini_3_6_flash_article_first" / "article.json"
)


def _fixture():
    return load_fixture(FIXTURE)


def _shell_article(body: str) -> dict:
    fixture = _fixture()
    source = json.loads(GEMINI_ARTICLE.read_text(encoding="utf-8"))
    article = deepcopy(source)
    article["article_body"] = body
    article["article_sections"] = [
        {
            "id": "s1",
            "section_id": "s1",
            "purpose": "",
            "paragraphs": [{"text": body, "claim_ids": ["claim-001"]}],
        }
    ]
    return article, fixture["article_input"]


class LedgerContractTests(unittest.TestCase):
    def test_unsupported_assertions_never_become_grounded(self) -> None:
        article, article_input = _shell_article(
            "The bill is expected to transform American crypto markets after years of neglect."
        )
        ledgers = build_evidence_ledgers(article_input)
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        self.assertEqual(
            mapped["article_body"],
            "The bill is expected to transform American crypto markets after years of neglect.",
        )
        qa = run_article_qa(deepcopy(mapped), article_input, article_mode="normal")
        uncovered = qa["metrics"]["uncovered_assertive_sentences"]
        self.assertTrue(any("transform American crypto markets" in row for row in uncovered))
        self.assertIn("body_assertion_not_in_claims", [item["code"] for item in qa["critical_failures"]])

    def test_ambiguous_assertions_never_become_grounded(self) -> None:
        article, article_input = _shell_article(
            "Observers say the package could reshape Washington's digital-asset posture overnight."
        )
        ledgers = build_evidence_ledgers(article_input)
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        self.assertEqual(mapped["claims"], [])
        qa = run_article_qa(deepcopy(mapped), article_input, article_mode="normal")
        self.assertIn("body_assertion_not_in_claims", [item["code"] for item in qa["critical_failures"]])

    def test_exact_evidence_supported_assertions_can_map(self) -> None:
        ledgers = build_evidence_ledgers(_fixture()["article_input"])
        claim = next(row for row in ledgers.claims if "final offer to Democrats" in row.text)
        article, article_input = _shell_article(claim.text)
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        self.assertIn(claim.claim_id, mapped["_ledger_mapped_claim_ids"])
        self.assertTrue(any(row["claim_id"] == claim.claim_id for row in mapped["claims"]))
        self.assertTrue(all(row["claim_id"].startswith("C") for row in mapped["claims"]))

    def test_paraphrase_maps_only_with_deterministic_equivalence(self) -> None:
        ledgers = build_evidence_ledgers(_fixture()["article_input"])
        safe = "Senate Republicans released revised CLARITY Act text as a final offer to Democrats, with a key procedural vote set for Tuesday."
        unsafe = "A central element of the updated CLARITY Act text involves ethics rules that have received the explicit endorsement of US President Donald Trump."
        self.assertTrue(matching_ledger_claims(safe, ledgers))
        self.assertFalse(matching_ledger_claims(unsafe, ledgers))

    def test_exact_approved_quotes_map(self) -> None:
        ledgers = build_evidence_ledgers(_fixture()["article_input"])
        quote = ledgers.quotes[0]
        article, article_input = _shell_article(f'Lummis said, "{quote.text}"')
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        self.assertIn(quote.quote_id, mapped["_ledger_mapped_quote_ids"])

    def test_modified_or_invented_quotes_fail(self) -> None:
        ledgers = build_evidence_ledgers(_fixture()["article_input"])
        claim = next(row for row in ledgers.claims if "final offer to Democrats" in row.text)
        article, article_input = _shell_article(
            f'{claim.text} An aide said, "This invented line does not appear in frozen evidence."'
        )
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        self.assertEqual(mapped["_ledger_mapped_quote_ids"], [])
        qa = run_article_qa(deepcopy(mapped), article_input, article_mode="normal")
        self.assertIn("quote_body_unmapped", [item["code"] for item in qa["critical_failures"]])

    def test_pm_am_segmentation_works(self) -> None:
        text = (
            "The procedural vote scheduled for Tuesday at 2:15 p.m. ET marks a "
            "decisive juncture for the CLARITY Act."
        )
        self.assertEqual(split_sentences(text), [text])
        morning = "The briefing begins at 9:00 a.m. Eastern Time on Sunday."
        self.assertEqual(split_sentences(morning), [morning])

    def test_abbreviations_do_not_create_false_assertions(self) -> None:
        text = "Ms. Lummis met in the U.S. Capitol before 2:15 p.m. ET."
        sentences = split_sentences(text)
        self.assertEqual(len(sentences), 1)
        self.assertFalse(any(row.startswith("ET") for row in sentences))

    def test_multiple_assertions_cannot_hide_unsupported(self) -> None:
        supported = "The 635-page revised proposal includes Trump-backed ethics provisions and comes just two days before a key procedural vote."
        hidden = f"{supported} The bill is expected to transform American crypto markets."
        article, article_input = _shell_article(hidden)
        ledgers = build_evidence_ledgers(article_input)
        mapped = apply_evidence_ledgers(article, ledgers, article_input)
        qa = run_article_qa(deepcopy(mapped), article_input, article_mode="normal")
        uncovered = " ".join(qa["metrics"]["uncovered_assertive_sentences"])
        self.assertIn("transform American crypto markets", uncovered)

    def test_claim_ids_originate_from_frozen_ledger(self) -> None:
        article = json.loads(GEMINI_ARTICLE.read_text(encoding="utf-8"))
        fixture = _fixture()
        ledgers = build_evidence_ledgers(fixture["article_input"])
        allowed = {row.claim_id for row in ledgers.claims}
        mapped = apply_evidence_ledgers(article, ledgers, fixture["article_input"])
        used = {row["claim_id"] for row in mapped["claims"]}
        self.assertTrue(used)
        self.assertTrue(used <= allowed)

    def test_resolver_cannot_create_new_factual_claims(self) -> None:
        article = json.loads(GEMINI_ARTICLE.read_text(encoding="utf-8"))
        fixture = _fixture()
        ledgers = build_evidence_ledgers(fixture["article_input"])
        allowed_texts = {row.text for row in ledgers.claims}
        mapped = apply_evidence_ledgers(article, ledgers, fixture["article_input"])
        for claim in mapped["claims"]:
            self.assertIn(claim["text"], allowed_texts)
        self.assertEqual(mapped["article_body"], article["article_body"])

    def test_qa_remains_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertFalse(REAL_INFERENCE_AUTHORIZED)
        self.assertIn("claims", ARTICLE_FIRST_REQUIRED)
        self.assertNotIn("claims", LEDGER_FIRST_REQUIRED)
        self.assertIn("Then return claims", ARTICLE_FIRST_SYSTEM_PROMPT)
        self.assertIn("closed factual ledger", LEDGER_FIRST_SYSTEM_PROMPT)
        self.assertIn("ONLY come from QuoteLedger", LEDGER_FIRST_SYSTEM_PROMPT)

    def test_ledger_first_request_omits_claim_schema_and_uses_gemini_3_6(self) -> None:
        fixture = _fixture()
        body = gemini_ledger_first_request(fixture)
        schema = body["generationConfig"]["responseSchema"]
        self.assertNotIn("claims", schema.get("properties") or {})
        self.assertNotIn("quotes", schema.get("properties") or {})
        old = gemini_article_first_request(fixture)
        self.assertIn("claims", (old["generationConfig"]["responseSchema"].get("properties") or {}))
        self.assertEqual(next_writer_candidate()["model"], "gemini-3.6-flash")
        missing = run_gemini_ledger_first(environ={}, persist=False)
        self.assertEqual(missing["generation_calls"], 0)
        self.assertEqual(missing["exact_model"], "gemini-3.6-flash")
        self.assertNotEqual(missing["exact_model"], "gemini-3.8-flash")
        self.assertFalse(missing["make_invoked"])
        self.assertEqual(missing["kimi_calls"], 0)

    def test_offline_replay_makes_zero_model_calls(self) -> None:
        report = run_offline_ledger_study()
        self.assertEqual(report["external_model_calls"], 0)
        self.assertEqual(report["kimi_calls"], 0)
        self.assertEqual(report["qa_changed"], "NO")
        self.assertEqual(report["evidence_changed"], "NO")
        self.assertEqual(report["article_prose_changed"], "NO")
        self.assertTrue(all(row["prose_unchanged"] for row in report["replays"]))
        self.assertEqual(len(report["replays"]), 4)

    def test_isolation_tokens_absent(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/article/writer/evidence_ledger.py",
            "src/newsagent_v2/article/writer/ledger_resolve.py",
            "src/newsagent_v2/bench/writer_bakeoff/ledger_replay.py",
            "src/newsagent_v2/bench/writer_bakeoff/gemini_ledger_first_audition.py",
            "src/newsagent_v2/bench/writer_bakeoff/groq_qwen_38_ledger_first_audition.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()


