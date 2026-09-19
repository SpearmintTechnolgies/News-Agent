from __future__ import annotations

import inspect
import json
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.batch_prompt import BATCH_SYSTEM_PROMPT, build_batch_messages, build_batch_story_payload
from newsagent_v2.article.batch_runner import generate_make_articles
from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS, evidence_sufficiency
from newsagent_v2.article.extract_clean import clean_extracted_article_text
from newsagent_v2.article.prompt_evidence import (
    GROQ_BATCH_ADMISSION_TARGET,
    GROQ_ON_DEMAND_TPM_LIMIT,
    MAX_PROMPT_EVIDENCE_WORDS_PER_STORY,
    compact_story_evidence,
    evidence_id_for,
)
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY, WARN_SENTENCE_SIMILARITY
from newsagent_v2.batch.viability import DEFAULT_CANDIDATE_SCAN_LIMIT
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.control import live as live_mod
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS, build_top5_article_batch_request_body
from newsagent_v2.providers.groq_editorial import estimate_prompt_tokens_from_bytes, safe_chat_request_diagnostics
from tests.test_top5_single_call import ENV, RecordingPost, _article, _payload, _sufficient_row
from tests.test_groq_editorial_benchmark import FakeResponse

REPO = Path(__file__).resolve().parents[1]
MAKE5_BATCH = REPO / "output" / "approval" / "20260915T063852Z-3ef218cc" / "batch.json"
SELECTED = ["event-005", "event-028", "event-032", "event-020"]
LIVE_SRC = inspect.getsource(live_mod)
BATCH_RUNNER_SRC = (REPO / "src" / "newsagent_v2" / "article" / "batch_runner.py").read_text(
    encoding="utf-8"
)

TICKER_BLOCK = (
    "DOGE $0.08298 1.42% TRX $0.3380 0.42% LINK $11.44 0.30% ZEC $1,146.50 1.65% "
    "ADA $0.2054 1.31% XRP $1.40 1.72% ETH $2,488.62 0.95% BTC $77,434.56 0.06% "
    "XMR $511.56 0.30% BNB $719.51 0.48% XLM $0.1941 5.76% SOL $101.13 0.21% HYPE $79.38 0.27%"
)
BYLINE_DUP = (
    "Written by Felix Ng staff editor Reviewed by Yohan Yun staff editor "
    "Written by Felix Ng staff editor Reviewed by Yohan Yun staff editor"
)
IN_STORY_BTC = (
    "Cross-chain liquidity protocol Symbiosis said it recovered 15 Bitcoin, "
    "worth around $1.1 million, from a Bitcoin bridge exploit on Friday according to Blockaid."
)


def _load_make5_stories() -> list[dict]:
    payload = json.loads(MAKE5_BATCH.read_text(encoding="utf-8"))
    stories = payload["telemetry"]["viability"]["stories"]
    by_id = {row["event_id"]: row for row in stories}
    return [by_id[event_id] for event_id in SELECTED]


def _old_style_messages(batch_id: str, stories: list[dict]) -> list[dict[str, str]]:
    payload = {
        "task": "Produce article-output-v1 for every requested event, or an explicit failure.",
        "batch_id": batch_id,
        "requested_event_ids": [row.get("event_id") for row in stories],
        "stories": [],
    }
    for index, row in enumerate(stories, start=1):
        article_input = row.get("article_input") if isinstance(row.get("article_input"), dict) else {}
        evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
        payload["stories"].append(
            {
                "event_id": row.get("event_id"),
                "rank": index,
                "evidence": evidence,
                "provenance": [
                    {
                        "event_id": row.get("event_id"),
                        "source": item.get("source"),
                        "url": item.get("url"),
                        "retrieved_at_utc": item.get("retrieved_at_utc"),
                        "source_type": item.get("source_type"),
                        "source_authority": item.get("source_authority"),
                        "research_only": True,
                    }
                    for item in evidence
                    if isinstance(item, dict)
                ],
                "evidence_metrics": article_input.get("evidence_sufficiency") or {},
            }
        )
    return [
        {"role": "system", "content": BATCH_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _old_request(stories: list[dict]) -> dict:
    from newsagent_v2.article.batch_prompt import batch_output_json_schema
    from newsagent_v2.providers.groq_article import BATCH_JSON_SCHEMA_NAME
    from newsagent_v2.providers.groq_editorial import DEFAULT_REASONING_EFFORT, GROQ_MODEL_ID

    return {
        "model": GROQ_MODEL_ID,
        "messages": _old_style_messages("20260915T063852Z-3ef218cc", stories),
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "include_reasoning": False,
        "max_completion_tokens": 32768,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": BATCH_JSON_SCHEMA_NAME,
                "strict": True,
                "schema": batch_output_json_schema(),
            },
        },
    }


