from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from time import perf_counter

from newsagent_v2.article.batch_prompt import BATCH_SYSTEM_PROMPT
from newsagent_v2.article.batch_runner import generate_make_articles
from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS, STATUS_INSUFFICIENT
from newsagent_v2.article.prompt import SYSTEM_PROMPT
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import HIGH_SENTENCE_SIMILARITY, check_similarity
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.batch.viability import (
    DEFAULT_CANDIDATE_SCAN_LIMIT,
    MAX_CANDIDATE_SCAN_LIMIT,
    MIN_CANDIDATE_SCAN_LIMIT,
    resolve_candidate_scan_limit,
    select_viable_stories,
)
from newsagent_v2.cluster import EventCluster
from newsagent_v2.control import live as live_mod
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.models import NewsItem
from tests.fixtures.article_qa import (
    DIRECT_QUOTE_FROM_EVIDENCE,
    article_input,
    article_with_attributed_direct_quote,
    article_with_close_source_paraphrase,
    article_with_copied_source_sentence,
    claim_without_evidence,
    clean_article,
    clean_paraphrase_with_entity_overlap,
    copied_source_paragraph,
)
from tests.test_article_qa import _codes
from tests.test_evidence_enrichment import WTO_HTML, _thin_story
from tests.test_groq_editorial_benchmark import FakeResponse
from tests.test_top5_single_call import ENV, IDS, RecordingPost, _article, _payload, _sufficient_row

REPO = Path(__file__).resolve().parents[1]
LIVE_REPLAY = REPO / "tests" / "fixtures" / "live_batch_20260915"
BATCH_RUNNER_SRC = (REPO / "src" / "newsagent_v2" / "article" / "batch_runner.py").read_text(
    encoding="utf-8"
)
LIVE_SRC = inspect.getsource(live_mod)


def _fold(text: str) -> str:
    return " ".join(text.split())


def _load_replay(event_id: str) -> dict:
    return json.loads((LIVE_REPLAY / f"{event_id}.json").read_text(encoding="utf-8"))


def _cluster(event_id: str, *, url: str, source: str = "Wire") -> EventCluster:
    item = NewsItem(
        source=source,
        title=f"Headline {event_id} Northwind Payments 12,400 records",
        url=url,
        published="Mon, 14 Sep 2026 12:00:00 +0000",
        summary="Northwind Payments said 12,400 customer records were exposed after a spoof.",
        source_role="primary_evidence",
        source_authority=0.8,
    )
    cluster = EventCluster(event_id=event_id, representative=item, members=[item])
    cluster.event_score = 20.0 - int(event_id.split("-")[-1])
    return cluster


def _ranked(n: int = 12) -> list[EventCluster]:
    rows = []
    for index in range(1, n + 1):
        event_id = f"event-{index:03d}"
        source = "CoinDesk" if index in {4, 5} else "Wire"
        rows.append(_cluster(event_id, url=f"https://example.com/{event_id}", source=source))
    return rows


def _fetch_factory(*, blocked: set[str], viable_html: bytes = WTO_HTML.encode("utf-8")):
    def fetch(url: str):
        event_id = url.rstrip("/").rsplit("/", 1)[-1]
        if event_id in blocked:
            return 429, "text/plain", b"rate limited", url
        return 200, "text/html; charset=utf-8", viable_html, url

    return fetch


