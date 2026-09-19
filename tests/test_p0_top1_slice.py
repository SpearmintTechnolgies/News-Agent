from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from newsagent_v2.article.batch_prompt import (
    BATCH_SYSTEM_PROMPT,
    TOP1_WRITER_ADDENDUM,
    batch_output_json_schema,
    build_batch_messages,
)
from newsagent_v2.article.batch_runner import generate_make_articles
from newsagent_v2.article.enrich import MIN_DISTINCT_FACTS, MIN_EXTRACTED_WORDS
from newsagent_v2.article.expand import evidence_id_index, expand_provider_article
from newsagent_v2.article.prompt import SYSTEM_PROMPT, article_output_json_schema
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY, WARN_SENTENCE_SIMILARITY
from newsagent_v2.article.qa.structure import check_structure, extract_quoted_spans
from newsagent_v2.article.render import materialize_article, render_article_body
from newsagent_v2.approval.store import STATE_AWAITING_APPROVAL, STATE_IMAGE_FAILED, ApprovalStore
from newsagent_v2.batch.contract import MAKE_STORY_COUNT, TOP5_COUNT
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.batch.viability import select_viable_stories
from newsagent_v2.control import live as live_mod
from newsagent_v2.control.live import wordpress_disabled_publish
from newsagent_v2.control.make import run_make_generation
from newsagent_v2.control.make_summary import format_make_summary
from newsagent_v2.providers.groq_article import BATCH_MAX_COMPLETION_TOKENS
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import CHAT_ENV, TOKEN_ENV, load_telegram_config
from newsagent_v2.telegram.contract import ACK_MAKE_TEXT, BUSY_MAKE_TEXT
from tests.fixtures.article_qa import article_input, structured_exploit_article, structured_regulatory_article
from tests.test_groq_editorial_benchmark import FakeResponse
from tests.test_p0_make3_contract import _fetch_factory, _ranked
from tests.test_top5_single_call import ENV, RecordingPost, _article, _payload, _sufficient_row

REPO = Path(__file__).resolve().parents[1]
LIVE_SRC = inspect.getsource(live_mod)
BATCH_RUNNER_SRC = (REPO / "src" / "newsagent_v2" / "article" / "batch_runner.py").read_text(
    encoding="utf-8"
)
PIXEL = REPO / "tests" / "fixtures" / "telegram" / "pixel.png"
TOKEN = "1234567890:AA-test-token-value-not-real"
CHAT = "-1001234567890"


def _fold(text: str) -> str:
    return " ".join(text.split())


def _codes(result: dict, *, severity: str | None = None) -> set[str]:
    found: set[str] = set()
    for item in result.get("critical_failures") or []:
        if isinstance(item, dict) and item.get("code"):
            found.add(str(item["code"]))
    if severity == "critical":
        return found
    for item in result.get("warnings") or []:
        if isinstance(item, dict) and item.get("code"):
            found.add(str(item["code"]))
    return found


def _ensure_pixel() -> None:
    PIXEL.parent.mkdir(parents=True, exist_ok=True)
    if not PIXEL.is_file():
        PIXEL.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
            )
        )


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.n = 0

    def __call__(self, *args, **kwargs):
        self.n += 1
        self.calls.append({"args": args, "kwargs": kwargs, "method": kwargs.get("method")})
        return FakeResponse(200, {"ok": True, "result": {"message_id": 200 + self.n}})


