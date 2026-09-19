"""Writer bake-off CLI. Default is dry-run with zero live calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from newsagent_v2.bench.writer_bakeoff.contract import DEFAULT_FIXTURE_REL, SOURCE_BATCH_REL
from newsagent_v2.bench.writer_bakeoff.credentials import load_environ
from newsagent_v2.bench.writer_bakeoff.freeze import freeze_top1_batch
from newsagent_v2.bench.writer_bakeoff.runner import dry_run, dry_run_one, refuse_groq_retest, run_one_writer

REPO_ROOT = Path(__file__).resolve().parents[4]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Same-evidence writer bake-off. Default: dry-run, no live calls.")
    parser.add_argument("--fixture", default=str(REPO_ROOT / DEFAULT_FIXTURE_REL))
    parser.add_argument("--dry-run", action="store_true", default=False)
    parser.add_argument("--dry-run-one", action="store_true", default=False)
    parser.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--live-one", action="store_true", default=False)
    parser.add_argument("--freeze-from", default=None, help="Path to a live approval batch directory or batch.json")
    args = parser.parse_args(argv)

    if args.freeze_from:
        source = Path(args.freeze_from)
        if not source.is_absolute():
            source = REPO_ROOT / source
        if source.is_dir():
            source = source / "batch.json"
        dest = Path(args.fixture)
        if not dest.is_absolute():
            dest = REPO_ROOT / dest
        freeze_top1_batch(batch_path=source, dest_dir=dest, repo_root=REPO_ROOT)
        print(json.dumps({"ok": True, "froze": str(dest), "live_http_calls": 0}, indent=2))
        if not args.dry_run and not args.dry_run_one and not args.live and not args.live_one:
            return 0

    fixture = Path(args.fixture)
    if not fixture.is_absolute():
        fixture = REPO_ROOT / fixture
    environ = load_environ(REPO_ROOT)

    if args.live and not args.live_one:
        result = refuse_groq_retest()
        print(json.dumps(result, indent=2, default=str))
        return 1

    if args.live_one and not args.dry_run and not args.dry_run_one:
        result = run_one_writer(fixture, environ=environ)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1

    if args.dry_run_one:
        result = dry_run_one(fixture, environ=environ)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok") else 1
    result = dry_run(fixture, environ=environ)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
