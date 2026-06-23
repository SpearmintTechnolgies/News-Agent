#!/usr/bin/env python3
"""project_config.py - Shared loader for per-project pipeline config.

A "project" represents one publishing target (one WordPress site + its niche
+ its editorial settings). All pipeline scripts read their per-site knobs
through this loader so the same agents can drive multiple sites by config
alone.

Resolution order:
  1. Explicit path (function arg).
  2. PROJECT_CONFIG env var (absolute path to projects/<slug>.json).
  3. PROJECT_SLUG env var (resolved to ~/.openclaw/projects/<slug>.json).
  4. manifest.json's `project` field (resolved to projects/<slug>.json).
  5. Fallback default slug 'coinography' (backward compat).

Usage:
    from project_config import load_project_config, project_root
    cfg = load_project_config()         # auto-discover
    wp = cfg["wordpress"]
    pw = cfg.wp_password()              # reads file referenced by
                                        # app_password_ref relative to
                                        # ~/.openclaw/

CLI:
    python3 project_config.py --slug coinography --field wordpress.url
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional

DEFAULT_PROJECT_SLUG = "coinography"


def openclaw_root() -> str:
    """Resolve ~/.openclaw (allow override via OPENCLAW_HOME env)."""
    env = os.environ.get("OPENCLAW_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.abspath(os.path.expanduser("~/.openclaw"))


def projects_dir() -> str:
    return os.path.join(openclaw_root(), "projects")


def project_path_for_slug(slug: str) -> str:
    return os.path.join(projects_dir(), f"{slug}.json")


def resolve_openclaw_path(rel: str) -> str:
    """Resolve a config-relative path to an absolute filesystem path.

    Paths stored in project configs (e.g. writer.template_path,
    wordpress.app_password_ref) are relative to ~/.openclaw. Sub-agents run
    with their own cwd (e.g. ~/.openclaw/workspace-writer), so resolving such a
    path relative to cwd doubles the prefix and fails. Always resolve via this
    helper. Absolute inputs are returned unchanged.
    """
    rel = os.path.expanduser(rel)
    if os.path.isabs(rel):
        return rel
    return os.path.join(openclaw_root(), rel)


class ProjectConfig(dict):
    """dict subclass that knows where it came from + how to read its password file."""

    def __init__(self, data: dict, source_path: str):
        super().__init__(data)
        self._source_path = source_path

    @property
    def source_path(self) -> str:
        return self._source_path

    @property
    def slug(self) -> str:
        return self["slug"]

    def wp_password(self) -> str:
        """Read the password from app_password_ref (relative to ~/.openclaw/).

        Raises FileNotFoundError with a clear message if missing.
        """
        ref = self.get("wordpress", {}).get("app_password_ref")
        if not ref:
            raise KeyError(
                f"project '{self.slug}' has no wordpress.app_password_ref in "
                f"{self._source_path}"
            )
        if os.path.isabs(ref):
            path = ref
        else:
            path = os.path.join(openclaw_root(), ref)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"WP password file for project '{self.slug}' not found at {path}. "
                f"Create it (mode 600) with the application password."
            )
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()

    def get_path(self, dotted_key: str, default: Any = None) -> Any:
        """Look up a nested key via dotted notation (e.g. wordpress.url)."""
        cur: Any = self
        for part in dotted_key.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur


def _read_manifest_project(manifest_path: str) -> Optional[str]:
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    val = data.get("project")
    return val if isinstance(val, str) and val else None


def resolve_project_slug(
    *,
    explicit_slug: Optional[str] = None,
    manifest_path: Optional[str] = None,
) -> str:
    """Resolve the active project slug using the documented order."""
    if explicit_slug:
        return explicit_slug
    env_slug = os.environ.get("PROJECT_SLUG")
    if env_slug:
        return env_slug
    env_cfg = os.environ.get("PROJECT_CONFIG")
    if env_cfg and os.path.exists(env_cfg):
        return os.path.splitext(os.path.basename(env_cfg))[0]
    if manifest_path is None:
        manifest_path = os.environ.get("PIPELINE_MANIFEST")
    if manifest_path and os.path.exists(manifest_path):
        slug = _read_manifest_project(manifest_path)
        if slug:
            return slug
    # Canonical active-manifest pointer (env-free fallback for isolated sub-agents)
    canonical = "/tmp/openclaw-active-manifest.json"
    if os.path.exists(canonical):
        slug = _read_manifest_project(canonical)
        if slug:
            return slug
    return DEFAULT_PROJECT_SLUG


def load_project_config(
    *,
    slug: Optional[str] = None,
    path: Optional[str] = None,
    manifest_path: Optional[str] = None,
) -> ProjectConfig:
    """Load a project config from explicit path, slug, or auto-discovery."""
    if path:
        source = os.path.abspath(path)
    else:
        if not slug:
            slug = resolve_project_slug(manifest_path=manifest_path)
        source = project_path_for_slug(slug)
    if not os.path.exists(source):
        raise FileNotFoundError(
            f"Project config not found: {source}. "
            f"Available projects: {list_available_projects()}"
        )
    with open(source, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"Project config at {source} is not valid JSON: {e}") from e
    if not isinstance(data, dict) or "slug" not in data:
        raise ValueError(f"Project config at {source} missing required 'slug' field")
    if slug and data["slug"] != slug:
        raise ValueError(
            f"Project config slug mismatch: file {source} declares "
            f"slug='{data['slug']}' but was requested as slug='{slug}'"
        )
    return ProjectConfig(data, source)


def list_available_projects() -> list[str]:
    pdir = projects_dir()
    if not os.path.isdir(pdir):
        return []
    out = []
    for fn in sorted(os.listdir(pdir)):
        if fn.endswith(".json") and not fn.startswith("_"):
            out.append(fn[:-5])
    return out


def _normalize_chat_id(chat_id: str) -> str:
    """Normalize Telegram chat ids for lookup (strip channel prefix)."""
    cid = str(chat_id or "").strip()
    if cid.startswith("telegram:"):
        cid = cid.split(":", 1)[1]
    return cid


def resolve_project_for_chat(chat_id: str) -> Optional[str]:
    """Return the project slug bound to a Telegram group chat id, if any.

    Inverts ``telegram.group_id`` from each project config. Returns ``None``
    when the chat is not bound (e.g. DMs or unknown groups).
    """
    cid = _normalize_chat_id(chat_id)
    if not cid:
        return None
    for slug in list_available_projects():
        try:
            cfg = load_project_config(slug=slug)
        except (FileNotFoundError, ValueError):
            continue
        group_id = str(cfg.get_path("telegram.group_id") or "").strip()
        if group_id and _normalize_chat_id(group_id) == cid:
            return slug
    return None


def assert_project_matches_manifest(cfg: ProjectConfig, manifest_path: str) -> None:
    """Reliability gate: env-loaded project must match the manifest's project.

    Call this in any worker that runs mid-pipeline. Fails fast and loud
    if there's a mismatch (would indicate a wrong-site publish risk).
    """
    if not os.path.exists(manifest_path):
        return  # no manifest yet (e.g. Step 0 itself)
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(
            f"Cannot read manifest at {manifest_path} to verify project: {e}"
        ) from e
    manifest_slug = manifest.get("project")
    if manifest_slug and manifest_slug != cfg.slug:
        raise RuntimeError(
            f"PROJECT MISMATCH: project_config slug='{cfg.slug}' but manifest "
            f"says project='{manifest_slug}'. Aborting to prevent publishing "
            f"to the wrong site."
        )


def main() -> int:
    p = argparse.ArgumentParser(description="Inspect a project config")
    p.add_argument("--slug", help="Project slug (defaults to env / manifest / coinography)")
    p.add_argument("--path", help="Explicit project config path (overrides --slug)")
    p.add_argument("--field", help="Dotted key to print (e.g. wordpress.url). If omitted, prints full JSON.")
    p.add_argument(
        "--absolute",
        action="store_true",
        help="With --field: resolve string value relative to ~/.openclaw and verify the path exists",
    )
    p.add_argument("--password", action="store_true", help="Print resolved WP password to stdout (use with care)")
    p.add_argument("--list", action="store_true", help="List available project slugs")
    p.add_argument(
        "--chat-id",
        help="Telegram chat id; prints the bound project slug (exit 1 if unbound)",
    )
    args = p.parse_args()

    if args.chat_id:
        slug = resolve_project_for_chat(args.chat_id)
        if not slug:
            print(
                f"PROJECT_CONFIG_ERROR: no project bound to chat_id={args.chat_id}",
                file=sys.stderr,
            )
            return 1
        print(slug)
        return 0

    if args.list:
        for s in list_available_projects():
            print(s)
        return 0

    try:
        cfg = load_project_config(slug=args.slug, path=args.path)
    except (FileNotFoundError, ValueError) as e:
        print(f"PROJECT_CONFIG_ERROR: {e}", file=sys.stderr)
        return 1

    if args.password:
        try:
            print(cfg.wp_password())
        except (KeyError, FileNotFoundError) as e:
            print(f"PROJECT_CONFIG_ERROR: {e}", file=sys.stderr)
            return 1
        return 0

    if args.field:
        val = cfg.get_path(args.field)
        if val is None:
            print(f"PROJECT_CONFIG_ERROR: field '{args.field}' not found in {cfg.source_path}", file=sys.stderr)
            return 1
        if args.absolute:
            if not isinstance(val, str):
                print(
                    f"PROJECT_CONFIG_ERROR: field '{args.field}' is not a string path "
                    f"(got {type(val).__name__})",
                    file=sys.stderr,
                )
                return 1
            resolved = resolve_openclaw_path(val)
            if not os.path.exists(resolved):
                print(
                    f"PROJECT_CONFIG_ERROR: resolved path does not exist: {resolved}",
                    file=sys.stderr,
                )
                return 1
            print(resolved)
            return 0
        if isinstance(val, (dict, list)):
            print(json.dumps(val, indent=2))
        else:
            print(val)
        return 0

    print(json.dumps(cfg, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