class Top1SelectionTests(unittest.TestCase):
    def test_make_story_count_is_one_and_top5_untouched(self) -> None:
        self.assertEqual(MAKE_STORY_COUNT, 1)
        self.assertEqual(TOP5_COUNT, 5)

    def test_selects_only_first_viable_ranked_story(self) -> None:
        result = select_viable_stories(
            _ranked(12),
            target=MAKE_STORY_COUNT,
            scan_limit=12,
            fetch=_fetch_factory(blocked=set()),
        )
        self.assertEqual(result["final_selected_ids"], ["event-001"])
        self.assertEqual(result["selected_count"], 1)
        leftover = [row for row in result["candidates"] if row["event_id"] == "event-002"]
        self.assertEqual(leftover[0]["viability_status"], "not_scanned")
        self.assertEqual(result["stories"][0]["original_rank"], 1)

    def test_selection_is_dynamic_not_event_005(self) -> None:
        pipeline_src = inspect.getsource(live_mod.build_live_pipeline)
        self.assertIn("target=MAKE_STORY_COUNT", pipeline_src)
        self.assertIn("discover_ranked_top5", pipeline_src)
        self.assertNotIn("event-005", pipeline_src)
        result = select_viable_stories(
            _ranked(12),
            target=MAKE_STORY_COUNT,
            scan_limit=12,
            fetch=_fetch_factory(blocked=set()),
        )
        self.assertNotEqual(result["final_selected_ids"], ["event-005"])
        self.assertEqual(result["final_selected_ids"][0], "event-001")

    def test_insufficient_rank1_backfills_next_viable(self) -> None:
        result = select_viable_stories(
            _ranked(12),
            target=MAKE_STORY_COUNT,
            scan_limit=12,
            fetch=_fetch_factory(blocked={"event-001"}),
        )
        self.assertEqual(result["final_selected_ids"], ["event-002"])
        self.assertTrue(result["stories"][0]["backfill"])
        self.assertEqual(result["stories"][0]["original_rank"], 2)
        self.assertNotEqual(result["final_selected_ids"], ["event-005"])


class Top1WriterContractTests(unittest.TestCase):
    def test_one_story_one_groq_request(self) -> None:
        row = _sufficient_row("event-042")
        poster = RecordingPost([FakeResponse(200, _payload([_article("event-042")]))])
        result = generate_make_articles([row], environ=ENV, http_post=poster, batch_id="top1-one")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(result["http_request_count"], 1)
        body = json.loads(poster.calls[0]["json"]["messages"][1]["content"])
        self.assertEqual(body["requested_event_ids"], ["event-042"])
        self.assertEqual(len(body["stories"]), 1)
        self.assertNotEqual(body["requested_event_ids"], ["event-005"])

    def test_top1_addendum_only_for_one_story(self) -> None:
        one = build_batch_messages(batch_id="b1", stories=[_sufficient_row("event-042")])
        many = build_batch_messages(
            batch_id="b5",
            stories=[_sufficient_row(f"event-00{i}") for i in range(1, 3)],
        )
        self.assertIn(TOP1_WRITER_ADDENDUM, one[0]["content"])
        self.assertNotIn(TOP1_WRITER_ADDENDUM, many[0]["content"])
        folded = _fold(one[0]["content"])
        self.assertIn("complete standalone news ARTICLE", folded)
        self.assertIn("180-250 word news brief", folded)
        self.assertIn("5-7 substantive paragraphs", folded)

    def test_compact_schema_renderer_and_thresholds_retained(self) -> None:
        schema = batch_output_json_schema()
        self.assertEqual(list(schema["required"]), ["failures", "articles"])
        article_props = schema["properties"]["articles"]["items"]["properties"]
        self.assertNotIn("article_body", article_props)
        self.assertNotIn("evidence_refs", article_props["claims"]["items"]["properties"])
        self.assertIn("evidence_ids", article_props["claims"]["items"]["properties"])
        self.assertNotIn("article_body", article_output_json_schema()["properties"])
        self.assertIn("expand_provider_article", BATCH_RUNNER_SRC)
        self.assertIn("materialize_article", BATCH_RUNNER_SRC)
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)
        self.assertEqual(MIN_EXTRACTED_WORDS, 80)
        self.assertEqual(MIN_DISTINCT_FACTS, 8)
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)
        self.assertEqual(WARN_SENTENCE_SIMILARITY, 0.84)
        self.assertEqual(BATCH_MAX_COMPLETION_TOKENS, 16384)
        folded = _fold(BATCH_SYSTEM_PROMPT + " " + SYSTEM_PROMPT + " " + TOP1_WRITER_ADDENDUM)
        self.assertIn("HARD REQUIREMENT: Python-rendered article_body >= 350 words", folded)
        self.assertIn("TARGET: 450-800 words", folded)
        self.assertIn("no second rewrite or repair call", folded.lower())
        self.assertNotIn("repair", BATCH_RUNNER_SRC.lower())

    def test_evidence_id_expansion_and_renderer_retained(self) -> None:
        article = structured_regulatory_article()
        pack = article_input()
        compact = deepcopy(article)
        evidence_id = next(iter(evidence_id_index(pack)))
        compact["claims"][0]["id"] = compact["claims"][0]["claim_id"]
        compact["claims"][0]["evidence_ids"] = [evidence_id]
        compact["claims"][0].pop("evidence_refs", None)
        expanded = expand_provider_article(compact, pack)
        self.assertTrue(expanded["claims"][0]["evidence_refs"])
        body = render_article_body(article)
        self.assertGreater(len(body.split()), 50)
        materialize_article(article)
        self.assertTrue(article.get("article_body"))

    def test_claim_grounding_retained(self) -> None:
        result = run_article_qa(structured_exploit_article(), article_input())
        self.assertEqual(result["metrics"]["uncovered_assertive_sentence_count"], 0)
        folded = _fold(BATCH_SYSTEM_PROMPT + " " + TOP1_WRITER_ADDENDUM)
        self.assertIn("claim_ids", folded)
        self.assertIn("Do not attach unrelated claim IDs", folded)


