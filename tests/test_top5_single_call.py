from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path

from newsagent_v2.article.batch_runner import generate_make_articles, normalize_batch_output
from newsagent_v2.article.enrich import (
    STATUS_INSUFFICIENT,
    STATUS_SUFFICIENT,
    USER_AGENT,
    enrich_stories,
)
from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.batch.images import IMAGE_MAX_CONCURRENCY
from newsagent_v2.batch.runner import run_top5_batch
from newsagent_v2.control import live as live_mod
from newsagent_v2.image.providers.cloudflare import DEFAULT_MODEL
from newsagent_v2.providers.groq_article import BATCH_TIMEOUT_SECONDS
from tests.fixtures.article_qa import article_input, clean_article, copied_source_paragraph
from tests.test_article_qa import _codes
from tests.test_evidence_enrichment import _thin_story
from tests.test_groq_editorial_benchmark import FakeResponse

IDS = [f"event-00{i}" for i in range(1, 6)]
ENV = {"GROQ_API_KEY": "TEST_GROQ_OFFLINE_KEY"}
LIVE_SRC = inspect.getsource(live_mod)


def _article(event_id: str, *, amount: str = "12400") -> dict:
    article = clean_article()
    article["event_id"] = event_id
    article["headline"] = f"{event_id} incident report issued"
    article["dek"] = f"Officials described the {event_id} case."
    article["slug"] = f"{event_id}-incident-report"
    article["article_body"] = article["article_body"].replace("event-syn-001", event_id)
    article["generation_notes"] = f"grounded only in {event_id}"
    return article


def _sufficient_row(event_id: str, *, amount: str = "$12,400") -> dict:
    extracted = (
        f"Investigators at {event_id} Lab said Northwind lost {amount} on 14 September 2026. "
        "According to the company, 12,400 customer records were reviewed the same day. "
        "Staff announced that passports and payment histories were in the exposed set. "
        "The disclosure concerned a retail payments file rather than a vendor platform. "
        "Officials stated that notifications began overnight in UTC. "
        "The firm confirmed an internal review of the forged mail domain. "
        "A second briefing repeated the 14 September chronology and named Northwind Payments. "
        "No recovery total was published beyond the disclosed figure."
    )
    return {
        "event_id": event_id,
        "article_input": {
            "event_id": event_id,
            "evidence": [
                {
                    "source": f"Wire-{event_id}",
                    "url": f"https://example.com/{event_id}",
                    "title": f"Headline {event_id}",
                    "extracted_text": extracted,
                    "factual_snippets": [extracted],
                    "source_role": "primary_evidence",
                    "research_only": True,
                }
            ],
            "evidence_sufficiency": {"status": STATUS_SUFFICIENT, "extracted_evidence_words": 120},
        },
        "evidence_sufficiency": {"status": STATUS_SUFFICIENT},
        "article_url": f"https://example.com/{event_id}",
        "source_count": 1,
    }


def _insufficient_row(event_id: str) -> dict:
    row = _thin_story(event_id, words=16)
    row["skip_reason"] = STATUS_INSUFFICIENT
    row["evidence_sufficiency"] = {"status": STATUS_INSUFFICIENT}
    return row


def _payload(articles: list[dict], failures: list[dict] | None = None, usage: dict | None = None) -> dict:
    return {
        "id": "chatcmpl-batch-test",
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"articles": articles, "failures": failures or []},
                        ensure_ascii=False,
                    )
                }
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 111,
            "completion_tokens": 222,
            "total_tokens": 333,
        },
    }