class WriterPromptContractTests(unittest.TestCase):
    def test_writer_prompt_requires_350_and_targets_450_800(self) -> None:
        folded = _fold(BATCH_SYSTEM_PROMPT + " " + SYSTEM_PROMPT)
        self.assertIn("HARD REQUIREMENT: Python-rendered article_body >= 350 words", folded)
        self.assertIn("HARD REQUIREMENT: the Python-rendered article_body must be at least 350 words", folded)
        self.assertIn("TARGET: 450-800 words", folded)
        self.assertIn("500-650", folded)
        self.assertIn("Never claim that length requirements are met", folded)

    def test_body_assertions_must_map_to_claims_with_evidence_refs(self) -> None:
        folded = _fold(BATCH_SYSTEM_PROMPT + " " + SYSTEM_PROMPT)
        self.assertIn("EVERY factual paragraph must include one or more claim_ids", folded)
        self.assertIn("Do not emit article_body", folded)
        self.assertIn("Every factual claim must have valid evidence_ids", folded)
        self.assertIn("Empty evidence_ids are forbidden", folded)
        schema = json.dumps(
            __import__("newsagent_v2.article.prompt", fromlist=["article_output_json_schema"]).article_output_json_schema()
        )
        self.assertIn('"minItems": 1', schema)

    def test_unsupported_generic_conclusion_is_forbidden(self) -> None:
        folded = _fold(BATCH_SYSTEM_PROMPT)
        self.assertIn("Do not invent generic concluding analysis", folded)
        self.assertIn("This development underscores", folded)
        self.assertIn("will be watching", folded)
        self.assertIn("could reshape the regulatory landscape", folded)

    def test_no_second_repair_llm_request(self) -> None:
        self.assertIn("no second rewrite or repair call", _fold(BATCH_SYSTEM_PROMPT).lower())
        self.assertEqual(BATCH_RUNNER_SRC.count("post_chat_completion("), 1)
        self.assertNotIn("repair", BATCH_RUNNER_SRC.lower())
        self.assertNotIn("run_groq_article_generation", LIVE_SRC)


class SimilarityLayerTests(unittest.TestCase):
    def test_genuine_source_copying_fails(self) -> None:
        result = run_article_qa(copied_source_paragraph(), article_input())
        codes = _codes(result, severity="critical")
        self.assertTrue("exact_phrase_overlap" in codes or "high_sentence_similarity" in codes)
        copied = article_with_copied_source_sentence()
        issues, metrics = check_similarity(copied, article_input())
        self.assertTrue(any(item["code"] == "exact_phrase_overlap" for item in issues) or metrics["max_similarity"] >= HIGH_SENTENCE_SIMILARITY)

    def test_direct_quote_does_not_create_false_copyright_failure(self) -> None:
        article = article_with_attributed_direct_quote()
        issues, _metrics = check_similarity(article, article_input())
        self.assertFalse(any(item["code"] == "exact_phrase_overlap" for item in issues))
        self.assertFalse(any(item["code"] == "high_sentence_similarity" for item in issues))
        self.assertIn(DIRECT_QUOTE_FROM_EVIDENCE, article["article_body"])

    def test_unavoidable_factual_phrases_do_not_create_false_copyright_failure(self) -> None:
        result = run_article_qa(clean_paraphrase_with_entity_overlap(), article_input())
        self.assertNotIn("exact_phrase_overlap", _codes(result, severity="critical"))
        self.assertNotIn("high_sentence_similarity", _codes(result, severity="critical"))

    def test_close_paraphrase_fails(self) -> None:
        article = article_with_close_source_paraphrase()
        issues, metrics = check_similarity(article, article_input())
        codes = {item["code"] for item in issues}
        self.assertTrue(
            "exact_phrase_overlap" in codes
            or "high_sentence_similarity" in codes
            or "elevated_sentence_similarity" in codes
            or metrics["max_similarity"] >= 0.84
        )

    def test_independent_rewrite_passes_similarity(self) -> None:
        result = run_article_qa(clean_article(), article_input())
        self.assertNotIn("exact_phrase_overlap", _codes(result, severity="critical"))
        self.assertNotIn("high_sentence_similarity", _codes(result, severity="critical"))
        self.assertTrue(result["publishable"])


