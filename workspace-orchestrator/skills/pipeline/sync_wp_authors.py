#!/usr/bin/env python3
"""
sync_wp_authors.py — Fetch a project's live WordPress users via the REST API
and print/suggest an `authors[]` block for projects/<slug>.json.

Unlike sync_wp_categories.py, this does NOT auto-write authors into the
project config by default (author selection is editorial/curated — the
onboarding wizard lets a human pick which users become bylines). Use
--write to overwrite `authors[]` with ALL fetched users (rarely what you
want; prefer --dry-run + manual curation, or drive selection via
onboard_project.py).

Usage:
    python3 sync_wp_authors.py --slug coinography
    python3 sync_wp_authors.py --slug coinography --write

Reads:  projects/<slug>.json (wordpress.url / user / app_password_ref)
        GET {url}/wp-json/wp/v2/users (paginated, context=edit)
Prints: id | name | slug | roles  (dry-run, default)
Writes: projects/<slug>.json (wordpress-adjacent `authors` = [{id, label, name}]) when --write

Exit 0 + prints WP_AUTHORS_FETCHED / WP_AUTHORS_WRITTEN: <slug> / <n> authors
Exit 1 + prints WP_AUTHORS_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import project_config as pc  # noqa: E402
from wp_rest_client import fetch_users  # noqa: E402


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", default=None, help="Project slug (defaults to env / manifest / coinnetwork)")
    parser.add_argument("--roles", default="author,editor,administrator", help="Comma-separated WP roles to include")
    parser.add_argument("--write", action="store_true", help="Overwrite authors[] with ALL fetched users (default: dry-run print only)")
    args = parser.parse_args()

    try:
        cfg = pc.load_project_config(slug=args.slug)
    except (FileNotFoundError, ValueError) as e:
        print(f"WP_AUTHORS_ERROR: {e}")
        return 1

    wp = cfg.get("wordpress") or {}
    url = wp.get("url")
    user = wp.get("user")
    if not url or not user:
        print(f"WP_AUTHORS_ERROR: project '{cfg.slug}' missing wordpress.url or wordpress.user")
        return 1
    try:
        password = cfg.wp_password()
    except (KeyError, FileNotFoundError) as e:
        print(f"WP_AUTHORS_ERROR: {e}")
        return 1

    try:
        users = fetch_users(url, user, password, roles=args.roles)
    except RuntimeError as e:
        print(f"WP_AUTHORS_ERROR: {e}")
        return 1

    if not users:
        print(f"WP_AUTHORS_ERROR: no users returned from {url}")
        return 1

    if not args.write:
        print(f"WP_AUTHORS_FETCHED (dry-run): {cfg.slug} / {len(users)} users")
        for u in users:
            roles = ",".join(u.get("roles") or []) or "?"
            print(f"  {u['id']:5} | {u['slug']:20} | {u['name']:30} | roles={roles}")
        return 0

    authors = [{"id": u["id"], "label": u["name"], "name": u["name"]} for u in users]
    with open(cfg.source_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["authors"] = authors

    try:
        atomic_write_json(cfg.source_path, data)
    except OSError as e:
        print(f"WP_AUTHORS_ERROR: cannot write {cfg.source_path}: {e}")
        return 1

    print(f"WP_AUTHORS_WRITTEN: {cfg.slug} / {len(authors)} authors -> {cfg.source_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
