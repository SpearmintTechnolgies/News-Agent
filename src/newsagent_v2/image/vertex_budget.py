"""
Vertex cost-safety guards for NewsAgent V4 orchestration.

Persisted counters and frozen-image reuse. Never logs credentials.
No automatic retries. Fail closed on budget exhaustion.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_BUDGET_ROOT = REPO_ROOT / "output" / "vertex_budget"

# Hard defaults (temporary testing ceilings; overridable via env).
MAX_VERTEX_CALLS_PER_CANDIDATE = 1
MAX_VERTEX_CALLS_PER_BATCH_DEFAULT = 10
MAX_VERTEX_CALLS_PER_DAY_DEFAULT = 20
MAX_CONCURRENT_BATCHES = 1

ENV_VERTEX_ENABLED = "NEWSAGENT_V2_VERTEX_ENABLED"
ENV_MAX_BATCH = "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_BATCH"
ENV_MAX_DAY = "NEWSAGENT_V2_MAX_VERTEX_CALLS_PER_DAY"

CODE_BATCH_EXHAUSTED = "VERTEX_BATCH_BUDGET_EXHAUSTED"
CODE_DAILY_EXHAUSTED = "VERTEX_DAILY_BUDGET_EXHAUSTED"
CODE_VERTEX_DISABLED = "VERTEX_DISABLED"
CODE_CANDIDATE_EXHAUSTED = "VERTEX_CANDIDATE_BUDGET_EXHAUSTED"
CODE_CONCURRENT_BATCH = "VERTEX_CONCURRENT_BATCH_BLOCKED"

_FILE_LOCK = threading.Lock()


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def vertex_enabled(environ: dict[str, str] | None) -> bool:
    """
    Emergency kill switch.

    Default True only when explicitly set to a truthy value OR unset with
    legacy behavior? Spec: if false → absolutely NO Vertex call.
    Safer default for testing: require explicit true to enable live calls
    when reading the switch in guards. But existing one-image tests may
    expect enabled. Spec says NEWSAGENT_V2_VERTEX_ENABLED=true/false.

    Policy: missing key → treat as enabled ONLY if other Vertex config is
    present would surprise. Spec: "If false: absolutely NO". So:
    - "false"/"0"/"no"/"off" → disabled
    - "true"/"1"/"yes"/"on" → enabled
    - missing → enabled (do not break already-configured Vertex tests)
    """
    if environ is None:
        return True
    raw = str(environ.get(ENV_VERTEX_ENABLED, "")).strip().lower()
    if raw == "":
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return True


def max_batch_calls(environ: dict[str, str] | None) -> int:
    source = environ or {}
    raw = str(source.get(ENV_MAX_BATCH) or "").strip()
    if not raw:
        return MAX_VERTEX_CALLS_PER_BATCH_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_VERTEX_CALLS_PER_BATCH_DEFAULT


def max_daily_calls(environ: dict[str, str] | None) -> int:
    source = environ or {}
    raw = str(source.get(ENV_MAX_DAY) or "").strip()
    if not raw:
        return MAX_VERTEX_CALLS_PER_DAY_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return MAX_VERTEX_CALLS_PER_DAY_DEFAULT


@dataclass(frozen=True)
class VertexGuardDecision:
    allow: bool
    code: str | None
    reason: str | None
    image_already_exists: bool
    reuse_path: str | None
    batch_vertex_calls_used: int
    daily_vertex_calls_used: int
    event_id: str
    article_hash: str
    safe_telemetry: dict[str, Any]


class VertexBudgetLedger:
    """
    Process + disk durable Vertex call accounting.

    Reservation is persisted BEFORE the provider is invoked so a crash
    mid-call still consumes budget (fail-closed, no uncontrolled retry loop).
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
        environ: dict[str, str] | None = None,
        batch_id: str | None = None,
    ) -> None:
        self.root = root or DEFAULT_BUDGET_ROOT
        self.environ = dict(environ or {})
        self.batch_id = batch_id or "default-batch"
        self.root.mkdir(parents=True, exist_ok=True)
        self._batch_file = self.root / "batches" / f"{self.batch_id}.json"
        self._daily_file = self.root / "daily" / f"{_utc_day()}.json"
        self._freeze_index = self.root / "frozen_images_index.json"
        self._candidate_file = self.root / "candidates" / f"{self.batch_id}.json"
        self._active_file = self.root / "active_batch.json"

    def _load_batch(self) -> dict[str, Any]:
        data = _read_json(self._batch_file)
        if not data:
            return {"batch_id": self.batch_id, "vertex_calls": 0, "reservations": []}
        return data

    def _load_daily(self) -> dict[str, Any]:
        data = _read_json(self._daily_file)
        day = _utc_day()
        if not data or data.get("day") != day:
            return {"day": day, "vertex_calls": 0, "reservations": []}
        return data

    def _load_candidates(self) -> dict[str, Any]:
        data = _read_json(self._candidate_file)
        if not data:
            return {"batch_id": self.batch_id, "by_event": {}}
        by_event = data.get("by_event")
        if not isinstance(by_event, dict):
            by_event = {}
        return {"batch_id": self.batch_id, "by_event": by_event}

    def _load_freeze_index(self) -> dict[str, Any]:
        data = _read_json(self._freeze_index)
        entries = data.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        return {"entries": entries}

    @staticmethod
    def freeze_key(event_id: str, article_hash: str) -> str:
        return f"{event_id}::{article_hash}"

    def find_frozen_image(self, event_id: str, article_hash: str) -> dict[str, Any] | None:
        if not event_id or not article_hash:
            return None
        with _FILE_LOCK:
            index = self._load_freeze_index()
            row = index["entries"].get(self.freeze_key(event_id, article_hash))
            if not isinstance(row, dict):
                return None
            path = Path(str(row.get("branded_image_path") or ""))
            if not path.is_file():
                return None
            # Optional hash verify if recorded.
            expected = row.get("branded_image_hash")
            if expected:
                from newsagent_v2.image.validate import file_sha256

                if file_sha256(path) != expected:
                    return None
            return dict(row)

    def register_frozen_image(
        self,
        *,
        event_id: str,
        article_hash: str,
        branded_image_path: str,
        branded_image_hash: str,
        raw_image_path: str | None = None,
        raw_image_hash: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with _FILE_LOCK:
            index = self._load_freeze_index()
            payload = {
                "event_id": event_id,
                "article_hash": article_hash,
                "branded_image_path": branded_image_path,
                "branded_image_hash": branded_image_hash,
                "raw_image_path": raw_image_path,
                "raw_image_hash": raw_image_hash,
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "batch_id": self.batch_id,
            }
            if extra:
                # Never persist secrets.
                for key, value in extra.items():
                    low = str(key).lower()
                    if any(s in low for s in ("token", "password", "secret", "credential", "authorization")):
                        continue
                    payload[key] = value
            index["entries"][self.freeze_key(event_id, article_hash)] = payload
            _write_json_atomic(self._freeze_index, index)

    def batch_calls_used(self) -> int:
        with _FILE_LOCK:
            return int(self._load_batch().get("vertex_calls") or 0)

    def daily_calls_used(self) -> int:
        with _FILE_LOCK:
            return int(self._load_daily().get("vertex_calls") or 0)

    def candidate_calls_used(self, event_id: str) -> int:
        with _FILE_LOCK:
            by_event = self._load_candidates()["by_event"]
            row = by_event.get(event_id) if isinstance(by_event.get(event_id), dict) else {}
            return int((row or {}).get("vertex_calls") or 0)

    def try_begin_batch(self) -> tuple[bool, str | None]:
        """Enforce MAX_CONCURRENT_BATCHES=1 via persisted active marker.

        Safe to call from inside execute_make (MAKE_LOCK already held). Duplicate
        /make is blocked by MAKE_LOCK before the pipeline runs; this marker
        covers cross-process / crash restart safety.
        """
        with _FILE_LOCK:
            active = _read_json(self._active_file)
            if active.get("active") and active.get("batch_id") == self.batch_id:
                return True, None
            if active.get("active") and active.get("batch_id") and active.get("batch_id") != self.batch_id:
                # Stale marker older than 6 hours is cleared (crash safety without infinite lockout).
                started = str(active.get("started_at") or "")
                stale = True
                try:
                    started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - started_dt).total_seconds()
                    stale = age > 6 * 3600
                except Exception:
                    stale = True
                if not stale:
                    return False, CODE_CONCURRENT_BATCH
            _write_json_atomic(
                self._active_file,
                {
                    "active": True,
                    "batch_id": self.batch_id,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        return True, None

    def end_batch(self) -> None:
        with _FILE_LOCK:
            active = _read_json(self._active_file)
            if active.get("batch_id") == self.batch_id:
                _write_json_atomic(
                    self._active_file,
                    {
                        "active": False,
                        "batch_id": None,
                        "ended_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

    def preflight(
        self,
        *,
        event_id: str,
        article_hash: str,
    ) -> VertexGuardDecision:
        """Check kill switch, reuse, and budgets WITHOUT reserving."""
        batch_used = self.batch_calls_used()
        daily_used = self.daily_calls_used()
        base_telem = {
            "batch_vertex_calls_used": batch_used,
            "daily_vertex_calls_used": daily_used,
            "event_id": event_id,
            "article_hash": article_hash,
            "image_already_exists": False,
            "batch_id": self.batch_id,
        }
        if not vertex_enabled(self.environ):
            return VertexGuardDecision(
                allow=False,
                code=CODE_VERTEX_DISABLED,
                reason="NEWSAGENT_V2_VERTEX_ENABLED is false",
                image_already_exists=False,
                reuse_path=None,
                batch_vertex_calls_used=batch_used,
                daily_vertex_calls_used=daily_used,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry=base_telem,
            )

        frozen = self.find_frozen_image(event_id, article_hash)
        if frozen is not None:
            telem = dict(base_telem)
            telem["image_already_exists"] = True
            return VertexGuardDecision(
                allow=False,  # no new Vertex call
                code=None,
                reason="reuse_frozen_image",
                image_already_exists=True,
                reuse_path=str(frozen.get("branded_image_path")),
                batch_vertex_calls_used=batch_used,
                daily_vertex_calls_used=daily_used,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry=telem,
            )

        if self.candidate_calls_used(event_id) >= MAX_VERTEX_CALLS_PER_CANDIDATE:
            return VertexGuardDecision(
                allow=False,
                code=CODE_CANDIDATE_EXHAUSTED,
                reason="per-candidate Vertex limit reached",
                image_already_exists=False,
                reuse_path=None,
                batch_vertex_calls_used=batch_used,
                daily_vertex_calls_used=daily_used,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry=base_telem,
            )

        if batch_used >= max_batch_calls(self.environ):
            return VertexGuardDecision(
                allow=False,
                code=CODE_BATCH_EXHAUSTED,
                reason="batch Vertex ceiling reached",
                image_already_exists=False,
                reuse_path=None,
                batch_vertex_calls_used=batch_used,
                daily_vertex_calls_used=daily_used,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry=base_telem,
            )

        if daily_used >= max_daily_calls(self.environ):
            return VertexGuardDecision(
                allow=False,
                code=CODE_DAILY_EXHAUSTED,
                reason="daily Vertex ceiling reached",
                image_already_exists=False,
                reuse_path=None,
                batch_vertex_calls_used=batch_used,
                daily_vertex_calls_used=daily_used,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry=base_telem,
            )

        return VertexGuardDecision(
            allow=True,
            code=None,
            reason=None,
            image_already_exists=False,
            reuse_path=None,
            batch_vertex_calls_used=batch_used,
            daily_vertex_calls_used=daily_used,
            event_id=event_id,
            article_hash=article_hash,
            safe_telemetry=base_telem,
        )

    def reserve_call(
        self,
        *,
        event_id: str,
        article_hash: str,
    ) -> VertexGuardDecision:
        """
        Atomically reserve one Vertex call slot BEFORE provider execution.

        On success, counters are already incremented on disk.
        """
        with _FILE_LOCK:
            # Re-check under lock.
            if not vertex_enabled(self.environ):
                batch_used = int(self._load_batch().get("vertex_calls") or 0)
                daily_used = int(self._load_daily().get("vertex_calls") or 0)
                return VertexGuardDecision(
                    allow=False,
                    code=CODE_VERTEX_DISABLED,
                    reason="NEWSAGENT_V2_VERTEX_ENABLED is false",
                    image_already_exists=False,
                    reuse_path=None,
                    batch_vertex_calls_used=batch_used,
                    daily_vertex_calls_used=daily_used,
                    event_id=event_id,
                    article_hash=article_hash,
                    safe_telemetry={
                        "batch_vertex_calls_used": batch_used,
                        "daily_vertex_calls_used": daily_used,
                        "event_id": event_id,
                        "article_hash": article_hash,
                        "image_already_exists": False,
                    },
                )

            index = self._load_freeze_index()
            frozen = index["entries"].get(self.freeze_key(event_id, article_hash))
            if isinstance(frozen, dict) and Path(str(frozen.get("branded_image_path") or "")).is_file():
                batch_used = int(self._load_batch().get("vertex_calls") or 0)
                daily_used = int(self._load_daily().get("vertex_calls") or 0)
                return VertexGuardDecision(
                    allow=False,
                    code=None,
                    reason="reuse_frozen_image",
                    image_already_exists=True,
                    reuse_path=str(frozen.get("branded_image_path")),
                    batch_vertex_calls_used=batch_used,
                    daily_vertex_calls_used=daily_used,
                    event_id=event_id,
                    article_hash=article_hash,
                    safe_telemetry={
                        "batch_vertex_calls_used": batch_used,
                        "daily_vertex_calls_used": daily_used,
                        "event_id": event_id,
                        "article_hash": article_hash,
                        "image_already_exists": True,
                    },
                )

            candidates = self._load_candidates()
            by_event = candidates["by_event"]
            crow = by_event.get(event_id) if isinstance(by_event.get(event_id), dict) else {}
            cand_used = int((crow or {}).get("vertex_calls") or 0)
            if cand_used >= MAX_VERTEX_CALLS_PER_CANDIDATE:
                batch_used = int(self._load_batch().get("vertex_calls") or 0)
                daily_used = int(self._load_daily().get("vertex_calls") or 0)
                return VertexGuardDecision(
                    allow=False,
                    code=CODE_CANDIDATE_EXHAUSTED,
                    reason="per-candidate Vertex limit reached",
                    image_already_exists=False,
                    reuse_path=None,
                    batch_vertex_calls_used=batch_used,
                    daily_vertex_calls_used=daily_used,
                    event_id=event_id,
                    article_hash=article_hash,
                    safe_telemetry={
                        "batch_vertex_calls_used": batch_used,
                        "daily_vertex_calls_used": daily_used,
                        "event_id": event_id,
                        "article_hash": article_hash,
                        "image_already_exists": False,
                    },
                )

            batch = self._load_batch()
            daily = self._load_daily()
            batch_used = int(batch.get("vertex_calls") or 0)
            daily_used = int(daily.get("vertex_calls") or 0)
            batch_limit = max_batch_calls(self.environ)
            daily_limit = max_daily_calls(self.environ)

            if batch_used >= batch_limit:
                return VertexGuardDecision(
                    allow=False,
                    code=CODE_BATCH_EXHAUSTED,
                    reason="batch Vertex ceiling reached",
                    image_already_exists=False,
                    reuse_path=None,
                    batch_vertex_calls_used=batch_used,
                    daily_vertex_calls_used=daily_used,
                    event_id=event_id,
                    article_hash=article_hash,
                    safe_telemetry={
                        "batch_vertex_calls_used": batch_used,
                        "daily_vertex_calls_used": daily_used,
                        "event_id": event_id,
                        "article_hash": article_hash,
                        "image_already_exists": False,
                    },
                )
            if daily_used >= daily_limit:
                return VertexGuardDecision(
                    allow=False,
                    code=CODE_DAILY_EXHAUSTED,
                    reason="daily Vertex ceiling reached",
                    image_already_exists=False,
                    reuse_path=None,
                    batch_vertex_calls_used=batch_used,
                    daily_vertex_calls_used=daily_used,
                    event_id=event_id,
                    article_hash=article_hash,
                    safe_telemetry={
                        "batch_vertex_calls_used": batch_used,
                        "daily_vertex_calls_used": daily_used,
                        "event_id": event_id,
                        "article_hash": article_hash,
                        "image_already_exists": False,
                    },
                )

            # Persist reservation BEFORE provider call.
            stamp = datetime.now(timezone.utc).isoformat()
            reservation = {
                "event_id": event_id,
                "article_hash": article_hash,
                "reserved_at": stamp,
            }
            batch["vertex_calls"] = batch_used + 1
            batch.setdefault("reservations", []).append(reservation)
            daily["vertex_calls"] = daily_used + 1
            daily.setdefault("reservations", []).append(reservation)
            by_event[event_id] = {
                "vertex_calls": cand_used + 1,
                "article_hash": article_hash,
                "last_reserved_at": stamp,
            }
            candidates["by_event"] = by_event

            _write_json_atomic(self._batch_file, batch)
            _write_json_atomic(self._daily_file, daily)
            _write_json_atomic(self._candidate_file, candidates)

            new_batch = int(batch["vertex_calls"])
            new_daily = int(daily["vertex_calls"])
            return VertexGuardDecision(
                allow=True,
                code=None,
                reason=None,
                image_already_exists=False,
                reuse_path=None,
                batch_vertex_calls_used=new_batch,
                daily_vertex_calls_used=new_daily,
                event_id=event_id,
                article_hash=article_hash,
                safe_telemetry={
                    "batch_vertex_calls_used": new_batch,
                    "daily_vertex_calls_used": new_daily,
                    "event_id": event_id,
                    "article_hash": article_hash,
                    "image_already_exists": False,
                    "reserved": True,
                },
            )


def guarded_vertex_generate(
    *,
    ledger: VertexBudgetLedger,
    event_id: str,
    article_hash: str,
    generate_fn: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """
    Orchestration wrapper: reuse / budget / reserve / single generate_fn call.

    generate_fn must perform exactly one provider attempt (retries=0).
    """
    pre = ledger.preflight(event_id=event_id, article_hash=article_hash)
    if pre.image_already_exists and pre.reuse_path:
        return {
            "success": True,
            "event_id": event_id,
            "final_path": pre.reuse_path,
            "image_request_count": 0,
            "reused_frozen_image": True,
            "image_already_exists": True,
            "safe_telemetry": pre.safe_telemetry,
            "canonical_article_hash": article_hash,
        }
    if not pre.allow:
        return {
            "success": False,
            "event_id": event_id,
            "reason": pre.code or pre.reason or "vertex_guard_blocked",
            "image_failure_code": pre.code or "vertex_guard_blocked",
            "image_request_count": 0,
            "safe_telemetry": pre.safe_telemetry,
            "canonical_article_hash": article_hash,
        }

    reserved = ledger.reserve_call(event_id=event_id, article_hash=article_hash)
    if reserved.image_already_exists and reserved.reuse_path:
        return {
            "success": True,
            "event_id": event_id,
            "final_path": reserved.reuse_path,
            "image_request_count": 0,
            "reused_frozen_image": True,
            "image_already_exists": True,
            "safe_telemetry": reserved.safe_telemetry,
            "canonical_article_hash": article_hash,
        }
    if not reserved.allow:
        return {
            "success": False,
            "event_id": event_id,
            "reason": reserved.code or reserved.reason or "vertex_guard_blocked",
            "image_failure_code": reserved.code or "vertex_guard_blocked",
            "image_request_count": 0,
            "safe_telemetry": reserved.safe_telemetry,
            "canonical_article_hash": article_hash,
        }

    # Provider call — no retries at this layer.
    result = generate_fn()
    if not isinstance(result, dict):
        return {
            "success": False,
            "event_id": event_id,
            "reason": "invalid_generate_fn_result",
            "image_failure_code": "invalid_generate_fn_result",
            "image_request_count": 1,
            "safe_telemetry": reserved.safe_telemetry,
        }
    out = dict(result)
    out.setdefault("event_id", event_id)
    out["safe_telemetry"] = reserved.safe_telemetry
    out["canonical_article_hash"] = article_hash
    # Count this as the reserved provider attempt.
    out["image_request_count"] = int(out.get("image_request_count") or 1)
    if out.get("success") and out.get("final_path"):
        branded = Path(str(out["final_path"]))
        if branded.is_file():
            from newsagent_v2.image.validate import file_sha256

            ledger.register_frozen_image(
                event_id=event_id,
                article_hash=article_hash,
                branded_image_path=str(branded),
                branded_image_hash=str(out.get("branded_image_hash") or file_sha256(branded)),
                raw_image_path=out.get("raw_image_path"),
                raw_image_hash=out.get("raw_image_hash"),
                extra={"provider": out.get("provider"), "model": out.get("model")},
            )
    return out
