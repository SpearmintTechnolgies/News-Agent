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
import base64
import html
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import project_config as pc  # noqa: E402


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, path)


def fetch_categories(base_url: str, user: str, password: str) -> list[dict]:
    """Fetch all categories (paginated, 100/page) from the WP REST API."""
    api = f"{base_url.rstrip('/')}/wp-json/wp/v2/categories"
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    out: list[dict] = []
    page = 1
    while True:
        url = f"{api}?per_page=100&page={page}&hide_empty=false&_fields=id,name,slug,count"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # WP returns 400 rest_post_invalid_page_number when paging past the end.
            if e.code == 400 and page > 1:
                break
            body = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {e.code} fetching categories (page {page}): {body}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise RuntimeError(f"request failed (page {page}): {e}") from e

        try:
            batch = json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"invalid JSON on page {page}: {raw[:200]}") from e

        if not isinstance(batch, list) or not batch:
            break
        for c in batch:
            out.append(
                {
                    "id": int(c.get("id")),
                    "name": html.unescape(str(c.get("name") or "")).strip(),
                    "slug": str(c.get("slug") or "").strip(),
                    "count": int(c.get("count") or 0),
                }
            )
        if len(batch) < 100:
            break
        page += 1
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--slug",
        default=None,
        help="Project slug (defaults to env / manifest / coinography)",
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
