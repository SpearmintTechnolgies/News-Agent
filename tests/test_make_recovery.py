from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.approval.store import ApprovalStore
from newsagent_v2.article.writer.controlled.failures import (
    COPYRIGHT_SIMILARITY_FAILED,
    WRITER_PROVIDER_BUDGET_EXCEEDED,
)
from newsagent_v2.article.writer.controlled.pipeline import CompileResult
from newsagent_v2.control import live as live_mod
from newsagent_v2.control.make_recovery import recover_publishable_article, run_make_top1
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig

PIXEL = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "telegram" / "pixel.png"


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.n = 0

    def __call__(self, *args, **kwargs):
        self.n += 1
        self.calls.append({"kwargs": kwargs, "method": kwargs.get("method")})

        class Resp:
            status_code = 200

            def json(self_inner):
                return {"ok": True, "result": {"message_id": 300 + self.n}}

        return Resp()


def _story(event_id: str, rank: int) -> dict:
    return {"event_id": event_id, "original_rank": rank, "article_input": {"event_id": event_id}}


def _eligible(_story: dict) -> dict:
    return {"eligible": True, "planned_safe_words": 500, "capacity_class": "SUFFICIENT_EVIDENCE"}


def _fail(code: str):
    def compile_fn(story: dict) -> CompileResult:
        return CompileResult(ok=False, event_id=str(story["event_id"]), failure_class=code)

    return compile_fn


def _pass(story: dict) -> CompileResult:
    article = {
        "event_id": story["event_id"],
        "headline": f"Headline {story['event_id']}",
        "dek": "Dek for the frozen article.",
        "article_body": ("body " * 400).strip(),
    }
    qa = {
        "qa_passed": True,
        "publishable": True,
        "metrics": {"article_word_count": 400, "body_claim_coverage": 1.0, "exact_overlap_count": 0},
        "critical_failures": [],
    }
    return CompileResult(ok=True, event_id=str(story["event_id"]), article=article, qa=qa)


