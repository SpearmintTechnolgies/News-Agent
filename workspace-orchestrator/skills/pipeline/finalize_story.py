#!/usr/bin/env python3
"""
finalize_story.py — Consolidates post-WordPress publish actions with fail-safe Drive upload & internal verification.

Actions executed in order:
1. (Optional) Convert article to docx & upload to Google Drive (--drive-upload) [Fail-Safe]
2. Update recent topics registry (~/.openclaw/workspace-orchestrator/state/recent_topics.json)
3. Aggregate LLM token usage (updates publish/tokens.json and manifest.json)
4. Build and send Telegram news card (photo + caption + inline keyboard)
5. Update pick status in editorial.db (status=published, token usage, cost)
6. Mark feed job as done in editorial.db (resolves feed_job_id from manifest or active running job)
7. Perform internal verification check & return verified_clean: true

Usage:
    python3 finalize_story.py --manifest /path/to/manifest.json [--pick-id 12] [--project news_site] [--drive-upload]

Exit 0 + prints STORY_FINALIZED: <json_result>
Exit 1 + prints STORY_FINALIZE_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import editorial_db  # noqa: E402
from aggregate_run_tokens import aggregate_tokens, write_tokens_artifacts  # noqa: E402
from build_and_send_card import (  # noqa: E402
    TELEGRAM_CONFIG,
    atomic_write_json,
    build_caption,
    build_card_from_manifest,
    build_inline_keyboard,
    insert_article,
    load_bot_token,
    load_json,
    resolve_chat_id,
    save_article_version,
    send_telegram_card,
)
import project_config as pc  # noqa: E402
import update_recent_topics  # noqa: E402


def _resolve_gog_keyring_passphrase() -> str:
    """Dynamically retrieve GOG keyring passphrase from environment or ~/.bashrc."""
    passphrase = os.environ.get("GOG_KEYRING_PASSWORD") or os.environ.get("GOG_KEYRING_PASSWPHRASE")
    if passphrase:
        return passphrase
    bashrc_path = os.path.expanduser("~/.bashrc")
    if os.path.isfile(bashrc_path):
        try:
            with open(bashrc_path, encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if "GOG_KEYRING_PASSPHRASE" in line or "GOG_KEYRING_PASSWORD" in line:
                        parts = line.split("=")
                        if len(parts) >= 2:
                            val = parts[1].strip().strip('"').ctrip("'")
                            if val:
                                return val
        except Exception:
            pass
    return "sawan"

def handle_drive_upload(manifest_path: str, manifest: dict, project_slug: str | None) -> str | None:
    """Fail-safe Google Drive upload. Converts to docx and uploads via gog."""
    run_dir = str(manifest.get("run_dir") or "").strip()
    if not run_dir or not os.path.isdir(run_dir):
        return None

    final_md = os.path.join(run_dir, "article", "final.md")
    docx_path = os.path.join(run_dir, "article", "article.docx")
    if not os.path.isfile(final_md):
        print(f"DRIVE_UPLOAD_WARN: final.md not found at {final_md}", file=sys.stderr)
        return None

    os.makedirs(os.path.dirname(docx_path), exist_ok=True)
    try:
        subprocess.run(
            ["pandoc", "-o", docx_path, final_md],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as pandoc_err:
        print(f"DRIVE_UPLOAD_WARN: pandoc conversion failed: {pandoc_err}", file=sys.stderr)
        return None

    # Resolve Drive parent & account from project config or manifest
    drive_parent = ""
    drive_account = ""
    if project_slug:
        try:
            cfg = pc.load_project_config(slug=project_slug)
            drive_parent = str(cfg.get_path("gdrive.parent_folder_id", "") or cfg.get_path("publisher.drive_parent_id", "") or "").strip()
            drive_account = str(cfg.get_path("gdrive.account", "") or cfg.get_path("publisher.drive_account", "") or "").strip()
        except Exception:
            pass

    if not drive_parent and isinstance(manifest.get("gdrive"), dict):
        drive_parent = str(manifest["gdrive"].get("parent_folder_id") or "").strip()
    if not drive_account and isinstance(manifest.get("gdrive"), dict):
        drive_account = str(manifest["gdrive"].get("account") or "").strip()

    if not drive_parent:
        drive_parent = os.environ.get("DRIVE_PARENT", "").strip() or "1DiEijL14zMSnuIqycvdoxAIgOdRvxCDx"

    if not drive_account:
        drive_account = os.environ.get("DRIVE_ACCT", "").strip() or os.environ.get("GOG_ACCOUNT", "").strip()

    cmd = ["gog", "drive", "upload", docx_path, "--parent", drive_parent, "--json"]
    if drive_account:
        cmd.extend(["--account", drive_account])

    keyring_pass = _resolve_gog_keyring_passphrase()
    env = os.environ.copy()
    env["GOG_KEYRING_PASSWORD"] = keyring_pass
    env["GOG_KEYRING_PASSPHRASE"] = keyring_pass
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=120, env=env)
        drive_json_path = os.path.join(run_dir, "publish", "google_drive.json")
        os.makedirs(os.path.dirname(drive_json_path), exist_ok=True)
        drive_data = {}
        try:
            drive_data = json.loads(res.stdout)
            with open(drive_json_path, "w", encoding="utf-8") as f:
                json.dump(drive_data, f, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            pass

        if isinstance(drive_data.get("file"), dict):
            file_obj = drive_data["file"]
        else:
            file_obj = drive_data

        link = (
            file_obj.get("webViewLink")
            or file_obj.get("web_view_link")
            or file_obj.get("link")
        )
        return str(link) if link else "uploaded"
    except Exception as gog_err:
        print(f"DRIVE_UPLOAD_WARN: gog upload failed: {gog_err}", file=sys.stderr)
        return None


def verify_pipeline_state(
    run_id: str,
    pick_id: int | None,
    feed_job_id: int | None,
    project_slug: str | None,
    run_dir: str,
    card_sent: bool,
    db_path: str,
) -> dict[str, bool]:
    """Internal self-verification check across DB and file artifacts."""
    checks = {
        "topics_updated": False,
        "tokens_aggregated": False,
        "card_delivered": card_sent,
        "pick_published": False,
        "queue_cleared": False,
    }

    # 1. Check Recent Topics Registry
    registry_json = os.path.expanduser("~/.openclaw/workspace-orchestrator/state/recent_topics.json")
    if os.path.isfile(registry_json):
        try:
            reg_data = load_json(registry_json)
            if isinstance(reg_data, list):
                checks["topics_updated"] = any(
                    isinstance(e, dict) and e.get("run_id") == run_id for e in reg_data
                )
        except Exception:
            pass

    # 2. Check Tokens JSON
    tokens_json = os.path.join(run_dir, "publish", "tokens.json")
    checks["tokens_aggregated"] = os.path.isfile(tokens_json)

    # 3. Check Pick Status in DB
    if pick_id is not None:
        try:
            pick = editorial_db.get_pick(int(pick_id), db_path=db_path)
            if pick and pick.get("status") == "published":
                checks["pick_published"] = True
        except Exception:
            pass
    else:
        checks["pick_published"] = True

    # 4. Check Queue Status in DB
    try:
        if feed_job_id is not None:
            job = editorial_db.get_feed_job(int(feed_job_id), db_path=db_path)
            if job and job.status in ("done", "failed"):
                checks["queue_cleared"] = True
        else:
            active_job = editorial_db.active_feed_job(db_path=db_path)
            if active_job is None or (project_slug and active_job.project != project_slug):
                checks["queue_cleared"] = True
    except Exception:
        pass

    return checks


def finalize_story(
    manifest_path: str,
    pick_id: int | None = None,
    project_slug: str | None = None,
    published_url: str | None = None,
    do_drive_upload: bool = False,
    db_path: str = editorial_db.DEFAULT_DB_PATH,
) -> dict[str, Any]:
    manifest_path = os.path.realpath(manifest_path)
    if not os.path.isfile(manifest_path):
        raise FileNotFoundError(f"manifest file not found: {manifest_path}")

    manifest = load_json(manifest_path)
    run_dir = str(manifest.get("run_dir") or "").strip()
    run_id = str(manifest.get("run_id") or "").strip()

    if not project_slug:
        project_slug = pc.resolve_project_slug(manifest_path=manifest_path)

    if pick_id is None:
        pick_id = manifest.get("pick_id")
        if pick_id is None and isinstance(manifest.get("pick"), dict):
            pick_id = manifest["pick"].get("id")

    # 1. Optional Fail-Safe Drive Upload
    drive_url = None
    if do_drive_upload:
        drive_url = handle_drive_upload(manifest_path, manifest, project_slug)

    # 2. Update Recent Topics Registry
    validated_json = os.path.join(run_dir, "research", "validated.json")
    registry_json = os.path.expanduser("~/.openclaw/workspace-orchestrator/state/recent_topics.json")
    if os.path.isfile(validated_json):
        try:
            current_research = load_json(validated_json)
            if isinstance(current_research, dict):
                entries: list[dict] = []
                if os.path.isfile(registry_json):
                    raw_reg = load_json(registry_json)
                    if isinstance(raw_reg, list):
                        entries = [e for e in raw_reg if isinstance(e, dict)]

                status_topic = "published" if published_url else "drafted"
                topic_entry = update_recent_topics.entry_from_research(
                    current_research, run_id, status_topic, published_url
                )
                updated = False
                for i, existing in enumerate(entries):
                    if existing.get("run_id") == run_id:
                        merged = {**existing, **topic_entry}
                        entries[i] = merged
                        updated = True
                        break
                if not updated:
                    entries.append(topic_entry)
                atomic_write_json(registry_json, entries)
        except Exception as topic_err:
            print(f"RECENT_TOPICS_WARN: {topic_err}", file=sys.stderr)

    # 3. Aggregate Run Tokens
    tokens_res: dict[str, Any] = {}
    try:
        tokens_res = aggregate_tokens(manifest_path)
        write_tokens_artifacts(manifest_path, tokens_res)
    except Exception as tok_err:
        print(f"TOKENS_AGGREGATE_WARN: {tok_err}", file=sys.stderr)

    # 4. Build & Send Telegram News Card
    card_sent = False
    message_id = None
    try:
        card, news_card_path, image_path = build_card_from_manifest(manifest_path)
        if drive_url and not card.get("drive_url"):
            card["drive_url"] = drive_url

        bot_token = load_bot_token()
        if bot_token:
            tg_cfg = load_json(TELEGRAM_CONFIG)
            fallback_chat_id = str(tg_cfg.get("group_id") or "").strip()
            chat_id = resolve_chat_id(project_slug, fallback=fallback_chat_id)
            if chat_id:
                card["telegram_group"] = chat_id
                caption = build_caption(card)
                keyboard = build_inline_keyboard(card)
                reply_markup = json.dumps(keyboard) if keyboard else None
                if keyboard:
                    card["inline_keyboard"] = keyboard.get("inline_keyboard", [])

                message_id = send_telegram_card(
                    bot_token, chat_id, caption, image_path, reply_markup=reply_markup
                )
                card["telegram_message_id"] = message_id
                card_sent = bool(message_id)

                try:
                    editorial_db.init_db(db_path)
                    article_id = insert_article(card, db_path=db_path)
                    final_md = os.path.join(run_dir, "article", "final.md") if run_dir else ""
                    if final_md and os.path.isfile(final_md):
                        with open(final_md, encoding="utf-8") as f:
                            snapshot = f.read()
                        if snapshot.strip():
                            save_article_version(article_id, "published_snapshot", snapshot, db_path=db_path)
                except Exception as db_art_err:
                    print(f"ARTICLE_DB_INSERT_WARN: {db_art_err}", file=sys.stderr)

                atomic_write_json(news_card_path, card)
    except Exception as card_err:
        print(f"CARD_SEND_WARN: {card_err}", file=sys.stderr)

    # 5. Update Pick Status in editorial.db
    if pick_id is not None:
        try:
            editorial_db.init_db(db_path)
            tokens_in = tokens_res.get("tokens_in")
            tokens_out = tokens_res.get("tokens_out")
            tokens_total = tokens_res.get("tokens_total")
            cost_usd = tokens_res.get("cost_usd")
            by_model = tokens_res.get("by_model")
            tokens_by_model_str = json.dumps(by_model) if by_model else None

            editorial_db.update_pick_status(
                int(pick_id),
                "published",
                pipeline_run_id=run_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_total=tokens_total,
                tokens_by_model=tokens_by_model_str,
                cost_usd=cost_usd,
                db_path=db_path,
            )
        except Exception as pick_err:
            print(f"PICK_STATUS_WARN: {pick_err}", file=sys.stderr)

    # 6. Bulletproof Queue Cleanup in editorial.db
    feed_job_marked = "none"
    resolved_job_id = manifest.get("feed_job_id") or manifest.get("feed_id")
    if resolved_job_id is None and isinstance(manifest.get("batch"), dict):
        resolved_job_id = manifest["batch"].get("feed_job_id")

    try:
        editorial_db.init_db(db_path)
        if resolved_job_id:
            editorial_db.mark_feed_job(int(resolved_job_id), "done", db_path=db_path)
            feed_job_marked = f"job_id={resolved_job_id}"
        else:
            # Fallback to active running job for this project
            active_job = editorial_db.active_feed_job(db_path=db_path)
            if active_job and (not project_slug or active_job.project == project_slug):
                editorial_db.mark_feed_job(active_job.id, "done", db_path=db_path)
                resolved_job_id = active_job.id
                feed_job_marked = f"job_id={active_job.id}"
    except Exception as feed_err:
        print(f"FEED_JOB_CLEAR_WARN: {feed_err}", file=sys.stderr)

    # 7. Internal Verification Query
    verification = verify_pipeline_state(
        run_id=run_id,
        pick_id=pick_id,
        feed_job_id=resolved_job_id,
        project_slug=project_slug,
        run_dir=run_dir,
        card_sent=card_sent,
        db_path=db_path,
    )
    verified_clean = all(verification.values())

    return {
        "success": True,
        "verified_clean": verified_clean,
        "run_id": run_id,
        "pick_id": pick_id,
        "card_sent": card_sent,
        "message_id": message_id,
        "drive_url": drive_url,
        "tokens_total": tokens_res.get("tokens_total", 0),
        "cost_usd": tokens_res.get("cost_usd", 0.0),
        "feed_job_status": feed_job_marked,
        "verification": verification,
        "message": "Pipeline complete. No further actions required." if verified_clean else "Finalized with warnings.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Finalize story post-WordPress publication")
    parser.add_argument("--manifest", required=True, help="Path to pipeline manifest.json")
    parser.add_argument("--pick-id", type=int, default=None, help="Pick ID")
    parser.add_argument("--project", default=None, help="Project slug")
    parser.add_argument("--published-url", default=None, help="Published article URL")
    parser.add_argument("--drive-upload", action="store_true", help="Convert to docx and upload to Google Drive")
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH, help="Path to editorial.db")
    args = parser.parse_args()

    try:
        result = finalize_story(
            manifest_path=args.manifest,
            pick_id=args.pick_id,
            project_slug=args.project,
            published_url=args.published_url,
            do_drive_upload=args.drive_upload,
            db_path=args.db_path,
        )
        print(f"STORY_FINALIZED: {json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as exc:
        print(f"STORY_FINALIZE_ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
