#!/usr/bin/env python3
"""
sync_wp_categories.py — Fetch a project's live WordPress categories via the REST
API and write them into projects/<slug>.json under `wordpress.categories`.

The Picker uses this list to classify each article into real WordPress
categories (by slug); the Publisher resolves slugs → numeric IDs for the post
payload. Re-run any time the site's categories change. Idempotent.

Usage:
    python3 sync_wp_categories.py --slug coinography
    python3 sync_wp_categories.py --slug coinography --dry-run

Reads:  projects/<slug>.json (wordpress.url / user / app_password_ref)
        GET {url}/wp-json/wp/v2/categories?per_page=100 (paginated)
Writes: projects/<slug>.json (wordpress.categories = [{id, name, slug, count}])

Exit 0 + prints WP_CATEGORIES_SYNCED: <slug> / <n> categories
Exit 1 + prints WP_CATEGORIES_ERROR: <reason>
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
from wp_rest_client import fetch_categories, WpAuthError, WpRequestError  # noqa: E402


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slug",
        default=None,
        help="Project slug (defaults to env / manifest / coinnetwork)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the fetched categories but do not modify the project config",
    )
    args = parser.parse_args()

    try:
        cfg = pc.load_project_config(slug=args.slug)
    except (FileNotFoundError, ValueError) as e:
        print(f"WP_CATEGORIES_ERROR: {e}")
        return 1

    wp = cfg.get("wordpress") or {}
    url = wp.get("url")
    user = wp.get("user")
    if not url or not user:
        print(f"WP_CATEGORIES_ERROR: project '{cfg.slug}' missing wordpress.url or wordpress.user")
        return 1
    try:
        password = cfg.wp_password()
    except (KeyError, FileNotFoundError) as e:
        print(f"WP_CATEGORIES_ERROR: {e}")
        return 1

    try:
        categories = fetch_categories(url, user, password)
    except RuntimeError as e:
        print(f"WP_CATEGORIES_ERROR: {e}")
        return 1

    if not categories:
        print(f"WP_CATEGORIES_ERROR: no categories returned from {url}")
        return 1

    categories.sort(key=lambda c: c["id"])

    if args.dry_run:
        print(f"WP_CATEGORIES_FETCHED (dry-run): {cfg.slug} / {len(categories)} categories")
        for c in categories:
            print(f"  {c['id']:5} | {c['slug']:40} | {c['name']}  (count={c['count']})")
        return 0

    # Merge into the project config, preserving everything else.
    with open(cfg.source_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("wordpress", {})
    data["wordpress"]["categories"] = categories

    try:
        atomic_write_json(cfg.source_path, data)
    except OSError as e:
        print(f"WP_CATEGORIES_ERROR: cannot write {cfg.source_path}: {e}")
        return 1

    print(f"WP_CATEGORIES_SYNCED: {cfg.slug} / {len(categories)} categories -> {cfg.source_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