class ExtractCleanupTests(unittest.TestCase):
    def test_ticker_block_removed_in_article_btc_kept(self) -> None:
        raw = (
            "Senate Republicans released revised CLARITY Act text. "
            + TICKER_BLOCK
            + " "
            + IN_STORY_BTC
        )
        cleaned = clean_extracted_article_text(raw)
        self.assertNotIn("DOGE $0.08298", cleaned)
        self.assertNotIn("TRX $0.3380", cleaned)
        self.assertIn("15 Bitcoin", cleaned)
        self.assertIn("$1.1 million", cleaned)

    def test_duplicated_byline_removed_attribution_kept(self) -> None:
        raw = BYLINE_DUP + " Lummis said the ethics provisions had been agreed to by Trump."
        cleaned = clean_extracted_article_text(raw)
        self.assertNotIn("Written by Felix Ng", cleaned)
        self.assertNotIn("Reviewed by Yohan Yun", cleaned)
        self.assertIn("Lummis said", cleaned)

    def test_related_and_latest_news_chrome_removed(self) -> None:
        raw = (
            "Odds of the CLARITY Act becoming law this year fell to 16%. "
            "Latest News Published Sep 15, 2026 Related: Unrelated sidebar headline. "
            "Source: \n Banking groups said the GOP text leaves stablecoin reward loopholes."
        )
        cleaned = clean_extracted_article_text(raw)
        self.assertNotIn("Latest News", cleaned)
        self.assertNotIn("Related:", cleaned)
        self.assertIn("16%", cleaned)
        self.assertIn("Banking groups said", cleaned)

    def test_publication_time_preserved_on_evidence_unit(self) -> None:
        stories = _load_make5_stories()
        compact = compact_story_evidence(stories[0])
        self.assertTrue(compact["evidence_units"])
        self.assertTrue(any(unit.get("published") for unit in compact["evidence_units"]))
        self.assertEqual(compact["evidence_units"][0]["source"], "Cointelegraph")