class QuoteContractTests(unittest.TestCase):
    def test_quote_exact_substring_required(self) -> None:
        article = structured_regulatory_article()
        article["quotes"] = []
        article["article_sections"][0]["paragraphs"][0]["text"] = (
            article["article_sections"][0]["paragraphs"][0]["text"]
            + ' Northwind said, "12,400 customer records were involved."'
        )
        materialize_article(article)
        result = run_article_qa(article, article_input())
        self.assertIn("quote_body_unmapped", _codes(result, severity="critical"))

    def test_curly_quotes_require_ledger(self) -> None:
        article = structured_regulatory_article()
        article["quotes"] = []
        article["article_sections"][0]["paragraphs"][0]["text"] = (
            article["article_sections"][0]["paragraphs"][0]["text"]
            + " Northwind said, â€œ12,400 customer records were involved.â€"
        )
        issues = check_structure(article, article_input())
        self.assertTrue(any(item.get("code") == "quote_body_unmapped" for item in issues))

    def test_glued_multi_quote_ledger_fails(self) -> None:
        article = structured_regulatory_article()
        paragraph = article["article_sections"][0]["paragraphs"][0]
        paragraph["text"] = (
            paragraph["text"]
            + ' Officials said, "We opened a review." Later they added, "Records were involved."'
        )
        article["quotes"] = [
            {
                "text": '"We opened a review," they added. "Records were involved."',
                "kind": "direct",
                "attribution": "Officials",
                "evidence_refs": [{"url": "https://example.com/news/northwind-records-exposed", "source": "TestWire"}],
            }
        ]
        spans = extract_quoted_spans(paragraph["text"])
        self.assertEqual(spans, ["We opened a review.", "Records were involved."])
        issues = check_structure(article, article_input())
        self.assertTrue(any(item.get("code") == "quote_body_unmapped" for item in issues))

    def test_exact_quote_span_passes_structure(self) -> None:
        article = structured_regulatory_article()
        paragraph = article["article_sections"][0]["paragraphs"][0]
        paragraph["text"] = (
            paragraph["text"] + ' Northwind said, "12,400 customer records were involved."'
        )
        article["quotes"] = [
            {
                "text": "12,400 customer records were involved.",
                "kind": "direct",
                "attribution": "Northwind Payments",
                "evidence_refs": [{"url": "https://example.com/news/northwind-records-exposed", "source": "TestWire"}],
            }
        ]
        issues = check_structure(article, article_input())
        self.assertFalse(any(item.get("code") == "quote_body_unmapped" for item in issues))


