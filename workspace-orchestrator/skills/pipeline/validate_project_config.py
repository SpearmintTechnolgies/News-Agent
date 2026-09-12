#!/usr/bin/env python3
"""
validate_project_config.py — Deterministic pre/post-flight checks for a
projects/<slug>.json file.

Never modifies anything (read-only). Prints OK: / ERROR: / WARN: lines and
exits 0 only when there are zero ERROR lines.

Usage:
    python3 validate_project_config.py --slug coinnetwork
    python3 validate_project_config.py --slug coinnetwork --scanner-ready
    python3 validate_project_config.py --slug coinnetwork --openclaw-sync
    python3 validate_project_config.py --slug coinnetwork --live
    python3 validate_project_config.py --slug coinnetwork --scanner-ready --openclaw-sync --live
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import project_config as pc  # noqa: E402
from wp_rest_client import verify_auth, fetch_categories, WpAuthError, WpRequestError  # noqa: E402

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
SUPERGROUP_RE = re.compile(r"^-100\d+$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

REQUIRED_TOP_KEYS = ["slug", "callback_code", "name", "wordpress", "telegram", "research", "authors", "state"]


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.oks: list[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def ok(self, msg: str) -> None:
        self.oks.append(msg)

    def print_all(self) -> None:
        for m in self.oks:
            print(f"OK: {m}")
        for m in self.warnings:
            print(f"WARN: {m}")
        for m in self.errors:
            print(f"ERROR: {m}")

    @property
    def passed(self) -> bool:
        return not self.errors


def _load_all_projects() -> dict[str, dict]:
    out = {}
    for slug in pc.list_available_projects():
        try:
            out[slug] = dict(pc.load_project_config(slug=slug))
        except (FileNotFoundError, ValueError):
            continue
    return out


def validate_structure(cfg: dict, slug: str, source_path: str, report: Report) -> None:
    for key in REQUIRED_TOP_KEYS:
        if key not in cfg:
            report.error(f"missing required top-level key '{key}'")
    if cfg.get("slug") != slug:
        report.error(f"config 'slug' field ({cfg.get('slug')!r}) does not match requested slug ({slug!r})")
    filename_slug = os.path.splitext(os.path.basename(source_path))[0]
    if filename_slug != slug:
        report.error(f"filename '{filename_slug}.json' does not match slug '{slug}'")
    if not report.errors:
        report.ok("required top-level keys present; slug matches filename")


def validate_uniqueness(cfg: dict, slug: str, all_projects: dict[str, dict], report: Report) -> None:
    callback_code = cfg.get("callback_code")
    group_id = str((cfg.get("telegram") or {}).get("group_id") or "").strip()

    dup_slugs = [s for s in all_projects if s != slug and all_projects[s].get("slug") == cfg.get("slug")]
    if dup_slugs:
        report.error(f"slug '{cfg.get('slug')}' also declared in: {dup_slugs}")

    dup_codes = [
        s for s, c in all_projects.items()
        if s != slug and c.get("callback_code") == callback_code and callback_code
    ]
    if dup_codes:
        report.error(f"callback_code '{callback_code}' already used by: {dup_codes}")

    if group_id:
        dup_groups = [
            s for s, c in all_projects.items()
            if s != slug and str((c.get("telegram") or {}).get("group_id") or "").strip() == group_id
        ]
        if dup_groups:
            report.error(f"telegram.group_id '{group_id}' already used by: {dup_groups}")

    if not dup_slugs and not dup_codes and (not group_id or True):
        report.ok(f"slug/callback_code/group_id uniqueness checked against {len(all_projects)} project(s)")


def validate_credentials(cfg: dict, report: Report) -> None:
    wp = cfg.get("wordpress") or {}
    ref = wp.get("app_password_ref")
    if not ref:
        report.error("wordpress.app_password_ref is not set")
        return
    path = ref if os.path.isabs(ref) else os.path.join(pc.openclaw_root(), ref)
    if not os.path.exists(path):
        report.error(f"WP password file not found: {path}")
        return
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode != 0o600:
        report.error(f"WP password file {path} has mode {oct(mode)}, expected 0600 (chmod 600 it)")
    else:
        report.ok(f"WP password file exists at {path} with mode 600")


def validate_categories(cfg: dict, report: Report) -> None:
    wp = cfg.get("wordpress") or {}
    categories = wp.get("categories") or []
    picker_slugs = wp.get("picker_category_slugs") or []
    fallback_id = wp.get("fallback_category_id")

    if not categories:
        report.warn("wordpress.categories is empty — run sync_wp_categories.py")
        return

    known_slugs = {c.get("slug") for c in categories if isinstance(c, dict)}
    known_ids = {c.get("id") for c in categories if isinstance(c, dict)}

    unknown_picker = [s for s in picker_slugs if s not in known_slugs]
    if unknown_picker:
        report.error(f"picker_category_slugs contains slugs not in live categories: {unknown_picker}")
    else:
        report.ok(f"all {len(picker_slugs)} picker_category_slugs exist in live categories")

    if fallback_id is None:
        report.error("wordpress.fallback_category_id is not set")
    elif fallback_id not in known_ids:
        report.error(f"fallback_category_id {fallback_id} not found in live categories")
    else:
        fallback_slug = next((c.get("slug") for c in categories if c.get("id") == fallback_id), None)
        if fallback_slug and fallback_slug in picker_slugs:
            report.warn(
                f"fallback_category_id's slug '{fallback_slug}' is also in picker_category_slugs "
                f"— the picker may over-select the fallback category"
            )
        report.ok(f"fallback_category_id {fallback_id} exists in live categories")


def validate_authors(cfg: dict, report: Report) -> None:
    authors = cfg.get("authors") or []
    if not authors:
        report.error("authors[] is empty — the publish-author picker needs at least one")
        return
    ids = [a.get("id") for a in authors if isinstance(a, dict)]
    if len(ids) != len(set(ids)):
        report.error("authors[] contains duplicate ids")
    elif any(not isinstance(i, int) or i <= 0 for i in ids):
        report.error("authors[] ids must all be positive integers")
    else:
        report.ok(f"authors[] has {len(authors)} unique positive-int id(s)")


def validate_telegram_group_format(cfg: dict, report: Report) -> None:
    group_id = str((cfg.get("telegram") or {}).get("group_id") or "").strip()
    if not group_id:
        report.error("telegram.group_id is not set")
        return
    if not SUPERGROUP_RE.match(group_id):
        report.error(
            f"telegram.group_id '{group_id}' is not a supergroup id (expected ^-100\\d+$); "
            f"legacy basic-group ids cause routing drift"
        )
    else:
        report.ok(f"telegram.group_id '{group_id}' is a valid supergroup id")


def validate_logo(cfg: dict, report: Report) -> None:
    creator = cfg.get("creator") or {}
    logo_path = str(creator.get("logo_path") or "").strip()
    if not logo_path:
        report.error("creator.logo_path is not set")
        return
    abs_path = logo_path if os.path.isabs(logo_path) else os.path.join(pc.openclaw_root(), logo_path)
    if not os.path.isfile(abs_path):
        report.error(f"creator.logo_path file not found: {abs_path}")
        return
    try:
        with open(abs_path, "rb") as f:
            header = f.read(len(PNG_MAGIC))
    except OSError as e:
        report.error(f"cannot read creator.logo_path file {abs_path}: {e}")
        return
    if header != PNG_MAGIC:
        report.error(f"creator.logo_path is not a valid PNG: {abs_path}")
        return
    report.ok(f"creator.logo_path exists and is a valid PNG ({logo_path})")


def validate_scanner_ready(cfg: dict, report: Report) -> None:
    feeds = (cfg.get("research") or {}).get("rss_feeds") or []
    group_id = str((cfg.get("telegram") or {}).get("group_id") or "").strip()
    if not feeds:
        report.error("research.rss_feeds is empty — scanner will skip this project (POOL_SKIP reason=no_feeds)")
    else:
        report.ok(f"research.rss_feeds has {len(feeds)} feed(s)")
    if not group_id:
        report.error("telegram.group_id is not set — send_feed_card.py will skip this project")


def _load_openclaw_json() -> dict:
    with open(OPENCLAW_JSON, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_openclaw_sync(cfg: dict, slug: str, report: Report) -> None:
    group_id = str((cfg.get("telegram") or {}).get("group_id") or "").strip()
    account_id = str((cfg.get("telegram") or {}).get("account") or "news").strip()
    if not group_id:
        report.error("cannot check openclaw-sync: telegram.group_id is not set")
        return

    try:
        oc = _load_openclaw_json()
    except (OSError, json.JSONDecodeError) as e:
        report.error(f"cannot read/parse openclaw.json: {e}")
        return

    accounts = (((oc.get("channels") or {}).get("telegram") or {}).get("accounts") or {})
    account = accounts.get(account_id) or {}
    groups = account.get("groups") or {}

    group_entry = groups.get(group_id)
    if group_entry is None:
        report.error(f"openclaw.json: no channels.telegram.accounts.{account_id}.groups[\"{group_id}\"] entry")
    else:
        system_prompt = str(group_entry.get("systemPrompt") or "")
        expected = f"PROJECT_SLUG={slug}"
        if expected not in system_prompt:
            report.error(
                f"openclaw.json group entry for {group_id} does not contain '{expected}' in systemPrompt"
            )
        else:
            report.ok(f"openclaw.json groups[\"{group_id}\"].systemPrompt contains '{expected}'")

    bindings = oc.get("bindings") or []
    bound = False
    for b in bindings:
        match = b.get("match") or {}
        peer = match.get("peer") or {}
        if (
            match.get("channel") == "telegram"
            and match.get("accountId") == account_id
            and peer.get("kind") == "group"
            and str(peer.get("id")) == group_id
        ):
            bound = True
            break
    if not bound:
        report.error(f"openclaw.json: no bindings[] entry routes group {group_id} to an agent")
    else:
        report.ok(f"openclaw.json bindings[] has an entry for group {group_id}")


def validate_live(cfg: dict, report: Report) -> None:
    wp = cfg.get("wordpress") or {}
    url = wp.get("url")
    user = wp.get("user")
    ref = wp.get("app_password_ref")
    if not (url and user and ref):
        report.error("cannot run --live check: wordpress.url/user/app_password_ref incomplete")
        return
    path = ref if os.path.isabs(ref) else os.path.join(pc.openclaw_root(), ref)
    try:
        with open(path, "r", encoding="utf-8") as f:
            password = f.read().strip()
    except OSError as e:
        report.error(f"cannot read WP password file for --live check: {e}")
        return

    try:
        me = verify_auth(url, user, password)
        report.ok(f"live WP auth OK — authenticated as '{me.get('name', user)}' (id={me.get('id')})")
    except WpAuthError as e:
        report.error(f"live WP auth FAILED: {e}")
        return
    except WpRequestError as e:
        report.error(f"live WP request error: {e}")
        return

    try:
        live_categories = fetch_categories(url, user, password)
        stored_slugs = {c.get("slug") for c in (wp.get("categories") or [])}
        live_slugs = {c.get("slug") for c in live_categories}
        if stored_slugs != live_slugs:
            report.warn(
                f"stored categories ({len(stored_slugs)}) differ from live WP categories ({len(live_slugs)}) "
                f"— re-run sync_wp_categories.py"
            )
        else:
            report.ok(f"stored categories match live WP ({len(live_slugs)} categories)")
    except (WpAuthError, WpRequestError) as e:
        report.error(f"live category fetch failed: {e}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slug", required=True, help="Project slug to validate")
    parser.add_argument("--scanner-ready", action="store_true", help="Check research.rss_feeds + telegram.group_id are set")
    parser.add_argument("--openclaw-sync", action="store_true", help="Cross-check openclaw.json groups + bindings alignment")
    parser.add_argument("--live", action="store_true", help="Re-verify WP auth and diff live categories (network call)")
    args = parser.parse_args()

    report = Report()

    try:
        project_cfg = pc.load_project_config(slug=args.slug)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}")
        return 1

    cfg = dict(project_cfg)
    source_path = project_cfg.source_path

    validate_structure(cfg, args.slug, source_path, report)
    if report.errors:
        report.print_all()
        return 1

    all_projects = _load_all_projects()
    validate_uniqueness(cfg, args.slug, all_projects, report)
    validate_credentials(cfg, report)
    validate_categories(cfg, report)
    validate_authors(cfg, report)
    validate_telegram_group_format(cfg, report)
    validate_logo(cfg, report)

    if args.scanner_ready:
        validate_scanner_ready(cfg, report)
    if args.openclaw_sync:
        validate_openclaw_sync(cfg, args.slug, report)
    if args.live:
        validate_live(cfg, report)

    report.print_all()
    print()
    if report.passed:
        print(f"VALIDATE_OK: {args.slug} — {len(report.oks)} check(s) passed, {len(report.warnings)} warning(s)")
        return 0
    print(f"VALIDATE_FAILED: {args.slug} — {len(report.errors)} error(s), {len(report.warnings)} warning(s)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
