#!/usr/bin/env python3
"""emit_feed_fetch_commands.py -- Print one bash `curl` line per RSS feed
configured for the active project.

This lets the Researcher SOUL stay project-agnostic. Instead of hardcoding
a feed list per site, the Researcher runs:

    eval "$(python3 .../emit_feed_fetch_commands.py)"

and the project's feeds get fetched.

Usage:
    python3 emit_feed_fetch_commands.py [--project <slug>] [--output-dir /tmp/feeds]

Each emitted line writes the feed body to /tmp/feeds/<slot>.xml (slot = 1..N).
The `Source-Name: <name>` header field is preserved in a sidecar
/tmp/feeds/<slot>.source so the parser can map back to the friendly source name.
"""
from __future__ import annotations

import argparse
import os
import shlex
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import project_config as pc  # noqa: E402

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; OpenClawScout/1.0)"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=None)
    parser.add_argument("--output-dir", default="/tmp/feeds")
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent header sent for each curl",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=12,
        help="Per-request timeout in seconds",
    )
    args = parser.parse_args()

    try:
        cfg = pc.load_project_config(slug=args.project)
    except (FileNotFoundError, ValueError) as e:
        print(f"# FEED_EMIT_ERROR: {e}", file=sys.stderr)
        return 1

    feeds = cfg.get_path("research.rss_feeds", []) or []
    if not isinstance(feeds, list) or not feeds:
        print(f"# FEED_EMIT_ERROR: no rss_feeds for project {cfg.slug}", file=sys.stderr)
        return 1

    print(f"# project={cfg.slug} feeds={len(feeds)} output_dir={args.output_dir}")
    print(f"mkdir -p {shlex.quote(args.output_dir)}")
    print(f"UA={shlex.quote(args.user_agent)}")

    for i, feed in enumerate(feeds, 1):
        if not isinstance(feed, dict):
            continue
        url = str(feed.get("url") or "").strip()
        if not url:
            continue
        source = str(feed.get("source") or "Unknown").strip() or "Unknown"
        out_xml = f"{args.output_dir}/{i:02d}.xml"
        out_source = f"{args.output_dir}/{i:02d}.source"
        print(f"# {i}: {source}")
        print(
            f"curl -sL -A \"$UA\" "
            f"{shlex.quote(url)} "
            f"--max-time {int(args.timeout)} "
            f"-o {shlex.quote(out_xml)}"
        )
        print(f"printf %s {shlex.quote(source)} > {shlex.quote(out_source)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