class ViabilityBackfillTests(unittest.TestCase):
    def test_blocked_top_ranked_source_is_backfilled(self) -> None:
        result = select_viable_stories(
            _ranked(12),
            scan_limit=12,
            fetch=_fetch_factory(blocked={"event-004", "event-005"}),
        )
        self.assertEqual(
            result["final_selected_ids"],
            ["event-001", "event-002", "event-003", "event-006", "event-007"],
        )
        self.assertEqual(result["backfilled_ids"], ["event-006", "event-007"])
        rejected = {row["event_id"]: row for row in result["candidates"]}
        self.assertEqual(rejected["event-004"]["viability_status"], "insufficient_evidence")
        self.assertIn("fetch_blocked_429", rejected["event-004"]["rejection_reason"])

    def test_backfill_preserves_deterministic_rank_order(self) -> None:
        result = select_viable_stories(
            _ranked(12),
            scan_limit=12,
            fetch=_fetch_factory(blocked={"event-001"}),
        )
        self.assertEqual(
            result["final_selected_ids"],
            ["event-002", "event-003", "event-004", "event-005", "event-006"],
        )
        ranks = [row["original_rank"] for row in result["stories"]]
        self.assertEqual(ranks, sorted(ranks))

    def test_backfill_stops_after_five_viable_stories(self) -> None:
        result = select_viable_stories(
            _ranked(12),
            scan_limit=12,
            fetch=_fetch_factory(blocked=set()),
        )
        self.assertEqual(result["final_selected_ids"], [f"event-{i:03d}" for i in range(1, 6)])
        self.assertEqual(result["selected_count"], 5)
        leftover = [row for row in result["candidates"] if row["event_id"] == "event-006"]
        self.assertEqual(leftover[0]["viability_status"], "not_scanned")

    def test_bounded_scan_limit_enforced(self) -> None:
        self.assertEqual(DEFAULT_CANDIDATE_SCAN_LIMIT, 12)
        self.assertEqual(MIN_CANDIDATE_SCAN_LIMIT, 10)
        self.assertEqual(MAX_CANDIDATE_SCAN_LIMIT, 15)
        self.assertEqual(resolve_candidate_scan_limit({"NEWSAGENT_V2_CANDIDATE_SCAN_LIMIT": "99"}), 15)
        self.assertEqual(resolve_candidate_scan_limit({"NEWSAGENT_V2_CANDIDATE_SCAN_LIMIT": "3"}), 10)
        result = select_viable_stories(
            _ranked(12),
            scan_limit=6,
            fetch=_fetch_factory(blocked={f"event-{i:03d}" for i in range(1, 7)}),
        )
        self.assertEqual(result["final_selected_ids"], [])
        self.assertEqual(result["scan_limit"], 6)
        skipped = [row["event_id"] for row in result["candidates"] if row["viability_status"] == "skipped_scan_limit"]
        self.assertEqual(skipped, [f"event-{i:03d}" for i in range(7, 13)])

    def test_insufficient_candidate_never_enters_groq_request(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS[:3]]
        thin = _thin_story("event-004")
        thin["skip_reason"] = STATUS_INSUFFICIENT
        thin["evidence_sufficiency"] = {"status": STATUS_INSUFFICIENT}
        rows.append(thin)
        poster = RecordingPost([FakeResponse(200, _payload([_article(event_id) for event_id in IDS[:3]]))])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-skip-thin")
        body = json.loads(poster.calls[0]["json"]["messages"][1]["content"])
        self.assertEqual(body["requested_event_ids"], IDS[:3])
        self.assertNotIn("event-004", body["requested_event_ids"])
        self.assertEqual(result["failures"]["event-004"]["code"], STATUS_INSUFFICIENT)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 1)

    def test_final_groq_request_still_one(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost([FakeResponse(200, _payload([_article(event_id) for event_id in IDS]))])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-one")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 1)


class ThresholdAndIsolationTests(unittest.TestCase):
    def test_qa_and_evidence_thresholds_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)

    def test_images_only_for_qa_pass_and_wordpress_disabled(self) -> None:
        self.assertIn("if not row.get(\"qa_publishable\")", Path(inspect.getsourcefile(run_top5_batch)).read_text(encoding="utf-8"))
        result = wordpress_disabled_publish(story={})
        self.assertTrue(result["wordpress_disabled"])
        self.assertIsNone(result["url"])

    def test_aadi_hermes_anime_isolation_untouched(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/batch/viability.py",
            "src/newsagent_v2/control/live.py",
            "src/newsagent_v2/article/qa/similarity.py",
            "src/newsagent_v2/article/batch_prompt.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)
        self.assertIn("select_viable_stories", LIVE_SRC)
        self.assertNotIn("C:\\\\NewsAgent-Local", LIVE_SRC)


class ReplayAndTimingTests(unittest.TestCase):
    def test_replay_event_003_still_rejected(self) -> None:
        payload = _load_replay("event-003")
        result = run_article_qa(payload["article"], payload["article_input"])
        codes = _codes(result, severity="critical")
        self.assertFalse(result["publishable"])
        self.assertIn("body_assertion_not_in_claims", codes)
        self.assertIn("ungrounded_contextual_assertion", codes)
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 350)
        self.assertLess(result["metrics"]["article_word_count"], 450)

    def test_replay_event_029_still_rejected(self) -> None:
        payload = _load_replay("event-029")
        result = run_article_qa(payload["article"], payload["article_input"])
        codes = _codes(result, severity="critical")
        self.assertFalse(result["publishable"])
        self.assertIn("below_article_minimum_length", codes)
        self.assertLess(result["metrics"]["article_word_count"], 350)

    def test_replay_event_033_still_rejected(self) -> None:
        payload = _load_replay("event-033")
        result = run_article_qa(payload["article"], payload["article_input"])
        codes = _codes(result, severity="critical")
        self.assertFalse(result["publishable"])
        self.assertIn("below_article_minimum_length", codes)
        self.assertIn("ungrounded_contextual_assertion", codes)
        self.assertLess(result["metrics"]["article_word_count"], 350)

    def test_corrected_synthetic_qa_passes(self) -> None:
        result = run_article_qa(clean_article(), article_input())
        self.assertTrue(result["publishable"])
        self.assertGreaterEqual(result["metrics"]["article_word_count"], 350)
        self.assertEqual(result["metrics"]["uncovered_assertive_sentence_count"], 0)
        self.assertGreaterEqual(result["metrics"]["claims_with_evidence"], 1)
        self.assertNotIn("exact_phrase_overlap", _codes(result, severity="critical"))

    def test_claim_without_evidence_still_fails(self) -> None:
        result = run_article_qa(claim_without_evidence(), article_input())
        self.assertFalse(result["publishable"])
        self.assertTrue(
            {"claim_missing_evidence", "claim_unknown_evidence", "empty_evidence_refs"}
            & _codes(result, severity="critical")
            or any("evidence" in code for code in _codes(result, severity="critical"))
        )

    def test_total_elapsed_ms_covers_editorial_and_stage_fields(self) -> None:
        stories = []
        for event_id in IDS:
            row = _sufficient_row(event_id)
            row["article"] = clean_article()
            row["article"]["event_id"] = event_id
            stories.append(row)
        started = perf_counter() - 6.0
        batch = run_top5_batch(
            event_ids=IDS,
            stories=stories,
            qa_fn=lambda article, article_input, **kwargs: {
                "event_id": article.get("event_id"),
                "publishable": False,
                "critical_failures": [{"code": "forced", "message": "hold"}],
            },
            provider_telemetry={"latency_ms": 5000, "editorial_ai_request_count": 1},
            pipeline_started_at=started,
            stage_timings={"discovery_ms": 100, "enrichment_ms": 200, "editorial_ms": 5000},
        )
        telemetry = batch["telemetry"]
        self.assertGreaterEqual(telemetry["total_elapsed_ms"], telemetry["editorial_ms"])
        self.assertGreaterEqual(telemetry["editorial_ms"], 5000)
        for key in ("discovery_ms", "enrichment_ms", "editorial_ms", "qa_ms", "images_ms", "telegram_ms"):
            self.assertIn(key, telemetry)
            self.assertIsInstance(telemetry[key], int)


if __name__ == "__main__":
    unittest.main()


