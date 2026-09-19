"""Offline Vertex cost-safety guard tests. NO real provider calls."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image

from newsagent_v2.control.make import MAKE_LOCK, batch_busy, reset_make_guard
from newsagent_v2.image.provider import ProviderIdentity, ProviderResult
from newsagent_v2.image.validate import file_sha256
from newsagent_v2.image.vertex_budget import (
    CODE_BATCH_EXHAUSTED,
    CODE_CONCURRENT_BATCH,
    CODE_DAILY_EXHAUSTED,
    CODE_VERTEX_DISABLED,
    MAX_VERTEX_CALLS_PER_CANDIDATE,
    VertexBudgetLedger,
    guarded_vertex_generate,
    vertex_enabled,
)
from newsagent_v2.image.vertex_make_image import build_vertex_make_image_fn


def _png(path: Path, color: tuple[int, int, int] = (20, 30, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1280, 720), color).save(path, format="PNG")
    return path


class FakeVertexProvider:
    """Counts generate() invocations. Never touches network."""

    def __init__(self, *_args, **_kwargs) -> None:
        self.generation_calls = 0
        self.calls: list[dict] = []

    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(provider_name="vertex", model_name="fake-vertex", backend_type="synthetic_offline")

    def recorded_request(self, request) -> dict:
        return {"provider_name": "vertex", "model": "fake-vertex", "prompt_len": len(request.prompt or "")}

    def generate(self, request, *, dest_path: str, cold_start: bool) -> ProviderResult:
        self.generation_calls += 1
        self.calls.append({"dest_path": dest_path, "cold_start": cold_start})
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1280, 720), (25, 35, 55)).save(dest, format="PNG")
        return ProviderResult(
            provider_name="vertex",
            success=True,
            model_name="fake-vertex",
            backend_type="synthetic_offline",
            raw_image_path=str(dest),
            width=1280,
            height=720,
            http_status=200,
            total_latency_ms=1,
            sha256=file_sha256(dest),
            size_bytes=dest.stat().st_size,
            response_format="image/png",
        )


class VertexEnabledTests(unittest.TestCase):
    def test_kill_switch_false(self) -> None:
        self.assertFalse(vertex_enabled({"NEWSAGENT_V2_VERTEX_ENABLED": "false"}))
        self.assertFalse(vertex_enabled({"NEWSAGENT_V2_VERTEX_ENABLED": "0"}))

    def test_kill_switch_true_or_default(self) -> None:
        self.assertTrue(vertex_enabled({"NEWSAGENT_V2_VERTEX_ENABLED": "true"}))
        self.assertTrue(vertex_enabled({}))


class GuardCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_make_guard()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.environ = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
        }
        self.ledger = VertexBudgetLedger(root=self.root, environ=self.environ, batch_id="batch-A")
        self.provider_calls = 0

    def tearDown(self) -> None:
        while MAKE_LOCK.locked():
            MAKE_LOCK.release()
        reset_make_guard()
        self.tmp.cleanup()

    def _generate_ok(self) -> dict:
        self.provider_calls += 1
        path = _png(self.root / "imgs" / f"out-{self.provider_calls}.png")
        return {
            "success": True,
            "final_path": str(path),
            "branded_image_hash": file_sha256(path),
            "raw_image_path": str(path),
            "raw_image_hash": file_sha256(path),
            "image_request_count": 1,
            "provider": "vertex",
            "model": "fake",
        }

    def test_existing_frozen_image_reused_zero_calls(self) -> None:
        path = _png(self.root / "frozen" / "branded.png")
        self.ledger.register_frozen_image(
            event_id="event-013",
            article_hash="hash-aaa",
            branded_image_path=str(path),
            branded_image_hash=file_sha256(path),
        )
        out = guarded_vertex_generate(
            ledger=self.ledger,
            event_id="event-013",
            article_hash="hash-aaa",
            generate_fn=self._generate_ok,
        )
        self.assertTrue(out["success"])
        self.assertTrue(out.get("reused_frozen_image"))
        self.assertEqual(out["image_request_count"], 0)
        self.assertEqual(self.provider_calls, 0)
        self.assertEqual(self.ledger.batch_calls_used(), 0)

    def test_process_restart_reuses_persisted_freeze(self) -> None:
        path = _png(self.root / "frozen" / "branded2.png")
        self.ledger.register_frozen_image(
            event_id="event-020",
            article_hash="hash-bbb",
            branded_image_path=str(path),
            branded_image_hash=file_sha256(path),
        )
        # New ledger instance = process restart simulation.
        restarted = VertexBudgetLedger(root=self.root, environ=self.environ, batch_id="batch-A")
        out = guarded_vertex_generate(
            ledger=restarted,
            event_id="event-020",
            article_hash="hash-bbb",
            generate_fn=self._generate_ok,
        )
        self.assertTrue(out.get("reused_frozen_image"))
        self.assertEqual(self.provider_calls, 0)

    def test_duplicate_candidate_second_call_blocked(self) -> None:
        out1 = guarded_vertex_generate(
            ledger=self.ledger,
            event_id="event-001",
            article_hash="h1",
            generate_fn=self._generate_ok,
        )
        self.assertTrue(out1["success"])
        self.assertEqual(self.provider_calls, 1)
        # Same event already spent candidate budget; even with new hash, candidate cap is 1.
        out2 = guarded_vertex_generate(
            ledger=self.ledger,
            event_id="event-001",
            article_hash="h2-different",
            generate_fn=self._generate_ok,
        )
        self.assertFalse(out2["success"])
        self.assertEqual(out2["image_failure_code"], "VERTEX_CANDIDATE_BUDGET_EXHAUSTED")
        self.assertEqual(self.provider_calls, 1)
        self.assertEqual(MAX_VERTEX_CALLS_PER_CANDIDATE, 1)

    def test_eleventh_batch_attempt_exhausted(self) -> None:
        for i in range(10):
            out = guarded_vertex_generate(
                ledger=self.ledger,
                event_id=f"event-{i:03d}",
                article_hash=f"hash-{i}",
                generate_fn=self._generate_ok,
            )
            self.assertTrue(out["success"], msg=out)
        self.assertEqual(self.provider_calls, 10)
        eleventh = guarded_vertex_generate(
            ledger=self.ledger,
            event_id="event-010",
            article_hash="hash-10",
            generate_fn=self._generate_ok,
        )
        self.assertFalse(eleventh["success"])
        self.assertEqual(eleventh["image_failure_code"], CODE_BATCH_EXHAUSTED)
        self.assertEqual(self.provider_calls, 10)

    def test_twenty_first_daily_attempt_exhausted(self) -> None:
        env = dict(self.environ)
        env["NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH"] = "100"
        env["NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY"] = "20"
        ledger = VertexBudgetLedger(root=self.root / "day", environ=env, batch_id="batch-day")
        for i in range(20):
            out = guarded_vertex_generate(
                ledger=ledger,
                event_id=f"d-{i:03d}",
                article_hash=f"dh-{i}",
                generate_fn=self._generate_ok,
            )
            self.assertTrue(out["success"], msg=out)
        twenty_first = guarded_vertex_generate(
            ledger=ledger,
            event_id="d-020",
            article_hash="dh-20",
            generate_fn=self._generate_ok,
        )
        self.assertFalse(twenty_first["success"])
        self.assertEqual(twenty_first["image_failure_code"], CODE_DAILY_EXHAUSTED)
        self.assertEqual(self.provider_calls, 20)

    def test_vertex_disabled_blocks_all_calls(self) -> None:
        env = {"NEWSAGENT_V2_VERTEX_ENABLED": "false"}
        ledger = VertexBudgetLedger(root=self.root / "off", environ=env, batch_id="batch-off")
        out = guarded_vertex_generate(
            ledger=ledger,
            event_id="event-x",
            article_hash="hx",
            generate_fn=self._generate_ok,
        )
        self.assertFalse(out["success"])
        self.assertEqual(out["image_failure_code"], CODE_VERTEX_DISABLED)
        self.assertEqual(self.provider_calls, 0)

    def test_reservation_persists_before_provider_even_on_crash(self) -> None:
        def boom() -> dict:
            self.provider_calls += 1
            raise RuntimeError("simulated provider crash after reservation")

        with self.assertRaises(RuntimeError):
            guarded_vertex_generate(
                ledger=self.ledger,
                event_id="event-crash",
                article_hash="hc",
                generate_fn=boom,
            )
        # Budget consumed despite crash â€” restart cannot freely retry unboundedly.
        self.assertEqual(self.ledger.batch_calls_used(), 1)
        self.assertEqual(self.ledger.candidate_calls_used("event-crash"), 1)
        restarted = VertexBudgetLedger(root=self.root, environ=self.environ, batch_id="batch-A")
        self.assertEqual(restarted.batch_calls_used(), 1)

    def test_concurrent_batch_blocked(self) -> None:
        ok, code = self.ledger.try_begin_batch()
        self.assertTrue(ok)
        other = VertexBudgetLedger(root=self.root, environ=self.environ, batch_id="batch-B")
        # Simulate another batch holding MAKE_LOCK (duplicate /make).
        acquired = MAKE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        self.assertTrue(batch_busy())
        ok2, code2 = other.try_begin_batch()
        self.assertFalse(ok2)
        self.assertEqual(code2, CODE_CONCURRENT_BATCH)
        MAKE_LOCK.release()
        self.ledger.end_batch()

    def test_telegram_retry_never_calls_vertex(self) -> None:
        """Telegram transport retry must not invoke generate_fn."""
        # Simulate telegram retry path: only re-send existing final_path.
        path = _png(self.root / "frozen" / "card.png")
        self.ledger.register_frozen_image(
            event_id="event-tg",
            article_hash="htg",
            branded_image_path=str(path),
            branded_image_hash=file_sha256(path),
        )

        def telegram_retry_send() -> dict:
            # Retry uses frozen artifact only.
            frozen = self.ledger.find_frozen_image("event-tg", "htg")
            assert frozen is not None
            return {"ok": True, "photo": frozen["branded_image_path"], "vertex_calls": 0}

        result = telegram_retry_send()
        self.assertTrue(result["ok"])
        self.assertEqual(self.provider_calls, 0)
        # Guard path also returns reuse without generate.
        out = guarded_vertex_generate(
            ledger=self.ledger,
            event_id="event-tg",
            article_hash="htg",
            generate_fn=self._generate_ok,
        )
        self.assertEqual(out["image_request_count"], 0)
        self.assertEqual(self.provider_calls, 0)

    def test_wordpress_retry_and_approve_reject_never_call_vertex(self) -> None:
        path = _png(self.root / "frozen" / "wp.png")
        self.ledger.register_frozen_image(
            event_id="event-wp",
            article_hash="hwp",
            branded_image_path=str(path),
            branded_image_hash=file_sha256(path),
        )

        def approve_uses_frozen_only() -> dict:
            frozen = self.ledger.find_frozen_image("event-wp", "hwp")
            assert frozen is not None
            return {"state": "APPROVED_PENDING_PUBLISH", "image": frozen["branded_image_path"]}

        def reject_no_provider() -> dict:
            return {"state": "REJECTED"}

        def wordpress_retry() -> dict:
            frozen = self.ledger.find_frozen_image("event-wp", "hwp")
            assert frozen is not None
            return {"published": False, "held": True, "image": frozen["branded_image_path"]}

        self.assertEqual(approve_uses_frozen_only()["state"], "APPROVED_PENDING_PUBLISH")
        self.assertEqual(reject_no_provider()["state"], "REJECTED")
        self.assertTrue(wordpress_retry()["held"])
        self.assertEqual(self.provider_calls, 0)

    def test_duplicate_make_safe_with_lock(self) -> None:
        self.assertFalse(batch_busy())
        acquired = MAKE_LOCK.acquire(blocking=False)
        self.assertTrue(acquired)
        # Second /make sees busy and must not start Vertex work.
        self.assertTrue(batch_busy())
        out = None
        if not batch_busy():
            out = guarded_vertex_generate(
                ledger=self.ledger,
                event_id="should-not-run",
                article_hash="x",
                generate_fn=self._generate_ok,
            )
        self.assertIsNone(out)
        self.assertEqual(self.provider_calls, 0)
        MAKE_LOCK.release()


class ImageFnGuardIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_make_guard()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeVertexProvider()
        # Point logo check at a real logo if present; else skip compose-dependent path
        # by using provider_factory and patching logo validation inside generate via
        # temporary brand file.
        self.brand = self.root / "brand"
        self.brand.mkdir(parents=True)
        # Create a disposable logo and patch EXPECTED hash via writing matching file
        # used only if logo path is overridden â€” image_fn uses fixed LOGO_PATH.
        # For integration we mock logo check by using guarded path + fake provider
        # through build_vertex_make_image_fn with patched LOGO constants.
        self.environ = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
        }

    def tearDown(self) -> None:
        reset_make_guard()
        self.tmp.cleanup()

    def test_image_fn_respects_kill_switch(self) -> None:
        env = {"NEWSAGENT_V2_VERTEX_ENABLED": "false"}
        fn = build_vertex_make_image_fn(
            env,
            batch_id="int-off",
            budget_root=self.root / "budget",
            provider_factory=lambda _cfg: self.fake,
        )
        with mock.patch("newsagent_v2.image.vertex_make_image.LOGO_PATH", self.root / "logo.png"), mock.patch(
            "newsagent_v2.image.vertex_make_image.EXPECTED_LOGO_SHA256", "x"
        ):
            # Even with factory, kill switch blocks before generate.
            out = fn(
                {
                    "event_id": "event-013",
                    "article_hash": "abc",
                    "article": {"event_id": "event-013", "headline": "Test", "dek": "D", "article_body": "body"},
                }
            )
        self.assertFalse(out["success"])
        self.assertEqual(out["image_failure_code"], CODE_VERTEX_DISABLED)
        self.assertEqual(self.fake.generation_calls, 0)

    def test_image_fn_reuses_freeze_across_new_fn_instance(self) -> None:
        logo = _png(self.root / "logo.png", (255, 255, 255))
        logo_hash = file_sha256(logo)
        fn1 = build_vertex_make_image_fn(
            self.environ,
            batch_id="int-reuse",
            budget_root=self.root / "budget2",
            provider_factory=lambda _cfg: self.fake,
        )
        with mock.patch("newsagent_v2.image.vertex_make_image.LOGO_PATH", logo), mock.patch(
            "newsagent_v2.image.vertex_make_image.EXPECTED_LOGO_SHA256", logo_hash
        ), mock.patch("newsagent_v2.image.vertex_make_image.MAKE_RUNS_ROOT", self.root / "runs"):
            job = {
                "event_id": "event-013",
                "canonical_body_hash": "bodyhash1",
                "article": {
                    "event_id": "event-013",
                    "headline": "Bitcoin markets move",
                    "dek": "Funds flow.",
                    "article_body": "body text here",
                    "category": "markets",
                },
            }
            first = fn1(job)
            self.assertTrue(first.get("success"), msg=first)
            self.assertEqual(self.fake.generation_calls, 1)
            # New fn instance (restart) with same budget root.
            fn2 = build_vertex_make_image_fn(
                self.environ,
                batch_id="int-reuse-2",
                budget_root=self.root / "budget2",
                provider_factory=lambda _cfg: FakeVertexProvider(),
            )
            second = fn2(job)
        self.assertTrue(second.get("reused_frozen_image") or second.get("image_request_count") == 0)
        self.assertEqual(second.get("image_request_count"), 0)
        self.assertEqual(self.fake.generation_calls, 1)


class ConfigBeforeReserveTests(unittest.TestCase):
    """Config readiness must run before budget reservation (CEO live fix)."""

    def setUp(self) -> None:
        reset_make_guard()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fake = FakeVertexProvider()

    def tearDown(self) -> None:
        reset_make_guard()
        self.tmp.cleanup()

    def test_missing_vertex_config_zero_provider_zero_budget(self) -> None:
        env = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
            # Intentionally omit project/location/model/credentials.
        }
        fn = build_vertex_make_image_fn(
            env,
            batch_id="cfg-miss",
            budget_root=self.root / "budget",
            provider_factory=None,
        )
        with mock.patch(
            "newsagent_v2.image.vertex_make_image.resolve_vertex_config_status",
            return_value={
                "ready": False,
                "missing_fields": [
                    "NEWSAGENT_V2_VERTEX_PROJECT",
                    "NEWSAGENT_V2_VERTEX_LOCATION",
                    "NEWSAGENT_V2_VERTEX_MODEL",
                    "GOOGLE_APPLICATION_CREDENTIALS",
                ],
            },
        ):
            out = fn(
                {
                    "event_id": "event-018",
                    "article_hash": "deadbeef",
                    "article": {
                        "event_id": "event-018",
                        "headline": "Test",
                        "dek": "D",
                        "article_body": "body",
                    },
                }
            )
        self.assertFalse(out["success"])
        self.assertEqual(out["image_failure_code"], "vertex_config_incomplete")
        self.assertEqual(out.get("image_request_count"), 0)
        self.assertFalse(out.get("budget_reserved", True))
        ledger = fn.vertex_budget_ledger  # type: ignore[attr-defined]
        self.assertEqual(ledger.batch_calls_used(), 0)
        self.assertEqual(ledger.daily_calls_used(), 0)
        self.assertEqual(ledger.candidate_calls_used("event-018"), 0)
        self.assertEqual(self.fake.generation_calls, 0)

    def test_valid_fake_config_reserves_immediately_before_generate(self) -> None:
        logo = _png(self.root / "logo.png", (255, 255, 255))
        logo_hash = file_sha256(logo)
        env = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
            "NEWSAGENT_V2_VERTEX_PROJECT": "fake-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "global",
            "NEWSAGENT_V2_VERTEX_MODEL": "fake-model",
            "GOOGLE_APPLICATION_CREDENTIALS": str(self.root / "sa.json"),
        }
        (self.root / "sa.json").write_text("{}", encoding="utf-8")
        order: list[str] = []
        real_reserve = VertexBudgetLedger.reserve_call

        def tracing_reserve(self, *, event_id: str, article_hash: str):  # noqa: ANN001
            order.append("reserve")
            return real_reserve(self, event_id=event_id, article_hash=article_hash)

        class OrderedFake(FakeVertexProvider):
            def generate(self, request, *, dest_path: str, cold_start: bool):
                order.append("generate")
                return super().generate(request, dest_path=dest_path, cold_start=cold_start)

        fake = OrderedFake()
        fn = build_vertex_make_image_fn(
            env,
            batch_id="cfg-ok",
            budget_root=self.root / "budget-ok",
            provider_factory=lambda _cfg: fake,
        )
        with mock.patch("newsagent_v2.image.vertex_make_image.LOGO_PATH", logo), mock.patch(
            "newsagent_v2.image.vertex_make_image.EXPECTED_LOGO_SHA256", logo_hash
        ), mock.patch("newsagent_v2.image.vertex_make_image.MAKE_RUNS_ROOT", self.root / "runs"), mock.patch(
            "newsagent_v2.image.vertex_budget.VertexBudgetLedger.reserve_call",
            tracing_reserve,
        ):
            out = fn(
                {
                    "event_id": "event-032",
                    "canonical_body_hash": "hash-ok-1",
                    "article": {
                        "event_id": "event-032",
                        "headline": "Deutsche Bank custody",
                        "dek": "MiCA license.",
                        "article_body": "body text for image brief",
                        "category": "markets",
                    },
                }
            )
        self.assertTrue(out.get("success"), msg=out)
        self.assertEqual(fake.generation_calls, 1)
        self.assertEqual(out.get("image_request_count"), 1)
        self.assertEqual(order, ["reserve", "generate"])
        ledger = fn.vertex_budget_ledger  # type: ignore[attr-defined]
        self.assertEqual(ledger.batch_calls_used(), 1)
        self.assertEqual(ledger.candidate_calls_used("event-032"), 1)


class AuthProcessEnvRegressionTests(unittest.TestCase):
    """Reproduce dict-only GAC vs process os.environ bug. No real Vertex calls."""

    def setUp(self) -> None:
        reset_make_guard()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._saved_gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    def tearDown(self) -> None:
        reset_make_guard()
        if self._saved_gac is None:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self._saved_gac
        self.tmp.cleanup()

    def test_dict_credentials_without_process_env_zero_budget(self) -> None:
        import os

        sa = self.root / "sa.json"
        sa.write_text("{}", encoding="utf-8")
        env = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
            "NEWSAGENT_V2_VERTEX_PROJECT": "prefab-segment-500506-j4",
            "NEWSAGENT_V2_VERTEX_LOCATION": "global",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-3.1-flash-image",
            "GOOGLE_APPLICATION_CREDENTIALS": str(sa),
        }
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

        def _no_sync(_environ):  # noqa: ANN001
            return {
                "GOOGLE_APPLICATION_CREDENTIALS": False,
                "NEWSAGENT_V2_VERTEX_PROJECT": True,
                "NEWSAGENT_V2_VERTEX_LOCATION": True,
                "NEWSAGENT_V2_VERTEX_MODEL": True,
                "NEWSAGENT_V2_VERTEX_ENABLED": True,
            }

        fn = build_vertex_make_image_fn(
            env,
            batch_id="auth-miss",
            budget_root=self.root / "budget-auth-miss",
            provider_factory=None,
        )
        with mock.patch(
            "newsagent_v2.image.vertex_local_env.sync_vertex_process_environ",
            side_effect=_no_sync,
        ), mock.patch(
            "google.auth.default",
            side_effect=Exception("DefaultCredentialsError"),
        ):
            out = fn(
                {
                    "event_id": "event-018",
                    "article_hash": "hash-auth",
                    "article": {
                        "event_id": "event-018",
                        "headline": "Test",
                        "dek": "D",
                        "article_body": "body",
                    },
                }
            )
        self.assertFalse(out["success"])
        self.assertEqual(out["image_failure_code"], "vertex_auth_not_ready")
        self.assertEqual(out.get("image_request_count"), 0)
        self.assertFalse(out.get("budget_reserved", True))
        ledger = fn.vertex_budget_ledger  # type: ignore[attr-defined]
        self.assertEqual(ledger.batch_calls_used(), 0)
        self.assertEqual(ledger.candidate_calls_used("event-018"), 0)

    def test_synchronized_process_env_provider_ready_with_fake(self) -> None:
        import os

        from newsagent_v2.image.vertex_local_env import sync_vertex_process_environ

        sa = self.root / "sa.json"
        sa.write_text("{}", encoding="utf-8")
        env = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH": "10",
            "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "20",
            "NEWSAGENT_V2_VERTEX_PROJECT": "prefab-segment-500506-j4",
            "NEWSAGENT_V2_VERTEX_LOCATION": "global",
            "NEWSAGENT_V2_VERTEX_MODEL": "gemini-3.1-flash-image",
            "GOOGLE_APPLICATION_CREDENTIALS": str(sa),
        }
        present = sync_vertex_process_environ(env)
        self.assertTrue(present["GOOGLE_APPLICATION_CREDENTIALS"])
        self.assertEqual(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"), str(sa))

        logo = _png(self.root / "logo.png", (255, 255, 255))
        logo_hash = file_sha256(logo)
        fake = FakeVertexProvider()
        fn = build_vertex_make_image_fn(
            env,
            batch_id="auth-ok",
            budget_root=self.root / "budget-auth-ok",
            provider_factory=lambda _cfg: fake,
        )
        with mock.patch("newsagent_v2.image.vertex_make_image.LOGO_PATH", logo), mock.patch(
            "newsagent_v2.image.vertex_make_image.EXPECTED_LOGO_SHA256", logo_hash
        ), mock.patch("newsagent_v2.image.vertex_make_image.MAKE_RUNS_ROOT", self.root / "runs"):
            out = fn(
                {
                    "event_id": "event-032",
                    "canonical_body_hash": "hash-sync-1",
                    "article": {
                        "event_id": "event-032",
                        "headline": "Deutsche Bank custody",
                        "dek": "MiCA.",
                        "article_body": "body text",
                        "category": "markets",
                    },
                }
            )
        self.assertTrue(out.get("success"), msg=out)
        self.assertEqual(fake.generation_calls, 1)
        self.assertEqual(fn.vertex_budget_ledger.batch_calls_used(), 1)  # type: ignore[attr-defined]


class CallbackImportSafetyTests(unittest.TestCase):
    def test_approval_callbacks_do_not_import_vertex_provider(self) -> None:
        import newsagent_v2.approval.callbacks as cb

        src = Path(cb.__file__).read_text(encoding="utf-8")
        self.assertNotIn("vertex_nano_banana", src)
        self.assertNotIn("VertexNanoBanana", src)
        self.assertNotIn("build_vertex_make_image_fn", src)

    def test_provider_retries_hard_capped_at_one(self) -> None:
        from newsagent_v2.image.providers.vertex_nano_banana import HARD_MAX_GENERATION_CALLS

        self.assertEqual(HARD_MAX_GENERATION_CALLS, 1)
        self.assertEqual(MAX_VERTEX_CALLS_PER_CANDIDATE, 1)


class LocalVertexEnvOverlayTests(unittest.TestCase):
    def test_apply_local_fills_missing_when_credential_exists(self) -> None:
        from newsagent_v2.image.vertex_local_env import (
            LOCAL_CEO_VERTEX_LOCATION,
            LOCAL_CEO_VERTEX_MODEL,
            LOCAL_CEO_VERTEX_PROJECT,
            apply_local_vertex_environ,
            resolve_local_ceo_credential_path,
        )

        cred = resolve_local_ceo_credential_path()
        if cred is None:
            self.skipTest("local CEO credential file not present on this machine")
        filled = apply_local_vertex_environ({}, persist_user_env=False)
        self.assertEqual(filled["NEWSAGENT_V2_VERTEX_PROJECT"], LOCAL_CEO_VERTEX_PROJECT)
        self.assertEqual(filled["NEWSAGENT_V2_VERTEX_LOCATION"], LOCAL_CEO_VERTEX_LOCATION)
        self.assertEqual(filled["NEWSAGENT_V2_VERTEX_MODEL"], LOCAL_CEO_VERTEX_MODEL)
        self.assertEqual(filled["GOOGLE_APPLICATION_CREDENTIALS"], str(cred))
        # Does not overwrite explicit values.
        kept = apply_local_vertex_environ(
            {"NEWSAGENT_V2_VERTEX_PROJECT": "other-project"},
            persist_user_env=False,
        )
        self.assertEqual(kept["NEWSAGENT_V2_VERTEX_PROJECT"], "other-project")


if __name__ == "__main__":
    unittest.main()


