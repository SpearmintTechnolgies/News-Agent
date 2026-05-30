#!/usr/bin/env python3
"""Deterministic batch orchestrator state machine — tells orchestrator what to do next."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402

STEPS = ("discover", "score", "audit", "content", "card")
STEP_AGENT = {
    "discover": "bl-discovery",
    "score": "bl-scorer",
    "audit": "bl-auditor",
    "content": "bl-content",
}
BATCH_PATH = Path.home() / ".openclaw" / "data" / "backlink_runs" / "active_batch.json"
MAX_RETRIES = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_batch() -> dict[str, Any]:
    if not BATCH_PATH.is_file():
        raise SystemExit("BATCH_ERROR: no active batch (run init first)")
    return json.loads(BATCH_PATH.read_text(encoding="utf-8"))


def _save_batch(batch: dict[str, Any]) -> None:
    BATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = BATCH_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(batch, indent=2) + "\n", encoding="utf-8")
    tmp.replace(BATCH_PATH)


def _spawn_message(step: str, workflow_id: str) -> str:
    verbs = {
        "discover": "Run discovery",
        "score": "Run scoring",
        "audit": "Run audit",
        "content": "Run content",
    }
    return (
        f"{verbs[step]} for workflow_id={workflow_id}. Follow your SOUL. "
        f"Do NOT return command output in your chat response. "
        f'Yield back ONLY the word "SUCCESS" or "FAILURE: reason".'
    )


def _workflow_state(workflow_id: str, db_path: str) -> str:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    return row.state


def cmd_init(args: argparse.Namespace) -> int:
    if args.discover_json:
        payload = json.loads(Path(args.discover_json).read_text(encoding="utf-8"))
    elif args.workflow_id:
        payload = {"created": [{"workflow_id": args.workflow_id}]}
    else:
        payload = json.load(sys.stdin)

    created = payload.get("created") or []
    workflow_ids = [str(item["workflow_id"]) for item in created if item.get("workflow_id")]

    batch: dict[str, Any] = {
        "batch_id": datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"),
        "db_path": args.db,
        "workflow_ids": workflow_ids,
        "current_index": 0,
        "current_step": STEPS[0],
        "phase": "await_spawn",
        "retries": {},
        "stats": {"cards_sent": 0, "archived": 0, "failed": 0, "skipped": 0},
        "updated_at": _now(),
    }
    _save_batch(batch)

    if not workflow_ids:
        print("ORCH_IDLE reason=no_workflows")
        return 0

    print(f"ORCH_BATCH_INIT count={len(workflow_ids)} batch_id={batch['batch_id']}")
    return cmd_next(argparse.Namespace(db=args.db))


def _retry_key(workflow_id: str, step: str) -> str:
    return f"{workflow_id}:{step}"


def _advance_step(batch: dict[str, Any], db_path: str) -> None:
    idx = batch["current_index"]
    step = batch["current_step"]

    if step == "card":
        batch["stats"]["cards_sent"] += 1
        idx += 1
        if idx >= len(batch["workflow_ids"]):
            batch["current_index"] = idx
            batch["phase"] = "done"
            return
        batch["current_index"] = idx
        batch["current_step"] = STEPS[0]
        batch["phase"] = "await_spawn"
        return

    step_idx = STEPS.index(step)
    batch["current_step"] = STEPS[step_idx + 1]
    batch["phase"] = "await_exec" if batch["current_step"] == "card" else "await_spawn"


def cmd_next(args: argparse.Namespace) -> int:
    batch = _load_batch()
    db_path = batch.get("db_path") or args.db

    if batch.get("phase") == "done" or batch["current_index"] >= len(batch["workflow_ids"]):
        stats = batch["stats"]
        print(
            f"ORCH_DONE cards_sent={stats['cards_sent']} archived={stats['archived']} "
            f"failed={stats['failed']} skipped={stats['skipped']}"
        )
        return 0

    idx = batch["current_index"]
    wf_id = batch["workflow_ids"][idx]
    step = batch["current_step"]
    phase = batch["phase"]

    state = _workflow_state(wf_id, db_path)
    if state == "ARCHIVED":
        batch["stats"]["archived"] += 1
        batch["current_index"] += 1
        batch["current_step"] = STEPS[0]
        batch["phase"] = "await_spawn"
        _save_batch(batch)
        print(f"ORCH_SKIP workflow_id={wf_id} reason=archived")
        return cmd_next(args)

    if step == "card" or phase == "await_exec":
        print(f"ORCH_EXEC action=send_card workflow_id={wf_id}")
        return 0

    if phase == "await_spawn":
        agent = STEP_AGENT[step]
        msg = _spawn_message(step, wf_id)
        print(f"ORCH_SPAWN agent={agent} workflow_id={wf_id} msg={msg}")
        return 0

    if phase == "await_validate":
        print(f"ORCH_VALIDATE step={step} workflow_id={wf_id}")
        return 0

    print(f"ORCH_ERROR unknown phase={phase}")
    return 1


def cmd_ack(args: argparse.Namespace) -> int:
    batch = _load_batch()
    db_path = batch.get("db_path") or args.db
    wf_id = args.workflow_id
    step = args.step

    if args.kind == "spawn":
        batch["phase"] = "await_validate"
        batch["updated_at"] = _now()
        _save_batch(batch)
        print(f"ORCH_ACK spawn workflow_id={wf_id} step={step}")
        return cmd_next(argparse.Namespace(db=db_path))

    if args.kind == "validate":
        key = _retry_key(wf_id, step)
        if args.result == "ok":
            batch["retries"].pop(key, None)
            _advance_step(batch, db_path)
            batch["updated_at"] = _now()
            _save_batch(batch)
            print(f"ORCH_ACK validate ok workflow_id={wf_id} step={step}")
            return cmd_next(argparse.Namespace(db=db_path))

        retries = batch["retries"].get(key, 0) + 1
        batch["retries"][key] = retries
        if retries > MAX_RETRIES:
            batch["stats"]["failed"] += 1
            batch["current_index"] += 1
            batch["current_step"] = STEPS[0]
            batch["phase"] = "await_spawn"
            batch["updated_at"] = _now()
            _save_batch(batch)
            print(f"ORCH_SKIP workflow_id={wf_id} reason=validate_failed step={step}")
            return cmd_next(argparse.Namespace(db=db_path))

        batch["phase"] = "await_spawn"
        batch["updated_at"] = _now()
        _save_batch(batch)
        print(f"ORCH_RETRY workflow_id={wf_id} step={step} attempt={retries + 1}")
        return cmd_next(argparse.Namespace(db=db_path))

    if args.kind == "exec":
        _advance_step(batch, db_path)
        if batch.get("phase") == "done" or batch["current_index"] >= len(batch["workflow_ids"]):
            batch["phase"] = "done"
        batch["updated_at"] = _now()
        _save_batch(batch)
        print(f"ORCH_ACK exec workflow_id={wf_id} step=card")
        return cmd_next(argparse.Namespace(db=db_path))

    print(f"ORCH_ERROR unknown ack kind={args.kind}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Backlink batch orchestrator state machine")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_db(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)

    p_init = sub.add_parser("init", help="Initialize batch from discover JSON")
    add_db(p_init)
    p_init.add_argument("--discover-json", help="Path to discover output JSON")
    p_init.add_argument("--workflow-id", help="Single workflow id")
    p_init.set_defaults(func=cmd_init)

    p_next = sub.add_parser("next", help="Print next orchestrator action")
    add_db(p_next)
    p_next.set_defaults(func=cmd_next)

    p_ack = sub.add_parser("ack", help="Acknowledge completed action")
    add_db(p_ack)
    p_ack.add_argument("kind", choices=["spawn", "validate", "exec"])
    p_ack.add_argument("--workflow-id", required=True)
    p_ack.add_argument("--step", required=True)
    p_ack.add_argument("--result", choices=["ok", "fail"], default="ok")
    p_ack.set_defaults(func=cmd_ack)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
