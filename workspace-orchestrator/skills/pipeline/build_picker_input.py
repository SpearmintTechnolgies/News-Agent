#!/usr/bin/env python3
"""
build_picker_input.py — Build picker_input.json from validated headlines.json,
filtering out URLs that the picker has already consumed in past runs and
attaching the recent-categories context the Picker needs for diversity.

Usage:
    python3 build_picker_input.py \
        --headlines /path/to/headlines.json \
        --output /path/to/picker_input.json \
        --target-count 3 \
        [--recent-window-hours 24] \
        [--db-path /path/to/editorial.db]

Reads:  headlines.json (cleaned by validate_headlines.py)
        editorial.db   (recent_published_categories + already-consumed pick URLs)
Writes: picker_input.json

Exit 0 + prints PICKER_INPUT_BUILT: <count> candidates / target=<N> / recent=<...>
Exit 1 + prints PICKER_INPUT_ERROR: <reason>
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db  # noqa: E402  (local import after sys.path insert)
import project_config as pc  # noqa: E402


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".picker_input_", suffix=".tmp", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _consumed_pick_urls(db_path: str, project: str) -> set[str]:
    """All primary URLs of picks that ever reached drafted/published
    for the active project.

    Used to filter out candidates that have been consumed in past pipeline
    runs of the same project (permanent dedup at the picker level —
    independent of the 7-day article_history.db). Different projects keep
    independent consumed-URL sets so the same story can run on each site.
    """
    editorial_db.init_db(db_path)
    urls: set[str] = set()
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT DISTINCT primary_url FROM picked_stories
            WHERE primary_url IS NOT NULL AND primary_url != ''
              AND status IN ('drafted', 'published')
              AND project = ?
            """,
            (project,),
        ).fetchall()
        for r in rows:
            urls.add(r["primary_url"])
    return urls


def _picker_candidate(
    headline: str,
    url: str,
    *,
    source: str = "",
    pub_date: str = "",
    summary: str = "",
    corro: object = None,
) -> dict:
    return {
        "headline": headline or url,
        "url": url,
        "pub_date": pub_date or "",
        "source": source or "Unknown",
        "summary": summary or "",
        "corroborating_sources": corro if isinstance(corro, list) else [],
    }


def _selection_file_paths(path: str) -> list[str]:
    """Windows Git-Bash /tmp paths are the same file as C:\\tmp or %TEMP%."""
    if not path:
        return []
    out = [path, os.path.realpath(path)]
    if "/tmp" in path.replace("\\", "/") or path.lower().startswith("\\tmp"):
        rest = path.replace("\\", "/").split("/tmp", 1)[-1].lstrip("/")
        if rest:
            out.append(os.path.join(r"C:\tmp", rest.replace("/", os.sep)))
            tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
            if tmp:
                out.append(os.path.join(tmp, rest.replace("/", os.sep)))
    seen: set[str] = set()
    uniq: list[str] = []
    for p in out:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _load_selection_payload(path: str) -> dict:
    last_err: Exception | None = None
    for candidate in _selection_file_paths(path):
        try:
            with open(candidate, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError) as e:
            last_err = e
            continue
    raise OSError(str(last_err or path))


def _candidates_from_selection(sel: dict, job: editorial_db.FeedJob | None) -> list[dict]:
    urls = [str(u) for u in (sel.get("urls") or []) if u]
    heads = [str(h) for h in (sel.get("headlines") or [])]
    fallback = (job.headline if job else "") or ""
    out: list[dict] = []
    for i, url in enumerate(urls):
        head = heads[i] if i < len(heads) else fallback or url
        out.append(_picker_candidate(head, url))
    return out


