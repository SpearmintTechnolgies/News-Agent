"""Backup Vertex service account in the image chain: primary -> backup -> Nova (offline, mocked HTTP)."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

import newsagent_v2.image.vertex_make_image as vmi
from newsagent_v2.image.providers.nova_proxy import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_FALLBACK_ENABLED,
    NovaHttpResponse,
    NovaProxyImageProvider,
)
from newsagent_v2.image.providers.vertex_nano_banana import (
    VertexHttpResponse,
    VertexNanoBananaConfig,
    VertexNanoBananaImageProvider,
)
from newsagent_v2.image.vertex_backup import (
    BACKUP_MAX_CALLS_PER_DAY_DEFAULT,
    ENV_BACKUP_CREDENTIALS,
    ENV_BACKUP_ENABLED,
    ENV_BACKUP_LOCATION,
    ENV_BACKUP_MAX_DAY,
    ENV_BACKUP_MODEL,
    ENV_BACKUP_PROJECT,
    VertexBackupConfigError,
    VertexBackupDailyBudget,
    load_vertex_backup_config,
    max_backup_daily_calls,
    vertex_backup_enabled,
)
from newsagent_v2.image.vertex_budget import VertexBudgetLedger

REPO = Path(__file__).resolve().parents[1]
LOGO = REPO / "brand" / "coinnetwork_logo.png"
FAKE_KEY = "test-proxy-key-not-real-0123456789"
FAKE_SA_MARKER = "fake-backup-private-key-not-real"


def _png_bytes(width: int = 1280, height: int = 720) -> bytes:
    image = Image.new("RGB", (width, height), (18, 28, 44))
    draw = ImageDraw.Draw(image)
    for x in range(0, width, 40):
        draw.line([(x, 0), (width - x, height)], fill=(40 + x % 200, 90, 160), width=3)
    buf = BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _json_response(cls, status: int, payload: dict):
    return cls(status, json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, *, headers, json_body, timeout):
        self.calls.append({"url": url, "auth": headers.get("Authorization"), "json_body": json_body})
        if not self.responses:
            raise AssertionError("unexpected extra HTTP call")
        return self.responses.pop(0)


def _vertex_ok():
    return _json_response(VertexHttpResponse, 200, {
        "candidates": [{
            "content": {"parts": [{"inlineData": {"mimeType": "image/png",
                                                  "data": base64.b64encode(_png_bytes()).decode("ascii")}}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 40, "candidatesTokenCount": 1120, "totalTokenCount": 1160},
    })


def _vertex_429():
    return _json_response(VertexHttpResponse, 429, {"error": {
        "code": 429, "message": "Resource exhausted. Please try again later.", "status": "RESOURCE_EXHAUSTED"}})


def _vertex_403():
    return _json_response(VertexHttpResponse, 403, {"error": {
        "code": 403, "message": "Permission denied on resource project.", "status": "PERMISSION_DENIED"}})


def _vertex_safety():
    return _json_response(VertexHttpResponse, 200, {
        "candidates": [{"content": {"parts": []}, "finishReason": "IMAGE_SAFETY"}]})


def _nova_ok():
    return _json_response(NovaHttpResponse, 200, {
        "created": 1, "data": [{"b64_json": base64.b64encode(_png_bytes()).decode("ascii")}]})


ARTICLE = {
    "headline": "Bitcoin ETF Inflows Climb as Institutions Return After Rate Cut",
    "dek": "Spot bitcoin funds took in fresh money for a fifth straight session.",
    "body": "Spot bitcoin exchange-traded funds recorded inflows on Thursday.",
    "category": "Bitcoin News",
}


@pytest.fixture()
def sa_file(tmp_path: Path) -> Path:
    path = tmp_path / "creds" / "vertex-backup-sa.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "service_account", "private_key": FAKE_SA_MARKER}), encoding="utf-8")
    return path


def _env(sa_file: Path | None, **extra: str) -> dict[str, str]:
    env = {
        ENV_BASE_URL: "https://proxy.test/v1",
        ENV_API_KEY: FAKE_KEY,
        "NEWSAGENT_V2_VERTEX_ENABLED": "true",
        "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY": "25",
        "NEWSAGENT_V2_VERTEX_MODEL": "gemini-3.1-flash-image",
    }
    if sa_file is not None:
        env.update({
            ENV_BACKUP_ENABLED: "true",
            ENV_BACKUP_CREDENTIALS: str(sa_file),
            ENV_BACKUP_PROJECT: "backup-project",
            ENV_BACKUP_LOCATION: "global",
        })
    env.update(extra)
    return env


@pytest.fixture()
def harness(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(vmi, "MAKE_RUNS_ROOT", tmp_path / "make_runs")
    monkeypatch.setattr(vmi, "resolve_article_logo", lambda environ=None: LOGO)

    def build(env, vertex_responses, backup_responses, nova_responses):
        vertex_tx, backup_tx, nova_tx = Scripted(vertex_responses), Scripted(backup_responses), Scripted(nova_responses)
        vconfig = VertexNanoBananaConfig(project="primary-project", location="global",
                                         model="gemini-3.1-flash-image", credentials_path_set=False, auth_mode="adc")
        seen_backup_configs: list[VertexNanoBananaConfig] = []

        def vertex_factory(_config):
            return VertexNanoBananaImageProvider(vconfig, transport=vertex_tx,
                                                 access_token_provider=lambda: "primary-token-not-real")

        def backup_factory(config):
            seen_backup_configs.append(config)
            return VertexNanoBananaImageProvider(config, transport=backup_tx,
                                                 access_token_provider=lambda: "backup-token-not-real")

        fn = vmi.build_vertex_make_image_fn(
            env, batch_id="test-batch", budget_root=tmp_path / "budget",
            provider_factory=vertex_factory, backup_provider_factory=backup_factory,
            nova_provider_factory=lambda c: NovaProxyImageProvider(c, transport=nova_tx),
        )
        fn.seen_backup_configs = seen_backup_configs  # type: ignore[attr-defined]
        return fn, vertex_tx, backup_tx, nova_tx

    return build


def _job(event_id: str):
    return {"event_id": event_id, "article": dict(ARTICLE), "canonical_body_hash": "hash-" + event_id}


def _bundle_files(result: dict) -> dict[str, dict]:
    bundle = Path(result["bundle_dir"])
    return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in bundle.glob("*.json")}


def _assert_no_secrets_on_disk(root: Path) -> None:
    for path in (root / "make_runs").rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        for secret in (FAKE_KEY, FAKE_SA_MARKER, "backup-token-not-real", "primary-token-not-real"):
            assert secret not in text, (path, secret)


# ----- config --------------------------------------------------------------------------------


def test_backup_config_and_toggles(sa_file: Path):
    assert not vertex_backup_enabled({})
    assert not vertex_backup_enabled({ENV_BACKUP_ENABLED: "false"})
    assert vertex_backup_enabled({ENV_BACKUP_ENABLED: "TRUE"})
    assert max_backup_daily_calls({}) == BACKUP_MAX_CALLS_PER_DAY_DEFAULT == 25
    assert max_backup_daily_calls({ENV_BACKUP_MAX_DAY: "7"}) == 7
    assert max_backup_daily_calls({ENV_BACKUP_MAX_DAY: "junk"}) == 25
    config, cred = load_vertex_backup_config(_env(sa_file))
    assert (config.project, config.location, config.model) == ("backup-project", "global", "gemini-3.1-flash-image")
    assert cred == str(sa_file) and config.auth_mode == "service_account_file"
    override, _ = load_vertex_backup_config(_env(sa_file, **{ENV_BACKUP_MODEL: "other-image-model"}))
    assert override.model == "other-image-model"
    with pytest.raises(VertexBackupConfigError) as exc:
        load_vertex_backup_config(_env(sa_file, **{ENV_BACKUP_CREDENTIALS: str(sa_file) + ".missing",
                                                  ENV_BACKUP_PROJECT: ""}))
    assert "file not found" in str(exc.value) and ENV_BACKUP_PROJECT in str(exc.value)
    assert FAKE_SA_MARKER not in str(exc.value)


def test_backup_daily_budget_is_separate_and_capped(tmp_path: Path):
    budget = VertexBackupDailyBudget(root=tmp_path, environ={ENV_BACKUP_MAX_DAY: "2"})
    assert budget.try_reserve(event_id="a", article_hash="h", reason="r") == (True, 1)
    assert budget.try_reserve(event_id="b", article_hash="h", reason="r") == (True, 2)
    assert budget.try_reserve(event_id="c", article_hash="h", reason="r") == (False, 2)
    assert VertexBackupDailyBudget(root=tmp_path, environ={}).calls_used() == 2
    assert VertexBudgetLedger(root=tmp_path, environ={}, batch_id="x").daily_calls_used() == 0


# ----- chain ---------------------------------------------------------------------------------


def test_primary_success_never_touches_backup(harness, sa_file: Path):
    fn, vtx, btx, ntx = harness(_env(sa_file), [_vertex_ok()], [], [])
    result = fn(_job("evt-bk-ok"))
    assert result["success"] and result["provider"] == "vertex" and result["vertex_cred"] == "primary"
    assert btx.calls == [] and ntx.calls == []
    assert _bundle_files(result)["article_link.json"]["vertex_cred"] == "primary"


def test_primary_daily_cap_uses_backup(harness, sa_file: Path, tmp_path: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    fn, vtx, btx, ntx = harness(env, [], [_vertex_ok()], [])
    result = fn(_job("evt-bk-daily"))
    assert result["success"] is True, result
    assert result["provider"] == "vertex_backup" and result["vertex_cred"] == "backup"
    assert result["fallback_reason"] == "daily_budget_exhausted"
    assert result["fallback_chain"] == ["vertex", "vertex_backup"]
    assert vtx.calls == [] and len(btx.calls) == 1 and ntx.calls == []
    assert "/projects/backup-project/locations/global/" in btx.calls[0]["url"]
    assert btx.calls[0]["auth"] == "Bearer backup-token-not-real"
    assert result["image_request_count"] == 1 and result["vertex_backup_request_count"] == 1
    files = _bundle_files(result)
    assert files["article_link.json"]["image_provider"] == "vertex_backup"
    assert files["article_link.json"]["vertex_cred"] == "backup"
    assert files["image_freeze.json"]["provider"] == "vertex_backup"
    assert VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch").daily_calls_used() == 0
    assert VertexBackupDailyBudget(root=tmp_path / "budget", environ=env).calls_used() == 1
    _assert_no_secrets_on_disk(tmp_path)
    again = fn(_job("evt-bk-daily"))
    assert again["success"] and again.get("reused_frozen_image") and len(btx.calls) == 1


def test_primary_429_uses_backup(harness, sa_file: Path, tmp_path: Path):
    env = _env(sa_file)
    fn, vtx, btx, ntx = harness(env, [_vertex_429()], [_vertex_ok()], [])
    result = fn(_job("evt-bk-429"))
    assert result["success"] is True, result
    assert result["provider"] == "vertex_backup" and result["fallback_reason"] == "vertex_rate_limited"
    assert len(vtx.calls) == 1 and len(btx.calls) == 1 and ntx.calls == []
    assert result["vertex_request_count"] == 1 and result["vertex_backup_request_count"] == 1
    assert result["image_request_count"] == 2
    assert VertexBudgetLedger(root=tmp_path / "budget", environ=env, batch_id="test-batch").daily_calls_used() == 1
    _assert_no_secrets_on_disk(tmp_path)


def test_backup_429_falls_through_to_nova_with_reason(harness, sa_file: Path, tmp_path: Path):
    fn, vtx, btx, ntx = harness(_env(sa_file), [_vertex_429()], [_vertex_429()], [_nova_ok()])
    result = fn(_job("evt-bk-nova"))
    assert result["success"] is True, result
    assert result["provider"] == "nova" and result["fallback_reason"] == "vertex_rate_limited"
    assert result["vertex_backup_failure_code"] == "vertex_rate_limited"
    assert result["fallback_chain"] == ["vertex", "vertex_backup", "nova"]
    assert (result["vertex_request_count"], result["vertex_backup_request_count"], result["nova_request_count"]) == (1, 1, 1)
    assert result["image_request_count"] == 3
    backup_dirs = [p for p in (tmp_path / "make_runs").iterdir() if p.name.endswith("-vertex_backup")]
    receipt = json.loads((backup_dirs[0] / "provider_result.json").read_text(encoding="utf-8"))
    assert receipt["http_status"] == 429 and receipt["provider_reported_cost_usd"] == 0.0


def test_backup_non_quota_error_still_falls_through_to_nova(harness, sa_file: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    fn, vtx, btx, ntx = harness(env, [], [_vertex_403()], [_nova_ok()])
    result = fn(_job("evt-bk-403"))
    assert result["success"] is True and result["provider"] == "nova"
    assert result["vertex_backup_failure_code"] == "vertex_permission_denied"


def test_all_three_fail_reports_every_reason(harness, sa_file: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0")
    bad = _json_response(NovaHttpResponse, 500, {"error": {"message": "upstream exploded"}})
    fn, vtx, btx, ntx = harness(env, [], [_vertex_429()], [bad])
    result = fn(_job("evt-bk-allfail"))
    assert result["success"] is False
    assert result["image_failure_code"] == (
        "VERTEX_DAILY_BUDGET_EXHAUSTED; vertex backup failed: vertex_rate_limited; "
        "nova fallback failed: nova_http_error"
    )
    assert result["vertex_backup_failure_code"] == "vertex_rate_limited"
    assert result["nova_failure_code"] == "nova_http_error"


def test_backup_safety_block_does_not_go_to_nova(harness, sa_file: Path):
    fn, vtx, btx, ntx = harness(_env(sa_file), [_vertex_429()], [_vertex_safety()], [])
    result = fn(_job("evt-bk-safe"))
    assert result["success"] is False and ntx.calls == []
    assert result["image_failure_code"] == "vertex_rate_limited; vertex backup failed: vertex_safety_block"


def test_backup_daily_cap_skips_to_nova(harness, sa_file: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0", **{ENV_BACKUP_MAX_DAY: "0"})
    fn, vtx, btx, ntx = harness(env, [], [], [_nova_ok()])
    result = fn(_job("evt-bk-cap"))
    assert result["success"] is True and result["provider"] == "nova"
    assert btx.calls == [] and result["vertex_backup_failure_code"] == "VERTEX_BACKUP_DAILY_BUDGET_EXHAUSTED"
    assert result["image_request_count"] == 1


def test_backup_misconfigured_skips_to_nova(harness, sa_file: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0", **{ENV_BACKUP_CREDENTIALS: "/nope/missing.json"})
    fn, vtx, btx, ntx = harness(env, [], [], [_nova_ok()])
    result = fn(_job("evt-bk-miss"))
    assert result["success"] is True and result["provider"] == "nova"
    assert btx.calls == [] and result["vertex_backup_failure_code"] == "vertex_backup_config_incomplete"


def test_backup_only_when_nova_disabled(harness, sa_file: Path):
    env = _env(sa_file, NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY="0", **{ENV_FALLBACK_ENABLED: "false"})
    fn, vtx, btx, ntx = harness(env, [], [_vertex_429()], [])
    result = fn(_job("evt-bk-nonova"))
    assert result["success"] is False and ntx.calls == []
    assert result["image_failure_code"] == "VERTEX_DAILY_BUDGET_EXHAUSTED; vertex backup failed: vertex_rate_limited"


def test_backup_disabled_keeps_previous_chain(harness):
    fn, vtx, btx, ntx = harness(_env(None), [_vertex_429()], [], [_nova_ok()])
    result = fn(_job("evt-bk-off"))
    assert result["success"] is True and result["provider"] == "nova"
    assert btx.calls == [] and "vertex_backup_failure_code" not in result and "fallback_chain" not in result
    assert result["image_request_count"] == 2
