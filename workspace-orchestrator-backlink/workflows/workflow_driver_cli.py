#!/usr/bin/env python3
"""CLI for workflow driver."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "database"))
sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_driver  # noqa: E402
import workflow_manager  # noqa: E402


def cmd_step(args: argparse.Namespace) -> int:
    result = workflow_driver.run_step(args.id, db_path=args.db)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.status in {"ok", "wait"} else 1


def cmd_run(args: argparse.Namespace) -> int:
    results = workflow_driver.run_until_wait(args.id, max_steps=args.max_steps, db_path=args.db)
    print(json.dumps([r.to_dict() for r in results], indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(json.dumps(workflow_driver.driver_summary(args.id, db_path=args.db), indent=2))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows = backlink_db.list_workflows(db_path=args.db)
    print(json.dumps({"db": args.db, "workflows": rows}, indent=2))
    return 0


def cmd_callback(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_ROOT / "tools" / "telegram"))
    import handle_backlink_callback  # noqa: E402

    result = handle_backlink_callback.dispatch(
        callback_data=args.callback_data,
        text=args.text,
        user_id=args.user_id,
        chat_id=args.chat_id,
        reply_to_message_id=args.reply_to,
        db_path=args.db,
    )
    print(json.dumps(result, indent=2))
    return 0


def _db_path(args: argparse.Namespace) -> str:
    return getattr(args, "db", backlink_db.DEFAULT_DB_PATH)


def _print_workflow_not_found(workflow_id: str, db_path: str) -> None:
    print(f"ERROR: Workflow not found: {workflow_id}", file=sys.stderr)
    try:
        rows = backlink_db.list_workflows(db_path=db_path)
    except OSError:
        rows = []
    if rows:
        print(f"\nWorkflows in {db_path}:", file=sys.stderr)
        for row in rows:
            print(
                f"  {row['workflow_id']}  {row['state']}  {row.get('url') or ''}",
                file=sys.stderr,
            )
        print(
            f"\nUse one of the ids above, or bootstrap again:\n"
            f"  python3 workflows/workflow_driver_cli.py bootstrap --db {db_path!r}",
            file=sys.stderr,
        )
    else:
        print(
            f"\nNo workflows in {db_path} (db may have been recreated).\n"
            f"  python3 workflows/workflow_driver_cli.py bootstrap --db {db_path!r}",
            file=sys.stderr,
        )


def cmd_expire(args: argparse.Namespace) -> int:
    backlink_db.init_db(args.db)
    expired = backlink_db.expire_stale_approvals(db_path=args.db)
    print(json.dumps({"expired": expired, "count": len(expired)}, indent=2))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))
    from update_learning_weights import generate_backlink_report  # noqa: E402

    report = generate_backlink_report(
        db_path=args.db,
        domain=args.domain,
        learning_limit=args.limit,
    )
    print(json.dumps(report, indent=2))
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    sys.path.insert(0, str(_ROOT / "skills" / "pipeline"))
    import discover_opportunities  # noqa: E402

    backlink_db.init_db(args.db)
    cid = backlink_db.get_or_create_campaign(args.campaign, args.domain, db_path=args.db)
    result = discover_opportunities.discover_from_search(
        cid,
        queries=args.queries.split("|") if args.queries else None,
        limit_per_query=args.limit,
        db_path=args.db,
        fetch_pages=not args.no_fetch,
        use_search_cache=not args.no_cache,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Create campaign + opportunity + workflow for manual testing."""
    backlink_db.init_db(args.db)
    cid = backlink_db.get_or_create_campaign(args.campaign, args.domain, db_path=args.db)
    wid = args.id or workflow_manager.new_workflow_id()
    hash_value = backlink_db.url_hash(args.url)
    existing = backlink_db.find_opportunity_by_url_hash(cid, hash_value, db_path=args.db)

    if existing and not args.reuse_opportunity:
        print(
            "ERROR: Opportunity already exists for this URL in this campaign.\n"
            f"  url_hash={hash_value}\n"
            f"  opportunity_id={existing.id}\n"
            "Options:\n"
            f"  rm {args.db!r}   # fresh db\n"
            "  bootstrap --reuse-opportunity   # new workflow on same opportunity\n"
            "  bootstrap --url 'https://other.example/page'   # different URL",
            file=sys.stderr,
        )
        return 1

    if existing:
        opp = existing
        wf = backlink_db.create_workflow(
            wid,
            campaign_id=cid,
            opportunity_id=opp.id,
            db_path=args.db,
        )
        reused = True
    else:
        opp, wf = backlink_db.create_opportunity_and_workflow(
            cid,
            args.url,
            wid,
            title=args.title,
            db_path=args.db,
        )
        reused = False

    payload = {
        "campaign_id": cid,
        "opportunity_id": opp.id,
        "workflow_id": wf.workflow_id,
        "url": opp.url,
        "state": wf.state,
        "reused_opportunity": reused,
        "next_action": workflow_manager.next_action_for(wf.workflow_id, db_path=args.db),
    }
    print(json.dumps(payload, indent=2))
    print(
        f"\n# Next:\n"
        f"python3 workflows/workflow_driver_cli.py run --id {wf.workflow_id} --db {args.db!r}\n"
        f"python3 workflows/workflow_driver_cli.py status --id {wf.workflow_id} --db {args.db!r}",
    )
    return 0