def _candidates_from_feed_card(
    job: editorial_db.FeedJob, pool_urls: list[str], db_path: str
) -> list[dict]:
    card = editorial_db.get_feed_card(job.feed_id, db_path=db_path)
    if not card:
        return []
    try:
        cands = json.loads(card.candidates_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(cands, list):
        return []
    wanted = {u for u in pool_urls if u}
    out: list[dict] = []
    for c in cands:
        if not isinstance(c, dict):
            continue
        url = str(c.get("url") or "")
        match = bool(url and url in wanted)
        try:
            if int(c.get("index", -1)) == int(job.candidate_index):
                match = True
        except (TypeError, ValueError):
            pass
        if match and url:
            out.append(
                _picker_candidate(
                    str(c.get("headline") or job.headline or url),
                    url,
                    source=str(c.get("source") or ""),
                    pub_date=str(c.get("pub_date") or ""),
                    summary=str(c.get("summary") or ""),
                    corro=c.get("corroborating_sources"),
                )
            )
    return out


def _load_pool_candidates(project: str, urls: list[str], db_path: str) -> list[dict]:
    """Build candidate dicts from the headline_pool for the given URLs, in order."""
    editorial_db.init_db(db_path)
    rows = editorial_db.pool_by_urls(project, urls, db_path=db_path)
    out: list[dict] = []
    for c in rows:
        corro = []
        if c.corroborating_json:
            try:
                corro = json.loads(c.corroborating_json)
            except (ValueError, TypeError):
                corro = []
        out.append(
            _picker_candidate(
                c.headline,
                c.url,
                source=c.source or "Unknown",
                pub_date=c.pub_date or "",
                summary=c.summary or "",
                corro=corro,
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headlines", help="Path to validated headlines.json (HEADLINE_SCAN mode)")
    parser.add_argument("--output", required=True, help="Path to write picker_input.json")
    parser.add_argument("--target-count", type=int, default=None, help="N stories to pick (default: candidate count in pool mode)")
    parser.add_argument(
        "--recent-window-hours",
        type=int,
        default=None,
        help="Recent-category diversity window. Default: project picker.diversity_window_hours, else 72.",
    )
    parser.add_argument("--db-path", default=editorial_db.DEFAULT_DB_PATH)
    parser.add_argument(
        "--project",
        default=None,
        help="Project slug; default: resolved from PROJECT_SLUG / manifest / coinnetwork",
    )
    # Approve-title-first: build candidates from the headline_pool (selected
    # stories) instead of a headlines.json file.
    parser.add_argument("--from-pool", action="store_true", help="Source candidates from headline_pool")
    parser.add_argument("--urls", help="Comma-separated candidate URLs (pool mode)")
    parser.add_argument("--selection-file", help="JSON file with {project, urls[]} (pool mode)")
    parser.add_argument(
        "--feed-job-id",
        type=int,
        default=None,
        help="FEED_DRAIN: resolve the selected story straight from the feed_jobs "
        "row (self-contained — no shell variable to lose across exec calls). "
        "Falls back to the durable feed_cards candidate if the /tmp selection "
        "file was pruned.",
    )
    parser.add_argument(
        "--pool-fresh",
        type=int,
        default=None,
        help="Pool mode: auto-pull the top N fresh pool candidates for the project "
        "(manual/auto runs where the picker SELECTS with diversity).",
    )
    parser.add_argument(
        "--classify-only",
        action="store_true",
        help="Keep ALL provided candidates; picker only classifies (no diversity selection/drop)",
    )
    args = parser.parse_args()

    pool_mode = bool(
        args.from_pool
        or args.urls
        or args.selection_file
        or args.pool_fresh
        or args.feed_job_id is not None
    )

    # In pool mode a selection file (or feed job) carries the authoritative
    # project + urls. Scanner refresh can drop the URL from headline_pool
    # after the human already tapped Run — keep the card/selection as source
    # of truth so Sieve does not die silently.
    pool_urls: list[str] = []
    selection_payload: dict = {}
    feed_job: editorial_db.FeedJob | None = None

    if args.feed_job_id is not None:
        # FEED_DRAIN: resolve the selection file straight from the feed_jobs row
        # so Step 1 never depends on a $SELECTION_FILE shell variable surviving
        # across separate exec calls (the confirmed root cause of intermittent
        # feed-drain failures).
        feed_job = editorial_db.get_feed_job(args.feed_job_id, db_path=args.db_path)
        if feed_job is None:
            print(
                f"PICKER_INPUT_ERROR: feed_job_not_found: id={args.feed_job_id} "
                "(job reclaimed/cleared, or wrong id passed)"
            )
            return 1
        if not args.project and feed_job.project:
            args.project = feed_job.project
        if feed_job.selection_file:
            try:
                selection_payload = _load_selection_payload(feed_job.selection_file)
                pool_urls = [str(u) for u in (selection_payload.get("urls") or []) if u]
            except (OSError, json.JSONDecodeError) as e:
                print(
                    f"PICKER_INPUT_WARN: selection-file unreadable "
                    f"({feed_job.selection_file}): {e} — trying feed card",
                    file=sys.stderr,
                )
        if not pool_urls:
            card_cands = _candidates_from_feed_card(feed_job, [], args.db_path)
            pool_urls = [c["url"] for c in card_cands if c.get("url")]
        if not pool_urls:
            print(
                f"PICKER_INPUT_ERROR: feed_job {args.feed_job_id} has no URL "
                f"(selection_file={feed_job.selection_file!r})"
            )
            return 1
    elif args.selection_file:
        try:
            selection_payload = _load_selection_payload(args.selection_file)
            pool_urls = [str(u) for u in (selection_payload.get("urls") or []) if u]
            if not args.project and selection_payload.get("project"):
                args.project = str(selection_payload["project"])
        except (OSError, json.JSONDecodeError) as e:
            print(f"PICKER_INPUT_ERROR: cannot read selection-file: {e}")
            return 1
        if not pool_urls:
            print(
                f"PICKER_INPUT_ERROR: selection produced 0 urls "
                f"(file={args.selection_file} — empty or no 'urls' key). "
                "For FEED_DRAIN prefer --feed-job-id <id>."
            )
            return 1
    elif args.urls:
        pool_urls = [u.strip() for u in args.urls.split(",") if u.strip()]

    if not pool_mode and not args.headlines:
        print(
            "PICKER_INPUT_ERROR: --headlines required unless "
            "--from-pool/--urls/--selection-file/--feed-job-id"
        )
        return 1

    try:
        cfg = pc.load_project_config(slug=args.project)
    except (FileNotFoundError, ValueError) as e:
        print(f"PICKER_INPUT_ERROR: {e}")
        return 1
    project_slug = cfg.slug

    if args.recent_window_hours is None:
        cfg_window = cfg.get_path("picker.diversity_window_hours", 72)
        try:
            args.recent_window_hours = int(cfg_window)
        except (TypeError, ValueError):
            args.recent_window_hours = 72

    # Build the WP category allow-list the Picker chooses from. Source of truth is
    # wordpress.categories (full live list, synced by sync_wp_categories.py).
    # wordpress.picker_category_slugs optionally narrows it to a curated subset.
    all_categories = cfg.get_path("wordpress.categories", []) or []
    if not isinstance(all_categories, list) or not all_categories:
        print(
            "PICKER_INPUT_ERROR: project has no wordpress.categories. "
            "Run sync_wp_categories.py --slug "
            f"{project_slug} first."
        )
        return 1
    curated_slugs = cfg.get_path("wordpress.picker_category_slugs", []) or []
    if not isinstance(curated_slugs, list):
        print("PICKER_INPUT_ERROR: wordpress.picker_category_slugs must be a list")
        return 1
    by_slug = {
        str(c.get("slug")): c
        for c in all_categories
        if isinstance(c, dict) and c.get("slug")
    }
    if curated_slugs:
        wp_categories = [
            {"slug": s, "name": by_slug[s].get("name", s)}
            for s in curated_slugs
            if s in by_slug
        ]
    else:
        wp_categories = [
            {"slug": c["slug"], "name": c.get("name", c["slug"])}
            for c in all_categories
            if isinstance(c, dict) and c.get("slug")
        ]
    if not wp_categories:
        print(
            "PICKER_INPUT_ERROR: no usable wp_categories after applying "
            "picker_category_slugs (none of the curated slugs exist in "
            "wordpress.categories)"
        )
        return 1

    if pool_mode:
        if not pool_urls and args.pool_fresh:
            fresh = editorial_db.fresh_pool(
                project_slug, limit=int(args.pool_fresh), db_path=args.db_path
            )
            pool_urls = [c.url for c in fresh]
        if not pool_urls:
            print("PICKER_INPUT_ERROR: pool mode but no candidates (empty pool / no urls)")
            return 1
        candidates_in = _load_pool_candidates(project_slug, pool_urls, args.db_path)
        if not candidates_in and (feed_job is not None or selection_payload):
            if feed_job is not None:
                candidates_in = _candidates_from_feed_card(
                    feed_job, pool_urls, args.db_path
                )
            if not candidates_in and selection_payload:
                candidates_in = _candidates_from_selection(selection_payload, feed_job)
            if candidates_in:
                print(
                    f"PICKER_INPUT_WARN: headline_pool miss; used saved card/"
                    f"selection ({len(candidates_in)} urls) project={project_slug}",
                    file=sys.stderr,
                )
        if not candidates_in:
            print(
                f"PICKER_INPUT_ERROR: none of the {len(pool_urls)} urls found in "
                f"headline_pool for project={project_slug}"
            )
            return 1
    else:
        headlines_path = os.path.realpath(args.headlines)
        if not os.path.exists(headlines_path):
            print(f"PICKER_INPUT_ERROR: headlines not found: {headlines_path}")
            return 1

        try:
            with open(headlines_path, encoding="utf-8") as f:
                hdata = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"PICKER_INPUT_ERROR: cannot parse headlines.json: {e}")
            return 1

        candidates_in = hdata.get("candidates") or []
        if not isinstance(candidates_in, list) or not candidates_in:
            print("PICKER_INPUT_ERROR: headlines.json has no candidates")
            return 1

    # Resolve target_count: in classify-only/pool mode default to candidate count.
    if args.target_count is None:
        args.target_count = len(candidates_in)
    if args.target_count < 1:
        print(f"PICKER_INPUT_ERROR: target_count must be >= 1, got {args.target_count}")
        return 1

    # Human FEED_DRAIN / classify-only: keep the tapped URL even if it ran before.
    skip_consumed = bool(args.classify_only or args.feed_job_id is not None)
    try:
        consumed = set() if skip_consumed else _consumed_pick_urls(args.db_path, project_slug)
    except sqlite3.Error as e:
        print(f"PICKER_INPUT_ERROR: db read failed: {e}")
        return 1

    try:
        recent_categories = editorial_db.recent_published_categories(
            hours=int(args.recent_window_hours),
            project=project_slug,
            db_path=args.db_path,
        )
    except sqlite3.Error as e:
        print(f"PICKER_INPUT_ERROR: db read failed: {e}")
        return 1

    candidates_out: list[dict] = []
    skipped_consumed = 0
    for c in candidates_in:
        if not isinstance(c, dict):
            continue
        url = str(c.get("url") or "").strip()
        if not url:
            continue
        if url in consumed:
            skipped_consumed += 1
            continue
        candidates_out.append({
            "candidate_index": len(candidates_out) + 1,
            "headline": str(c.get("headline") or "").strip(),
            "url": url,
            "pub_date": c.get("pub_date"),
            "source": str(c.get("source") or "Unknown"),
            "summary": str(c.get("summary") or ""),
            "corroborating_sources": c.get("corroborating_sources") or [],
        })

    if not candidates_out:
        print(
            f"PICKER_INPUT_ERROR: all {len(candidates_in)} candidates filtered out "
            f"(consumed={skipped_consumed})"
        )
        return 1

    # classify-only: every provided candidate is kept; the picker just labels
    # categories and never drops for diversity, so target == surviving count.
    if args.classify_only:
        args.target_count = len(candidates_out)

    out = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "project": project_slug,
        "project_name": cfg.get("name", project_slug),
        "target_count": int(args.target_count),
        "classify_only": bool(args.classify_only),
        "recent_window_hours": int(args.recent_window_hours),
        "recent_categories": recent_categories,
        "wp_categories": wp_categories,
        "skipped_consumed_urls": skipped_consumed,
        "candidates": candidates_out,
    }

    try:
        atomic_write_json(os.path.realpath(args.output), out)
    except OSError as e:
        print(f"PICKER_INPUT_ERROR: cannot write output: {e}")
        return 1

    print(
        f"PICKER_INPUT_BUILT: project={project_slug} / {len(candidates_out)} candidates / "
        f"target={args.target_count} / classify_only={bool(args.classify_only)} / "
        f"wp_categories={len(wp_categories)} / "
        f"recent={recent_categories or 'none'} / window={args.recent_window_hours}h / "
        f"skipped_consumed={skipped_consumed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