class Top1ImageAndTelegramTests(unittest.TestCase):
    def setUp(self) -> None:
        _ensure_pixel()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.store = ApprovalStore(Path(self.tmpdir.name))
        self.config = load_telegram_config({TOKEN_ENV: TOKEN, CHAT_ENV: CHAT})
        self.transport = RecordingTransport()
        self.client = TelegramTestClient(self.config, transport=self.transport)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _story(self, *, qa_ok: bool = True, event_id: str = "event-syn-001") -> dict:
        article = structured_regulatory_article()
        article["event_id"] = event_id
        if not qa_ok:
            article["headline"] = ""
        pack = article_input()
        pack["event_id"] = event_id
        return {
            "event_id": event_id,
            "original_rank": 2,
            "viability_status": "selected",
            "article": article,
            "article_input": pack,
            "article_url": "https://example.com/event-syn-001",
            "source_count": 1,
            "evidence_sufficiency": {
                "status": "sufficient",
                "extracted_evidence_words": 120,
                "distinct_fact_count": 10,
            },
        }

    def _qa(self, article, article_input, **kwargs):
        ok = bool((article or {}).get("headline"))
        return {
            "event_id": article_input.get("event_id"),
            "publishable": ok,
            "qa_passed": ok,
            "metrics": {"article_word_count": 520 if ok else 220},
            "critical_failures": []
            if ok
            else [{"code": "empty_headline", "message": "headline is empty", "module": "headline"}],
        }

    def test_qa_pass_exactly_one_image_job(self) -> None:
        jobs: list[str] = []

        def image_fn(job):
            jobs.append(job["event_id"])
            return {
                "success": True,
                "event_id": job["event_id"],
                "final_path": str(PIXEL),
                "image_request_count": 1,
                "http_status": 200,
                "latency_ms": 12,
            }

        result = run_top5_batch(
            event_ids=["event-syn-001"],
            stories=[self._story()],
            qa_fn=self._qa,
            image_fn=image_fn,
        )
        self.assertEqual(jobs, ["event-syn-001"])
        self.assertEqual(result["telemetry"]["image_request_count"], 1)

    def test_qa_fail_zero_image_jobs(self) -> None:
        jobs: list[str] = []

        def image_fn(job):
            jobs.append(job["event_id"])
            return {"success": True, "event_id": job["event_id"], "final_path": str(PIXEL), "image_request_count": 1}

        result = run_top5_batch(
            event_ids=["event-syn-001"],
            stories=[self._story(qa_ok=False)],
            qa_fn=self._qa,
            image_fn=image_fn,
        )
        self.assertEqual(jobs, [])
        self.assertEqual(result["telemetry"]["image_request_count"], 0)
        self.assertFalse(result["stories"][0]["deliverable"])

    def test_image_success_sends_one_approval_card(self) -> None:
        result = run_make_generation(
            event_ids=["event-syn-001"],
            stories=[self._story()],
            qa_fn=self._qa,
            image_fn=lambda job: {
                "success": True,
                "event_id": job["event_id"],
                "final_path": str(PIXEL),
                "image_request_count": 1,
            },
            store=self.store,
            telegram_config=self.config,
            telegram_client=self.client,
            parallel_images=False,
            viability={"selected_count": 1, "scanned_count": 4, "target": 1},
        )
        photos = [call for call in self.transport.calls if call["kwargs"].get("method") == "sendPhoto"]
        self.assertEqual(len(photos), 1)
        self.assertEqual(len(result["approval_cards"]), 1)
        self.assertIn("ðŸ“° 1/1", result["approval_cards"][0]["caption"])
        self.assertIn("âœ… V2 TOP 1 READY â€” 1/1", result["completion_text"])
        self.assertTrue(result["telemetry"]["approval_card_sent"])
        story = self.store.read_story(result["batch_id"], "event-syn-001")
        self.assertEqual(story["state"], STATE_AWAITING_APPROVAL)
        self.assertEqual(story["original_rank"], 2)

    def test_image_failure_no_approval_ready(self) -> None:
        result = run_make_generation(
            event_ids=["event-syn-001"],
            stories=[self._story()],
            qa_fn=self._qa,
            image_fn=lambda job: {
                "success": False,
                "event_id": job["event_id"],
                "reason": "cloudflare_timeout",
                "image_request_count": 1,
                "http_status": 504,
            },
            store=self.store,
            telegram_config=self.config,
            telegram_client=self.client,
            parallel_images=False,
            viability={"selected_count": 1, "scanned_count": 4, "target": 1},
        )
        photos = [call for call in self.transport.calls if call["kwargs"].get("method") == "sendPhoto"]
        self.assertEqual(photos, [])
        self.assertEqual(result["approval_cards"], [])
        self.assertFalse(result["stories"][0]["deliverable"])
        self.assertIn("âš ï¸ V2 TOP 1 â€” IMAGE FAILED", result["completion_text"])
        self.assertFalse(result["telemetry"]["approval_card_sent"])
        story = self.store.read_story(result["batch_id"], "event-syn-001")
        self.assertEqual(story["state"], STATE_IMAGE_FAILED)