def _add_db_arg(parser: argparse.ArgumentParser, *, inherit: bool = False) -> None:
    parser.add_argument(
        "--db",
        default=argparse.SUPPRESS if inherit else backlink_db.DEFAULT_DB_PATH,
        help="SQLite db path",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Backlink workflow driver")
    _add_db_arg(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    p_step = sub.add_parser("step", help="Run one automated step")
    _add_db_arg(p_step, inherit=True)
    p_step.add_argument("--id", required=True, help="Workflow id")
    p_step.set_defaults(func=cmd_step)

    p_run = sub.add_parser("run", help="Run steps until wait state")
    _add_db_arg(p_run, inherit=True)
    p_run.add_argument("--id", required=True)
    p_run.add_argument("--max-steps", type=int, default=20)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Show workflow driver summary")
    _add_db_arg(p_status, inherit=True)
    p_status.add_argument("--id", required=True)
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("list", help="List workflows in the database")
    _add_db_arg(p_list, inherit=True)
    p_list.set_defaults(func=cmd_list)

    p_cb = sub.add_parser("callback", help="Simulate Telegram callback or edit text")
    _add_db_arg(p_cb, inherit=True)
    p_cb.add_argument("--callback-data", help="e.g. bl_approve:WF-...")
    p_cb.add_argument("--text", help="Edit instructions or APPROVE/REJECT/EDIT/BLACKLIST")
    p_cb.add_argument("--user-id", default="cli-tester")
    p_cb.add_argument("--chat-id")
    p_cb.add_argument("--reply-to", type=int, help="Reply-to message id for text resolution")
    p_cb.set_defaults(func=cmd_callback)

    p_boot = sub.add_parser("bootstrap", help="Create test campaign/opportunity/workflow")
    _add_db_arg(p_boot, inherit=True)
    p_boot.add_argument("--url", default="https://example.com/write-for-us")
    p_boot.add_argument("--campaign", default="cryptography.com")
    p_boot.add_argument("--domain", default="cryptography.com")
    p_boot.add_argument("--title", default="Write For Us")
    p_boot.add_argument("--id", default=None, help="Workflow id (auto-generated if omitted)")
    p_boot.add_argument(
        "--reuse-opportunity",
        action="store_true",
        help="If URL already exists, create a new workflow on that opportunity",
    )
    p_boot.set_defaults(func=cmd_bootstrap)

    p_disc = sub.add_parser("discover", help="Search for new backlink opportunities")
    _add_db_arg(p_disc, inherit=True)
    p_disc.add_argument("--campaign", default="cryptography.com")
    p_disc.add_argument("--domain", default="cryptography.com")
    p_disc.add_argument("--queries", help="Pipe-separated search queries (overrides campaign.json)")
    p_disc.add_argument("--limit", type=int, default=5, help="Max results per query")
    p_disc.add_argument("--no-fetch", action="store_true", help="Skip page fetch/parse")
    p_disc.add_argument("--no-cache", action="store_true", help="Bypass search cache")
    p_disc.set_defaults(func=cmd_discover)

    p_report = sub.add_parser("report", help="On-demand backlink pipeline SQL report")
    _add_db_arg(p_report, inherit=True)
    p_report.add_argument("--domain", help="Filter learning rows by domain")
    p_report.add_argument("--limit", type=int, default=20)
    p_report.set_defaults(func=cmd_report)

    p_expire = sub.add_parser("expire", help="Expire stale PENDING_APPROVAL workflows")
    _add_db_arg(p_expire, inherit=True)
    p_expire.set_defaults(func=cmd_expire)

    args = parser.parse_args()
    try:
        return args.func(args)
    except KeyError as exc:
        msg = exc.args[0] if exc.args else str(exc)
        if isinstance(msg, str) and msg.startswith("Workflow not found:"):
            missing_id = msg.split(":", 1)[1].strip()
            _print_workflow_not_found(missing_id, _db_path(args))
            return 1
        print(f"ERROR: {msg}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