class CompactEvidenceTests(unittest.TestCase):
    def test_live_make5_cleanup_and_ids(self) -> None:
        stories = _load_make5_stories()
        self.assertEqual([row["event_id"] for row in stories], SELECTED)
        for row in stories:
            compact = compact_story_evidence(row)
            blob = json.dumps(compact)
            self.assertNotIn("extracted_text", blob)
            self.assertNotIn("factual_snippets", blob)
            self.assertTrue(compact["evidence_units"])
            self.assertEqual(
                compact["evidence_units"][0]["evidence_id"],
                evidence_id_for(row["event_id"], 1),
            )
            text = " ".join(unit["text"] for unit in compact["evidence_units"])
            self.assertNotIn("DOGE $", text)
            self.assertGreaterEqual(compact["prompt_compaction"]["evidence_words_after_compaction"], 80)
            self.assertLessEqual(
                compact["prompt_compaction"]["evidence_words_after_compaction"],
                MAX_PROMPT_EVIDENCE_WORDS_PER_STORY,
            )
            sufficiency = evidence_sufficiency(
                {
                    **row["article_input"],
                    "evidence": [
                        {
                            **item,
                            "extracted_text": clean_extracted_article_text(item.get("extracted_text") or ""),
                            "factual_snippets": [],
                        }
                        for item in row["article_input"]["evidence"]
                    ],
                }
            )
            self.assertEqual(sufficiency["status"], "SUFFICIENT_EVIDENCE")

    def test_numeric_attribution_chronology_retained(self) -> None:
        stories = {row["event_id"]: row for row in _load_make5_stories()}
        clarity = compact_story_evidence(stories["event-005"])
        text = " ".join(unit["text"] for unit in clarity["evidence_units"])
        self.assertIn("635", text)
        self.assertRegex(text, r"said|according to|told")
        self.assertTrue("Tuesday" in text or "Sunday" in text)
        hack = compact_story_evidence(stories["event-032"])
        hack_text = " ".join(unit["text"] for unit in hack["evidence_units"])
        self.assertIn("15", hack_text)
        self.assertIn("Blockaid", hack_text)

    def test_new_payload_does_not_duplicate_extract_and_snippets(self) -> None:
        stories = _load_make5_stories()
        user = json.loads(build_batch_messages(batch_id="replay-5", stories=stories)[1]["content"])
        serialized = json.dumps(user)
        self.assertNotIn("extracted_text", serialized)
        self.assertNotIn("factual_snippets", serialized)
        self.assertIn("evidence_units", serialized)
        self.assertEqual(user["requested_event_ids"], SELECTED)

    def test_deterministic_dedup(self) -> None:
        row = deepcopy(_load_make5_stories()[0])
        evidence = row["article_input"]["evidence"][1]
        evidence["factual_snippets"] = [evidence["extracted_text"], evidence["extracted_text"]]
        compact = compact_story_evidence(row)
        sentences = compact["evidence_units"][0]["text"]
        first = sentences.split(". ")[0]
        self.assertEqual(sentences.count(first), 1)

    def test_five_story_synthetic_under_admission_target(self) -> None:
        base = _load_make5_stories()
        stories = []
        for index in range(5):
            row = deepcopy(base[index % len(base)])
            row["event_id"] = f"event-syn-{index:03d}"
            row["article_input"] = deepcopy(row["article_input"])
            row["article_input"]["event_id"] = row["event_id"]
            stories.append(row)
        body = build_top5_article_batch_request_body(batch_id="five-synth", stories=stories)
        diag = safe_chat_request_diagnostics(body)
        self.assertEqual(diag["max_completion_tokens"], 16384)
        self.assertLessEqual(diag["estimated_admission_tokens"], GROQ_BATCH_ADMISSION_TARGET)
        self.assertLess(diag["estimated_admission_tokens"], GROQ_ON_DEMAND_TPM_LIMIT)

    def test_make5_four_story_replay_under_limit(self) -> None:
        stories = _load_make5_stories()
        old = _old_request(stories)
        new = build_top5_article_batch_request_body(batch_id="20260915T063852Z-3ef218cc", stories=stories)
        old_diag = safe_chat_request_diagnostics(old)
        new_diag = safe_chat_request_diagnostics(new)
        self.assertGreaterEqual(old_diag["estimated_admission_tokens"], 9000)
        self.assertEqual(old_diag["max_completion_tokens"], 32768)
        self.assertEqual(new_diag["max_completion_tokens"], BATCH_MAX_COMPLETION_TOKENS)
        self.assertLess(new_diag["serialized_bytes"], old_diag["serialized_bytes"])
        self.assertLessEqual(new_diag["estimated_admission_tokens"], GROQ_BATCH_ADMISSION_TARGET)
        self.assertLess(new_diag["estimated_admission_tokens"], GROQ_ON_DEMAND_TPM_LIMIT)
        self.assertTrue(new_diag["admission_excludes_max_completion_tokens"])
        self.assertEqual(
            json.loads(new["messages"][1]["content"])["requested_event_ids"],
            SELECTED,
        )


class ArchitectureGuardTests(unittest.TestCase):
    def test_thresholds_and_single_call_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(WARN_SENTENCE_SIMILARITY, 0.84)
        self.assertEqual(DEFAULT_CANDIDATE_SCAN_LIMIT, 12)
        self.assertEqual(BATCH_RUNNER_SRC.count("post_chat_completion("), 1)
        self.assertNotIn("repair", BATCH_RUNNER_SRC.lower())
        self.assertIn("select_viable_stories", LIVE_SRC)
        result = wordpress_disabled_publish(story={})
        self.assertTrue(result["wordpress_disabled"])
        rows = [_sufficient_row(f"event-00{i}") for i in range(1, 6)]
        poster = RecordingPost(
            [FakeResponse(200, _payload([_article(f"event-00{i}") for i in range(1, 6)]))]
        )
        out = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="tpm-one")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(out["telemetry"]["editorial_ai_request_count"], 1)
        user = json.loads(poster.calls[0]["json"]["messages"][1]["content"])
        self.assertIn("evidence_units", json.dumps(user))
        self.assertNotIn("NewsAgent-Local", Path(inspect.getsourcefile(compact_story_evidence)).read_text(encoding="utf-8"))

    def test_old_live_diagnostics_match_serialized_not_output_budget(self) -> None:
        # Persisted #5: Groq Requested 9778 vs estimated_serialized_tokens 9977.
        # 9778 != 9009+32768, so max_completion_tokens did not add to that TPM figure.
        self.assertNotEqual(9009 + 32768, 9778)
        self.assertLess(abs(9977 - 9778), 250)
        self.assertEqual(estimate_prompt_tokens_from_bytes(39906), 9977)


if __name__ == "__main__":
    unittest.main()