class TruthfulTelegramSummaryTests(unittest.TestCase):
    def test_no_viable_summary(self) -> None:
        text = format_make_summary(
            stories=[],
            telemetry={},
            viability={"selected_count": 0, "scanned_count": 12},
        )
        self.assertIn("âš ï¸ V2 TOP 1 â€” NO VIABLE STORY", text)
        self.assertIn("Scanned: 12", text)
        self.assertIn("Generation: not reached", text)

    def test_provider_failure_summary(self) -> None:
        text = format_make_summary(
            stories=[{"event_id": "event-042", "skip_reason": "provider_http_error"}],
            telemetry={
                "editorial": {
                    "http_status": 413,
                    "provider_error": {"provider_error_code": "rate_limit_tpm"},
                }
            },
            viability={"selected_count": 1, "scanned_count": 6},
        )
        self.assertIn("âŒ V2 TOP 1 â€” EDITORIAL FAILED", text)
        self.assertIn("Groq: HTTP 413", text)
        self.assertIn("Error: rate_limit_tpm", text)
        self.assertNotIn("gsk_", text)

    def test_qa_failure_summary(self) -> None:
        text = format_make_summary(
            stories=[
                {
                    "event_id": "event-042",
                    "article": {"headline": "Short", "article_sections": [{"paragraphs": []}]},
                    "qa_publishable": False,
                    "qa_result": {
                        "metrics": {"article_word_count": 214},
                        "critical_failures": [
                            {"code": "below_article_minimum_length"},
                            {"code": "quote_body_unmapped"},
                        ],
                    },
                }
            ],
            telemetry={"editorial": {"http_status": 200}},
            viability={"selected_count": 1},
        )
        self.assertIn("âš ï¸ V2 TOP 1 â€” ARTICLE FAILED QA", text)
        self.assertIn("Words: 214", text)
        self.assertIn("Structure: FAIL", text)
        self.assertIn("quote_body_unmapped", text)
        self.assertIn("Images: not reached", text)

    def test_image_failure_and_success_summaries(self) -> None:
        failed = format_make_summary(
            stories=[
                {
                    "event_id": "event-042",
                    "article": {"headline": "Ok"},
                    "qa_publishable": True,
                    "deliverable": False,
                    "skip_reason": "cloudflare_timeout",
                    "image": {"reason": "cloudflare_timeout"},
                }
            ],
            viability={"selected_count": 1},
        )
        self.assertIn("âš ï¸ V2 TOP 1 â€” IMAGE FAILED", text := failed)
        self.assertIn("Image provider: Cloudflare", text)
        success = format_make_summary(
            stories=[
                {
                    "event_id": "event-042",
                    "article": {"headline": "Ok"},
                    "qa_publishable": True,
                    "deliverable": True,
                }
            ],
            viability={"selected_count": 1},
            approval_cards=[{"event_id": "event-042", "ok": True}],
        )
        self.assertIn("âœ… V2 TOP 1 READY â€” 1/1", success)
        self.assertIn("Approval card: sent", success)


class IsolationTests(unittest.TestCase):
    def test_wordpress_disabled(self) -> None:
        result = wordpress_disabled_publish(story={})
        self.assertTrue(result["wordpress_disabled"])
        self.assertIsNone(result["url"])
        self.assertTrue(result["held"])

    def test_ack_is_top5(self) -> None:
        self.assertIn("latest 5 stories", ACK_MAKE_TEXT)
        self.assertIn("already running", BUSY_MAKE_TEXT)

    def test_aadi_hermes_anime_untouched(self) -> None:
        blocked = ("NewsAgent-Local", "Aadi", "Hermes", "Anime", "hermes-home")
        for rel in (
            "src/newsagent_v2/control/live.py",
            "src/newsagent_v2/control/make.py",
            "src/newsagent_v2/control/make_summary.py",
            "src/newsagent_v2/article/batch_prompt.py",
            "src/newsagent_v2/telegram/listener.py",
            "src/newsagent_v2/batch/viability.py",
        ):
            text = (REPO / rel).read_text(encoding="utf-8")
            for token in blocked:
                self.assertNotIn(token, text)
        self.assertNotIn("C:\\\\NewsAgent-Local", LIVE_SRC)


if __name__ == "__main__":
    unittest.main()


