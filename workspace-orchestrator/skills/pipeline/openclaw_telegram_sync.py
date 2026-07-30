#!/usr/bin/env python3
"""Shared helpers to sync Telegram group + binding entries in openclaw.json from project configs."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import tempfile
import time
from datetime import datetime, timezone

ACCOUNT_ID = "news"

OPENCLAW_JSON = os.path.expanduser("~/.openclaw/openclaw.json")
BACKUP_DIR = os.path.expanduser("~/.openclaw/.backups/openclaw-json")
ENSURE_SCHEDULER = os.path.expanduser(
    "~/.openclaw/workspace-orchestrator/skills/pipeline/ensure_scheduler.sh"
)


def load_openclaw(path: str | None = None) -> dict:
    path = path or OPENCLAW_JSON
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def backup_openclaw(path: str | None = None) -> str:
    path = path or OPENCLAW_JSON
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = os.path.join(BACKUP_DIR, f"openclaw.json.bak.{ts}")
    with open(path, "rb") as src, open(dest, "wb") as dst:
        dst.write(src.read())
    return dest


def atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(os.path.abspath(path))
    os.makedirs(dir_, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=dir_, delete=False, suffix=".tmp") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        tmp = f.name
    os.replace(tmp, path)


def build_patch(cfg: dict, *, slug: str, group_id: str, name: str) -> tuple[dict, list[str]]:
    """Return (patched_copy, human_readable_change_lines). Never mutates cfg."""
    new_cfg = copy.deepcopy(cfg)
    changes: list[str] = []

    telegram = new_cfg.setdefault("channels", {}).setdefault("telegram", {})
    accounts = telegram.setdefault("accounts", {})
    account = accounts.setdefault(ACCOUNT_ID, {})
    groups = account.setdefault("groups", {})

    system_prompt = (
        f"PROJECT_SLUG={slug}. You operate EXCLUSIVELY for the {name} project in this group. "
        f"Never ask which project; never act on other projects here."
    )
    desired_group = {"requireMention": True, "systemPrompt": system_prompt}
    existing_group = groups.get(group_id)
    if existing_group != desired_group:
        groups[group_id] = desired_group
        changes.append(
            f'channels.telegram.accounts.{ACCOUNT_ID}.groups["{group_id}"] = '
            f"{{requireMention: true, systemPrompt: PROJECT_SLUG={slug}...}}"
        )
    else:
        changes.append(f'(no change) groups["{group_id}"] already correct')

    bindings = new_cfg.setdefault("bindings", [])
    already_bound = any(
        (b.get("match") or {}).get("channel") == "telegram"
        and (b.get("match") or {}).get("accountId") == ACCOUNT_ID
        and ((b.get("match") or {}).get("peer") or {}).get("kind") == "group"
        and str(((b.get("match") or {}).get("peer") or {}).get("id")) == group_id
        for b in bindings
    )
    if not already_bound:
        catchall_idx = next(
            (
                i
                for i, b in enumerate(bindings)
                if (b.get("match") or {}).get("channel") == "telegram"
                and (b.get("match") or {}).get("accountId") == ACCOUNT_ID
                and "peer" not in (b.get("match") or {})
            ),
            None,
        )
        new_binding = {
            "agentId": "orchestrator",
            "match": {
                "channel": "telegram",
                "accountId": ACCOUNT_ID,
                "peer": {"kind": "group", "id": group_id},
            },
        }
        if catchall_idx is not None:
            bindings.insert(catchall_idx, new_binding)
        else:
            bindings.append(new_binding)
        changes.append(f"bindings[] += orchestrator binding for group {group_id}")
    else:
        changes.append(f"(no change) bindings[] already has an entry for group {group_id}")

    return new_cfg, changes


def sync_projects_into_config(
    cfg: dict,
    projects: list[tuple[str, str, str]],
) -> tuple[dict, list[str]]:
    """Apply build_patch for each (slug, group_id, name). Returns merged config + change lines."""
    merged = copy.deepcopy(cfg)
    all_changes: list[str] = []
    for slug, group_id, name in projects:
        merged, changes = build_patch(merged, slug=slug, group_id=group_id, name=name)
        for line in changes:
            if not line.startswith("(no change)"):
                all_changes.append(f"{slug}: {line}")
    return merged, all_changes


def restart_gateway() -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["openclaw", "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        out = (result.stdout or "") + (result.stderr or "")
        if result.returncode != 0:
            return False, f"'openclaw gateway restart' exited {result.returncode}:\n{out}"
        return True, out
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"could not run 'openclaw gateway restart': {e}"


def check_gateway_health(*, retries: int = 5, delay_sec: float = 2.0) -> tuple[bool, str]:
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["openclaw", "gateway", "health"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if result.returncode == 0:
                return True, result.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
        if attempt < retries - 1:
            time.sleep(delay_sec)
    return False, "gateway health check did not succeed after restart"


def run_ensure_scheduler() -> tuple[bool, str]:
    if not os.path.isfile(ENSURE_SCHEDULER):
        return False, f"ensure_scheduler.sh not found at {ENSURE_SCHEDULER}"
    try:
        result = subprocess.run(
            ["bash", ENSURE_SCHEDULER],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0, (result.stdout + result.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def validate_json_roundtrip(data: dict) -> bool:
    try:
        json.loads(json.dumps(data))
        return True
    except (TypeError, ValueError):
        return False