class RecordingPost:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.calls: list[dict] = []
        self._responses = list(responses)

    def __call__(self, url, *, headers, json, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if not self._responses:
            raise AssertionError("unexpected extra provider HTTP call")
        return self._responses.pop(0)


class SingleCallEditorialTests(unittest.TestCase):
    def test_five_sufficient_events_invoke_provider_once(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        articles = [_article(event_id) for event_id in IDS]
        poster = RecordingPost([FakeResponse(200, _payload(articles))])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-five")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(result["http_request_count"], 1)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 1)
        body = poster.calls[0]["json"]
        user = json.loads(body["messages"][1]["content"])
        self.assertEqual(user["requested_event_ids"], IDS)
        self.assertEqual(len(user["stories"]), 5)
        self.assertEqual(poster.calls[0]["timeout"], BATCH_TIMEOUT_SECONDS)
        self.assertEqual(set(result["articles"]), set(IDS))

    def test_four_sufficient_one_insufficient_excluded_from_request(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS[:4]]
        rows.append(_insufficient_row(IDS[4]))
        articles = [_article(event_id) for event_id in IDS[:4]]
        poster = RecordingPost([FakeResponse(200, _payload(articles))])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-four")
        self.assertEqual(len(poster.calls), 1)
        user = json.loads(poster.calls[0]["json"]["messages"][1]["content"])
        self.assertEqual(user["requested_event_ids"], IDS[:4])
        self.assertNotIn(IDS[4], user["requested_event_ids"])
        self.assertEqual(result["failures"][IDS[4]]["code"], STATUS_INSUFFICIENT)
        self.assertTrue(result["completeness"]["ok"])
        self.assertIn(IDS[4], result["completeness"]["explicitly_failed_ids"])

    def test_zero_sufficient_invokes_provider_zero_times(self) -> None:
        rows = [_insufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost([])
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-zero")
        self.assertEqual(len(poster.calls), 0)
        self.assertEqual(result["http_request_count"], 0)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 0)

    def test_missing_requested_event_id_is_caught(self) -> None:
        requested = IDS[:4]
        parsed = {"articles": [_article(event_id) for event_id in IDS[:3]], "failures": []}
        normalized = normalize_batch_output(parsed, requested_ids=requested)
        self.assertIn(IDS[3], normalized["failures_by_id"])
        self.assertEqual(normalized["failures_by_id"][IDS[3]]["code"], "unaccounted_event")
        poster = RecordingPost([FakeResponse(200, _payload([_article(event_id) for event_id in IDS[:4]]))])
        rows = [_sufficient_row(event_id) for event_id in IDS]
        # five rows but provider omits one of the five sufficient ids
        omit = RecordingPost(
            [FakeResponse(200, _payload([_article(event_id) for event_id in IDS[:4]]))]
        )
        result = generate_make_articles(rows, environ=ENV, http_post=omit, batch_id="batch-miss")
        self.assertEqual(len(omit.calls), 1)
        self.assertEqual(result["failures"][IDS[4]]["code"], "unaccounted_event")

    def test_duplicate_event_id_rejected(self) -> None:
        parsed = {
            "articles": [_article("event-001"), _article("event-001"), _article("event-002")],
            "failures": [],
        }
        normalized = normalize_batch_output(parsed, requested_ids=["event-001", "event-002"])
        self.assertNotIn("event-001", normalized["articles_by_id"])
        self.assertEqual(normalized["failures_by_id"]["event-001"]["code"], "duplicate_event_ids")

    def test_unknown_event_id_rejected(self) -> None:
        ghost = _article("event-ghost")
        parsed = {"articles": [_article("event-001"), ghost], "failures": []}
        normalized = normalize_batch_output(parsed, requested_ids=["event-001"])
        self.assertNotIn("event-ghost", normalized["articles_by_id"])
        self.assertNotIn("event-ghost", normalized["failures_by_id"])
        self.assertIn("event-001", normalized["articles_by_id"])

    def test_missing_schema_version_is_stamped_locally(self) -> None:
        article = _article("event-001")
        article.pop("schema_version", None)
        normalized = normalize_batch_output(
            {"articles": [article], "failures": []},
            requested_ids=["event-001"],
        )
        stamped = normalized["articles_by_id"]["event-001"]
        self.assertEqual(stamped["schema_version"], "article-output-v1")

    def test_same_id_in_articles_and_failures_rejected(self) -> None:
        parsed = {
            "articles": [_article("event-001")],
            "failures": [{"event_id": "event-001", "code": "x", "reason": "y"}],
        }
        normalized = normalize_batch_output(parsed, requested_ids=["event-001"])
        self.assertNotIn("event-001", normalized["articles_by_id"])
        self.assertEqual(normalized["failures_by_id"]["event-001"]["code"], "event_id_overlap")

    def test_explicit_failure_continues_valid_articles(self) -> None:
        articles = [_article(event_id) for event_id in IDS[:4]]
        failures = [{"event_id": IDS[4], "code": "cannot_ground", "reason": "thin facts"}]
        poster = RecordingPost([FakeResponse(200, _payload(articles, failures))])
        rows = [_sufficient_row(event_id) for event_id in IDS]
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-fail-one")
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(set(result["articles"]), set(IDS[:4]))
        self.assertEqual(result["failures"][IDS[4]]["code"], "cannot_ground")
        self.assertEqual(rows[4]["skip_reason"], "cannot_ground")

    def test_no_per_story_writer_on_live_make_path(self) -> None:
        self.assertNotIn("run_groq_article_generation", LIVE_SRC)
        self.assertNotIn("run_groq_editorial_benchmark", LIVE_SRC)
        self.assertIn("run_v4_final_pipeline", LIVE_SRC)
        self.assertNotIn("generate_make_articles", LIVE_SRC)

    def test_fake_provider_count_matches_persisted_telemetry(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        poster = RecordingPost(
            [FakeResponse(200, _payload([_article(event_id) for event_id in IDS]))]
        )
        generated = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-tel")
        batch = run_top5_batch(
            event_ids=IDS,
            stories=rows,
            qa_fn=lambda article, article_input, **kwargs: {
                "event_id": article_input["event_id"],
                "publishable": False,
                "critical_failures": [{"code": "forced", "message": "qa hold", "module": "test"}],
            },
            provider_telemetry=generated["telemetry"],
        )
        self.assertEqual(len(poster.calls), 1)
        self.assertEqual(
            generated["telemetry"]["editorial_ai_request_count"],
            batch["telemetry"]["editorial_ai_request_count"],
        )
        self.assertEqual(batch["telemetry"]["ai_request_count"], 1)
        self.assertEqual(batch["telemetry"]["editorial"]["provider"], "groq")

    def test_retries_are_counted(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        payload = _payload([_article(event_id) for event_id in IDS])
        poster = RecordingPost(
            [
                FakeResponse(429, {"error": "rate"}, {"Retry-After": "0"}),
                FakeResponse(200, payload),
            ]
        )
        result = generate_make_articles(
            rows,
            environ=ENV,
            http_post=poster,
            sleep=lambda _s: None,
            batch_id="batch-retry",
        )
        self.assertEqual(len(poster.calls), 2)
        self.assertEqual(result["telemetry"]["editorial_ai_request_count"], 1)
        self.assertEqual(result["telemetry"]["retries"], 1)
        self.assertEqual(result["telemetry"]["http_status"], 200)

    def test_token_totals_match_provider_response(self) -> None:
        rows = [_sufficient_row(event_id) for event_id in IDS]
        usage = {"prompt_tokens": 401, "completion_tokens": 902, "total_tokens": 1303, "cost": 0.012}
        poster = RecordingPost(
            [FakeResponse(200, _payload([_article(event_id) for event_id in IDS], usage=usage))]
        )
        result = generate_make_articles(rows, environ=ENV, http_post=poster, batch_id="batch-tokens")
        tel = result["telemetry"]
        self.assertEqual(tel["prompt_tokens"], 401)
        self.assertEqual(tel["completion_tokens"], 902)
        self.assertEqual(tel["total_tokens"], 1303)
        self.assertEqual(tel["provider_reported_cost_usd"], 0.012)
        self.assertIsNotNone(tel["estimated_list_price_usd"])
        self.assertNotEqual(tel["estimated_list_price_usd"], tel["provider_reported_cost_usd"])

    def test_no_image_generation_before_article_qa_pass(self) -> None:
        images: list[str] = []
        stories = [_insufficient_row(event_id) for event_id in IDS]
        run_top5_batch(
            event_ids=IDS,
            stories=stories,
            image_fn=lambda job: images.append(job["event_id"]) or {"success": True, "final_path": "x.png"},
        )
        self.assertEqual(images, [])

    def test_failed_article_generates_no_image(self) -> None:
        images: list[str] = []
        stories = []
        for event_id in IDS:
            row = _sufficient_row(event_id)
            row["article"] = _article(event_id)
            stories.append(row)

        def qa_fn(article, article_input, **kwargs):
            return {
                "event_id": article_input["event_id"],
                "publishable": False,
                "critical_failures": [
                    {"code": "below_hard_minimum_words", "message": "too short", "module": "depth"}
                ],
            }

        batch = run_top5_batch(
            event_ids=IDS,
            stories=stories,
            qa_fn=qa_fn,
            image_fn=lambda job: images.append(job["event_id"])
            or {"success": True, "final_path": "x.png", "image_request_count": 1},
        )
        self.assertEqual(images, [])
        self.assertEqual(batch["telemetry"]["image_request_count"], 0)

    def test_event_a_facts_cannot_contaminate_event_b(self) -> None:
        article_a_input = {
            "event_id": "event-A",
            "evidence": [
                {
                    "url": "https://example.com/a",
                    "source": "WireA",
                    "summary": "Protocol lost $47,318,221 after the exploit on 12 September 2026.",
                    "extracted_text": "Blockaid said $47,318,221 left the bridge on 12 September 2026.",
                }
            ],
        }
        article_b_input = {
            "event_id": "event-B",
            "evidence": [
                {
                    "url": "https://example.com/b",
                    "source": "WireB",
                    "summary": "The WTO director said stablecoins were about 3 percent of payments.",
                    "extracted_text": "The WTO director said stablecoins were about 3 percent of global payments in 2026.",
                }
            ],
        }
        article_b = clean_article()
        article_b["event_id"] = "event-B"
        article_b["article_body"] = (
            article_b["article_body"]
            + " The same briefing claimed $47,318,221 left a bridge, a figure that belongs to another event."
        )
        result = run_article_qa(
            article_b,
            article_b_input,
            other_article_inputs=[article_a_input, article_b_input],
        )
        self.assertFalse(result["publishable"])
        self.assertIn("cross_story_contamination", _codes(result, severity="critical"))

    def test_evidence_fetch_failures_fail_safely(self) -> None:
        story = _thin_story("event-019", words=16)
        story["article_input"]["evidence"][0]["url"] = "https://example.com/blocked"
        story["article_input"]["evidence"].append(
            {
                "source": "Alt",
                "url": "https://example.com/timeout",
                "title": "Alt",
                "summary": "thin",
                "source_role": "discovery",
            }
        )

        def fetch(url: str):
            if url.endswith("blocked"):
                return 403, "text/html", b"<html>forbidden article body 3 percent WTO</html>", url
            if url.endswith("timeout"):
                return 0, "", b"", url
            if url.endswith("rate"):
                return 429, "text/plain", b"slow down", url
            if url.endswith("empty"):
                return 200, "text/html", b"<html><script>window.__APP=[]</script></html>", url
            return 404, "", b"", url

        enriched = enrich_stories([story], fetch=fetch)[0]
        self.assertEqual(enriched["skip_reason"], STATUS_INSUFFICIENT)
        methods = [row.get("extraction_method") for row in enriched["article_input"]["evidence"]]
        self.assertTrue(any(str(item).startswith("fetch_blocked_403") for item in methods))
        self.assertTrue(any(item == "fetch_timeout_or_connection_error" for item in methods))
        self.assertFalse(any("3 percent" in str(row.get("extracted_text") or "") for row in enriched["article_input"]["evidence"]))

        rate_story = _thin_story("event-429", words=16)
        rate_story["article_input"]["evidence"][0]["url"] = "https://example.com/rate"
        rate_enriched = enrich_stories([rate_story], fetch=fetch)[0]
        self.assertTrue(rate_enriched["article_input"]["evidence"][0].get("access_blocked"))
        empty_story = _thin_story("event-empty", words=16)
        empty_story["article_input"]["evidence"][0]["url"] = "https://example.com/empty"
        empty_enriched = enrich_stories([empty_story], fetch=fetch)[0]
        self.assertEqual(
            empty_enriched["article_input"]["evidence"][0].get("extraction_method"),
            "empty_or_js_only_body",
        )

    def test_no_access_control_bypass_behavior(self) -> None:
        enrich_src = Path(inspect.getsourcefile(enrich_stories)).read_text(encoding="utf-8")
        self.assertNotRegex(enrich_src, r"archive\.(is|ph)|webcache|12ft|bypass|paywall.?proxy")
        self.assertIn(USER_AGENT, enrich_src)
        self.assertIn("NewsAgentV2-evidence/1.0", USER_AGENT)
        story = _thin_story("event-paywall", words=16)
        story["article_input"]["evidence"][0]["url"] = "https://example.com/paywall"

        def fetch(url: str):
            html = b"<html><p>Subscribe to continue reading the WTO director said 3 percent.</p></html>"
            return 200, "text/html", html, url

        enriched = enrich_stories([story], fetch=fetch)[0]
        row = enriched["article_input"]["evidence"][0]
        self.assertEqual(row.get("extraction_method"), "access_or_subscription_block")
        self.assertFalse(row.get("extracted_text"))

    def test_qa_hard_minimum_and_target_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_min_words, 450)
        self.assertEqual(NORMAL_ARTICLE_POLICY.target_max_words, 800)

    def test_source_similarity_qa_still_active(self) -> None:
        article = copied_source_paragraph()
        result = run_article_qa(article, article_input())
        codes = _codes(result, severity="critical")
        self.assertTrue("exact_phrase_overlap" in codes or "high_sentence_similarity" in codes)

    def test_image_architecture_constants_unchanged(self) -> None:
        self.assertEqual(IMAGE_MAX_CONCURRENCY, 3)
        self.assertEqual(DEFAULT_MODEL, "@cf/black-forest-labs/flux-2-klein-4b")
        self.assertIn("logo_only=True", LIVE_SRC)
        self.assertIn("wordpress_disabled_publish", LIVE_SRC)


if __name__ == "__main__":
    unittest.main()


