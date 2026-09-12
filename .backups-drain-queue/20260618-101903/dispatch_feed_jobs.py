#!/usr/bin/env python3
"""dispatch_feed_jobs.py — Drain the feed_jobs queue one pipeline at a time.

Pure Python, NO LLM. Reclaims stale running jobs, claims the oldest queued job
if none is running, and wakes the orchestrator with a one-shot FEED_JOB cron.

Always exits 0.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402

STALE_JOB_HOURS = float(os.environ.get("FEED_JOB_STALE_HOURS", "3"))
# Max pipeline runs in flight at once. Default 1 = fully serial (unchanged).
# Raise (e.g. 2) ONLY after the per-run/per-slug file isolation is verified with
# a live test — see AGENT_PIPELINE_REGISTRY.md. Even above 1, the queue still
# runs at most ONE job per project (different publications run in parallel).
try:
    MAX_CONCURRENT = max(1, int(os.environ.get("FEED_JOB_MAX_CONCURRENT", "1")))
except ValueError:
    MAX_CONCURRENT = 1


def _edit_card_state(job: db.FeedJob, state: str) -> None:
    """Best-effort update of the headline card button to queued/running."""
    try:
        import send_feed_card as sfc
        from build_and_send_card import (
            TELEGRAM_CONFIG,
            edit_message_reply_markup,
            load_bot_token,
            load_json,
            resolve_chat_id,
        )

        card = db.get_feed_card(job.feed_id)
        if not card:
            return
        candidates = json.loads(card.candidates_json)
        by_idx = {int(c["index"]): c for c in candidates if "index" in c}
        cand = by_idx.get(int(job.candidate_index)) or {}
        msg_id = cand.get("message_id")
        if not msg_id:
            return
        token = load_bot_token()
        # The card stores the group it was posted to; fall back to project
        # config, then the global default, so the button edit always lands
        # in the right publication's group.
        tg_cfg = load_json(TELEGRAM_CONFIG)
        chat_id = str(card.telegram_group or "").strip() or resolve_chat_id(
            card.project, fallback=str(tg_cfg.get("group_id") or "").strip()
        )
        if not token or not chat_id:
            return
        kb = sfc.build_run_card_keyboard(job.feed_id, int(job.candidate_index), state)
        edit_message_reply_markup(token, chat_id, int(msg_id), json.dumps(kb))
    except Exception as e:
        print(f"DISPATCH_CARD_WARN: job={job.id} {e}", file=sys.stderr)


def _fire_job(job: "db.FeedJob", *, dry_run: bool) -> str:
    payload = {
        "job_id": job.id,
        "project": job.project,
        "feed_id": job.feed_id,
        "candidate_index": job.candidate_index,
        "selection_file": job.selection_file,
    }
    msg = "FEED_JOB " + json.dumps(payload, ensure_ascii=False)

    if dry_run:
        print(f"DISPATCH_DRY_RUN: {msg}", file=sys.stderr)
        db.mark_feed_job(job.id, "queued")
        return f"DISPATCH_DRY_RUN: job_id={job.id}"

    _edit_card_state(job, "running")

    # Include job.id so concurrent fires in the same second get distinct cron names.
    job_name = (
        f"feed-job-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{job.id}"
    )
    cmd = [
        "openclaw",
        "cron",
        "add",
        "--name",
        job_name,
        "--at",
        "1m",
        "--agent",
        "orchestrator",
        "--session",
        "isolated",
        "--message",
        msg,
        "--timeout-seconds",
        "7200",
        "--delete-after-run",
        "--no-deliver",
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode != 0:
            db.mark_feed_job(job.id, "failed")
            detail = (res.stderr or res.stdout or "").strip()
            print(f"DISPATCH_FIRE_FAILED: job_id={job.id} {detail}", file=sys.stderr)
            return f"DISPATCH_FIRE_FAILED: job_id={job.id}"
    except Exception as e:
        db.mark_feed_job(job.id, "failed")
        print(f"DISPATCH_FIRE_FAILED: job_id={job.id} detail={e}", file=sys.stderr)
        return f"DISPATCH_FIRE_FAILED: job_id={job.id}"

    print(f"DISPATCH_FIRED: job_id={job.id} cron={job_name}", file=sys.stderr)
    return f"DISPATCH_FIRED: job_id={job.id}"


def dispatch_once(*, dry_run: bool = False) -> str:
    """Fill the run slots: claim and fire eligible jobs up to MAX_CONCURRENT.

    With the default MAX_CONCURRENT=1 this fires at most one job and is
    behavior-identical to the previous serial dispatcher. Above 1, it fires one
    job per idle slot, never more than one per project.
    """
    db.init_db()
    reclaimed = db.reclaim_stale_jobs(STALE_JOB_HOURS)
    if reclaimed:
        print(f"DISPATCH_RECLAIMED: count={reclaimed}", file=sys.stderr)

    fired: list[str] = []
    # Bound the loop by available slots so we never spin.
    for _ in range(MAX_CONCURRENT):
        if db.count_running_jobs() >= MAX_CONCURRENT:
            break
        job = db.claim_next_feed_job(max_concurrent=MAX_CONCURRENT)
        if not job:
            break
        result = _fire_job(job, dry_run=dry_run)
        fired.append(result)
        if dry_run:
            # In dry-run the job is returned to 'queued', so stop to avoid a loop.
            break

    if not fired:
        if db.count_running_jobs() >= MAX_CONCURRENT:
            return "DISPATCH_BUSY"
        return "DISPATCH_IDLE"
    return " | ".join(fired)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Dispatch the next queued feed job")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    print(dispatch_once(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