class MakeRecoveryTests(unittest.TestCase):
    def test_qa_failure_continues_to_reserve(self) -> None:
        calls: list[str] = []

        def compile_fn(story: dict) -> CompileResult:
            calls.append(str(story["event_id"]))
            if story["event_id"] == "e1":
                return CompileResult(
                    ok=False,
                    event_id="e1",
                    failure_class=COPYRIGHT_SIMILARITY_FAILED,
                    qa={"qa_passed": False, "publishable": False, "critical_failures": [{"code": "exact_phrase_overlap"}]},
                )
            return _pass(story)

        result = recover_publishable_article(
            [_story("e1", 1), _story("e2", 2)],
            compile_fn=compile_fn,
            assess_fn=_eligible,
        )
        self.assertEqual(calls, ["e1", "e2"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["winner"]["story"]["event_id"], "e2")
        self.assertEqual(result["candidate_attempts"], 2)

    def test_budget_failure_continues(self) -> None:
        calls: list[str] = []

        def compile_fn(story: dict) -> CompileResult:
            calls.append(str(story["event_id"]))
            if story["event_id"] == "e1":
                return CompileResult(
                    ok=False,
                    event_id="e1",
                    failure_class=WRITER_PROVIDER_BUDGET_EXCEEDED,
                )
            return _pass(story)

        result = recover_publishable_article(
            [_story("e1", 1), _story("e2", 2)],
            compile_fn=compile_fn,
            assess_fn=_eligible,
        )
        self.assertEqual(calls, ["e1", "e2"])
        self.assertTrue(result["ok"])

    def test_first_qa_pass_stops_generation(self) -> None:
        calls: list[str] = []

        def compile_fn(story: dict) -> CompileResult:
            calls.append(str(story["event_id"]))
            return _pass(story)

        result = recover_publishable_article(
            [_story("e1", 1), _story("e2", 2), _story("e3", 3)],
            compile_fn=compile_fn,
            assess_fn=_eligible,
        )
        self.assertEqual(calls, ["e1"])
        self.assertEqual(result["candidate_attempts"], 1)
        self.assertTrue(result["ok"])

    def test_insufficient_candidate_is_researched_before_reject(self) -> None:
        researched: list[str] = []

        def assess(story: dict) -> dict:
            return {
                "eligible": bool(story.get("researched")),
                "planned_safe_words": 500 if story.get("researched") else 40,
                "capacity_class": "SUFFICIENT_EVIDENCE" if story.get("researched") else "INSUFFICIENT_EVIDENCE",
            }

        def research(story: dict) -> dict:
            researched.append(str(story["event_id"]))
            row = dict(story)
            row["researched"] = True
            return {"story": row}

        story = {
            "event_id": "e-research",
            "original_rank": 1,
            "article_input": {"evidence": [{"url": "https://example.invalid/story"}]},
        }
        result = recover_publishable_article(
            [story],
            compile_fn=_pass,
            assess_fn=assess,
            research_fn=research,
        )
        self.assertEqual(researched, ["e-research"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["winner"]["story"].get("_research_applied"))

    def test_max_attempt_bound(self) -> None:
        calls: list[str] = []

        def compile_fn(story: dict) -> CompileResult:
            calls.append(str(story["event_id"]))
            return CompileResult(ok=False, event_id=str(story["event_id"]), failure_class="MECHANICS_FAILED")

        stories = [_story(f"e{i}", i) for i in range(1, 8)]
        result = recover_publishable_article(
            stories,
            compile_fn=compile_fn,
            assess_fn=_eligible,
            max_generations=5,
        )
        self.assertEqual(len(calls), 5)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "NO SAFE ARTICLE")

    def test_image_only_after_qa_pass_and_telegram_only_after_image(self) -> None:
        image_jobs: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            store = ApprovalStore(root=Path(tmp) / "approval")
            config = TelegramConfig(bot_token="1234567890:AA-test-token-value-not-real", test_chat_id="-1001")
            transport = RecordingTransport()
            client = TelegramTestClient(config, transport=transport, live_send_enabled=True)

            def image_fail(job):
                image_jobs.append("fail")
                return {"success": False, "reason": "flux_down", "image_request_count": 1}

            failed = run_make_top1(
                environ={},
                store=store,
                telegram_config=config,
                telegram_client=client,
                compile_fn=_pass,
                image_fn=image_fail,
                stories=[_story("e1", 1)],
                assess_fn=_eligible,
            )
            self.assertEqual(failed["status"], "IMAGE FAILED")
            self.assertEqual(image_jobs, ["fail"])
            self.assertFalse(any(row.get("method") == "sendPhoto" or "sendPhoto" in str(row) for row in transport.calls))

            def image_ok(job):
                image_jobs.append("ok")
                PIXEL.parent.mkdir(parents=True, exist_ok=True)
                if not PIXEL.is_file():
                    PIXEL.write_bytes(
                        bytes.fromhex(
                            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
                        )
                    )
                return {"success": True, "final_path": str(PIXEL), "image_request_count": 1}

            ok = run_make_top1(
                environ={},
                store=store,
                telegram_config=config,
                telegram_client=client,
                compile_fn=_pass,
                image_fn=image_ok,
                stories=[_story("e2", 1)],
                assess_fn=_eligible,
            )
            self.assertEqual(ok["status"], "SUCCESS")
            self.assertIn("ok", image_jobs)
            self.assertTrue(any("sendPhoto" in str(row) or row.get("kwargs", {}).get("files") for row in transport.calls))

    def test_make_pipeline_uses_recovery_orchestrator(self) -> None:
        src = inspect.getsource(live_mod.build_live_pipeline)
        self.assertIn("run_v4_final_pipeline", src)
        self.assertIn("discover_ranked_top5", src)
        self.assertIn("target=MAKE_STORY_COUNT", src)
        self.assertNotIn("generate_make_articles", src)


if __name__ == "__main__":
    unittest.main()


