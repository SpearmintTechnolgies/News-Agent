#!/usr/bin/env python3
"""feed_job_card.py — Best-effort Telegram button state for feed job cards."""
from __future__ import annotations

import json
import sys

import editorial_db as db


def edit_feed_job_card_state(job: db.FeedJob, state: str) -> None:
    """Update the headline card button to ready/queued/running."""
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
        tg_cfg = load_json(TELEGRAM_CONFIG)
        chat_id = str(card.telegram_group or "").strip() or resolve_chat_id(
            card.project, fallback=str(tg_cfg.get("group_id") or "").strip()
        )
        if not token or not chat_id:
            return
        kb = sfc.build_run_card_keyboard(job.feed_id, int(job.candidate_index), state)
        edit_message_reply_markup(token, chat_id, int(msg_id), json.dumps(kb))
    except Exception as e:
        print(f"FEED_CARD_STATE_WARN: job={job.id} {e}", file=sys.stderr)


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Update feed card button state for a job")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--state", choices=("ready", "queued", "running"), required=True)
    args = p.parse_args()

    db.init_db()
    job = db.get_feed_job(args.job_id)
    if not job:
        print(f"FEED_JOB_NOT_FOUND: id={args.job_id}", file=sys.stderr)
        return 1
    edit_feed_job_card_state(job, args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
