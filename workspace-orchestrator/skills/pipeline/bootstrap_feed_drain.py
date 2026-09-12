#!/usr/bin/env python3
"""Windows-safe FEED_DRAIN bootstrap: ensure RUN_DIR, write picker_input.json.

Creates the run via init_run.sh when none exists. Reuses an empty placeholder
run instead of leaving 0-byte picker_input. Nexus must only exec oc-drain-first.

Usage:
  python bootstrap_feed_drain.py --feed-job-id 122 [--project coinnetwork]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
INIT_RUN = os.path.join(HERE, "init_run.sh")


def _git_bash() -> str:
    for key in ("OPENCLAW_BASH", "GIT_BASH"):
        env = (os.environ.get(key) or "").strip().strip('"')
        if env and os.path.isfile(env):
            return env
    for path in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
    ):
        if os.path.isfile(path):
            return path
    return "bash"


def _env_paths(project: str) -> list[str]:
    home_runs = os.path.join(os.path.expanduser("~"), ".openclaw", "runs")
    return [
        os.path.join(home_runs, f"{project}-run-env.sh"),
        os.path.join("/tmp", f"{project}-run-env.sh"),
        os.path.join(r"C:\tmp", f"{project}-run-env.sh"),
        os.path.join(os.environ.get("TEMP", ""), f"{project}-run-env.sh"),
    ]


def _git_to_win_path(raw: str) -> str:
    p = raw.replace("\\", "/").strip().strip('"').strip("'")
    if len(p) >= 4 and p[0] == "/" and p[2] == "/" and p[1].isalpha():
        return p[1].upper() + ":\\" + p[3:].replace("/", "\\")
    return ""


def _normalize_run_dir(run_dir: str) -> str:
    raw = str(run_dir or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    candidates = [raw, raw.replace("\\", "/"), raw.replace("/", "\\")]
    git_win = _git_to_win_path(raw)
    if git_win:
        candidates.append(git_win)
    if raw.startswith("/tmp/") or raw.startswith("\\tmp\\"):
        rest = raw.replace("\\", "/")[5:].lstrip("/")
        candidates.append(os.path.join(r"C:\tmp", rest.replace("/", os.sep)))
        temp = os.environ.get("TEMP") or ""
        if temp:
            candidates.append(os.path.join(temp, os.path.basename(raw.replace("\\", "/"))))
        home_runs = os.path.join(os.path.expanduser("~"), ".openclaw", "runs")
        candidates.append(os.path.join(home_runs, os.path.basename(raw.replace("\\", "/"))))
    for cand in candidates:
        if cand and os.path.isdir(cand):
            return os.path.abspath(cand)
    return ""


def _run_dir_from_env(project: str) -> str:
    for env_path in _env_paths(project):
        if not os.path.isfile(env_path):
            continue
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("export RUN_DIR="):
                        found = _normalize_run_dir(line.split("=", 1)[1])
                        if found:
                            return found
        except OSError:
            continue
    return ""


def _init_run(project: str) -> str:
    env = os.environ.copy()
    home = os.path.expanduser("~")
    env.setdefault("HOME", home)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    py311 = os.path.join(home, r"AppData\Local\Programs\Python\Python311")
    if os.path.isdir(py311):
        env["PATH"] = py311 + os.pathsep + env.get("PATH", "")
    subprocess.check_call(
        [_git_bash(), INIT_RUN, project],
        env=env,
        timeout=120,
    )
    run_dir = _run_dir_from_env(project)
    if not run_dir:
        raise RuntimeError("init_run.sh did not write RUN_DIR")
    return run_dir


def ensure_run_dir(project: str) -> str:
    """Always start a fresh run. Reusing yesterday's env pointed drain at Cardano."""
    return _init_run(project)


def _openclaw_bin() -> str:
    import shutil

    return (
        shutil.which("openclaw")
        or shutil.which("openclaw.cmd")
        or r"C:\Users\Aditya Singh\AppData\Roaming\npm\openclaw.cmd"
    )


def _node_exe() -> str:
    pinned = (os.environ.get("OPENCLAW_NODE_EXE") or "").strip().strip('"')
    if pinned and os.path.isfile(pinned):
        return pinned
    for path in (
        r"C:\Program Files\nodejs\node.exe",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "nodejs", "node.exe"),
    ):
        if os.path.isfile(path):
            return path
    return ""


def _openclaw_mjs() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser(r"~\AppData\Roaming")
    path = os.path.join(appdata, "npm", "node_modules", "openclaw", "openclaw.mjs")
    return path if os.path.isfile(path) else ""


def _openclaw_env() -> dict[str, str]:
    """Env for `openclaw agent` on Windows.

    `openclaw.cmd` runs a bare `node`. cmd.exe searches CWD first, so a drain
    started from ~/.openclaw hits ~/.openclaw/node.cmd (laptop-node watchdog)
    instead of Node.js. That process never classifies and Sieve looks stuck.

    Always pin OPENCLAW_CONFIG_PATH / OPENCLAW_STATE_DIR to the host
    ~/.openclaw tree. Docker boot and leftover gateway env vars otherwise make
    `openclaw agent --agent picker` briefly resolve an empty agents.list
    ("Unknown agent id picker") and burn the 3 Sieve retries.
    """
    env = os.environ.copy()
    home_oc = os.path.abspath(os.path.expanduser("~/.openclaw"))
    node_dir = os.path.dirname(_node_exe()) if _node_exe() else r"C:\Program Files\nodejs"
    parts: list[str] = []
    seen: set[str] = set()

    def _add(folder: str) -> None:
        if not folder:
            return
        key = os.path.normcase(os.path.abspath(folder))
        if key in seen or not os.path.isdir(folder):
            return
        seen.add(key)
        parts.append(folder)

    _add(node_dir)
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        _add(os.path.join(appdata, "npm"))
    for raw in env.get("PATH", "").split(os.pathsep):
        if not raw:
            continue
        abs_p = os.path.abspath(raw)
        if os.path.normcase(abs_p) == os.path.normcase(home_oc):
            continue
        if os.path.isfile(os.path.join(abs_p, "node.cmd")) and os.path.normcase(
            os.path.basename(abs_p)
        ) != "nodejs":
            continue
        _add(raw)
    env["PATH"] = os.pathsep.join(parts)
    for key in (
        "OPENCLAW_WINDOWS_TASK_NAME",
        "OPENCLAW_SERVICE_MARKER",
        "OPENCLAW_SERVICE_KIND",
        "OPENCLAW_LOG_PREFIX",
        "OPENCLAW_GATEWAY_TOKEN",
    ):
        env.pop(key, None)
    cfg_path = os.path.join(home_oc, "openclaw.json")
    env["OPENCLAW_STATE_DIR"] = home_oc
    if os.path.isfile(cfg_path):
        env["OPENCLAW_CONFIG_PATH"] = cfg_path
    else:
        env.pop("OPENCLAW_CONFIG_PATH", None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _openclaw_agent_cmd(*args: str) -> list[str]:
    node = _node_exe()
    mjs = _openclaw_mjs()
    if node and mjs:
        return [node, mjs, *args]
    return [_openclaw_bin(), *args]


def _posix_run(run_dir: str) -> str:
    """Path the picker `read` tool can open (real Windows path, not Git /tmp)."""
    return os.path.abspath(run_dir).replace("\\", "/")


def _picker_known(env: dict[str, str]) -> bool:
    """True when host openclaw.json currently lists the picker agent."""
    cmd = _openclaw_agent_cmd("agents", "list")
    try:
        res = subprocess.run(
            cmd,
            timeout=90,
            env=env,
            text=True,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"PICKER_PREFLIGHT_WARN: {e}", file=sys.stderr)
        return False
    blob = f"{res.stdout or ''}\n{res.stderr or ''}"
    return bool(re.search(r"(?m)^-\s+picker\b", blob))


def _spawn_log_reason(spawn_log: str) -> str:
    try:
        text = open(spawn_log, encoding="utf-8", errors="replace").read().strip()
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "Unknown agent id" in stripped or stripped.startswith("Error:"):
            return stripped[:300]
    return (text.splitlines() or [""])[0][:300]


def spawn_picker(run_dir: str) -> int:
    """Run Sieve via CLI --message-file (newlines survive). Call from local Python only."""
    posix = _posix_run(run_dir)
    msg_path = os.path.join(run_dir, "picker", "spawn_picker.txt")
    os.makedirs(os.path.dirname(msg_path), exist_ok=True)
    with open(msg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"INPUT_FILE: {posix}/picker/picker_input.json\n")
        f.write(f"OUTPUT_FILE: {posix}/picker/picks.json\n")
        f.write("Classify-only. First tool: read INPUT_FILE. Second tool: write OUTPUT_FILE.\n")
        f.write("Keep every candidate headline/url verbatim. Do not invent stories.\n")
        f.write("No exec. No Telegram. No /home/bhard. Reply SUCCESS after write.\n")
    env = _openclaw_env()
    # Docker / gateway boot can briefly hide agents.list — wait instead of failing.
    for wait_i in range(1, 7):
        if _picker_known(env):
            break
        print(
            f"PICKER_PREFLIGHT: picker not in agents.list yet; waiting {wait_i}/6",
            flush=True,
        )
        time.sleep(5)
    else:
        print(
            'SPAWN_PICKER_EMPTY picks=0 rc=1 reason=Unknown agent id "picker" '
            "(agents.list missing picker after preflight waits)",
            file=sys.stderr,
        )
        return 1

    cmd = _openclaw_agent_cmd(
        "agent",
        "--agent",
        "picker",
        "--timeout",
        "900",
        "--session-key",
        f"agent:picker:{os.path.basename(run_dir)}",
        "--message-file",
        msg_path,
    )
    print(f"SPAWN_PICKER {posix}/picker/picks.json", flush=True)

    spawn_log = os.path.join(run_dir, "picker", "spawn.log")
    picker_dir = os.path.dirname(msg_path)
    picks = os.path.join(run_dir, "picker", "picks.json")
    last_rc = 1
    for attempt in range(1, 4):
        with open(spawn_log, "w", encoding="utf-8") as logf:
            res = subprocess.run(
                cmd,
                timeout=960,
                env=env,
                cwd=picker_dir,
                stdout=logf,
                stderr=subprocess.STDOUT,
            )
        last_rc = res.returncode or 1
        size = os.path.getsize(picks) if os.path.isfile(picks) else 0
        if size >= 80:
            break
        reason = _spawn_log_reason(spawn_log) or "empty picks.json"
        print(
            f"SPAWN_PICKER_RETRY {attempt}/3 picks={size} rc={last_rc} reason={reason}",
            file=sys.stderr,
        )
        if "Unknown agent id" in reason:
            time.sleep(8)
        else:
            time.sleep(3)
    size = os.path.getsize(picks) if os.path.isfile(picks) else 0
    if size < 80:
        reason = _spawn_log_reason(spawn_log) or "empty picks.json"
        print(
            f"SPAWN_PICKER_EMPTY picks={size} rc={last_rc} reason={reason} log={spawn_log}",
            file=sys.stderr,
        )
        return last_rc or 1
    try:
        raw = open(picks, "rb").read()
        raw.decode("utf-8")
    except UnicodeDecodeError:
        for enc in ("cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                json.loads(text)
                with open(picks, "w", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                print(f"PICKS_REENCODED from={enc}", flush=True)
                break
            except (UnicodeDecodeError, json.JSONDecodeError, OSError):
                continue
    print(f"PICKS_READY bytes={os.path.getsize(picks)} path={picks}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--feed-job-id", type=int, required=True)
    p.add_argument("--project", default="coinnetwork")
    p.add_argument("--run-dir", default="")
    p.add_argument("--no-spawn-picker", action="store_true")
    args = p.parse_args()

    run_dir = args.run_dir.strip() or ensure_run_dir(args.project)
    run_dir = os.path.abspath(run_dir)
    manifest = os.path.join(run_dir, "manifest.json")
    out = os.path.join(run_dir, "picker", "picker_input.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run_id = os.path.basename(run_dir).replace(f"{args.project}-run-", "")

    subprocess.check_call(
        [
            sys.executable,
            os.path.join(HERE, "update_manifest_batch.py"),
            "--manifest",
            manifest,
            "--target",
            "1",
            "--pick-run-id",
            f"{run_id}-pick",
        ]
    )
    subprocess.check_call(
        [
            sys.executable,
            os.path.join(HERE, "build_picker_input.py"),
            "--feed-job-id",
            str(args.feed_job_id),
            "--classify-only",
            "--output",
            out,
            "--project",
            args.project,
        ]
    )
    size = os.path.getsize(out) if os.path.isfile(out) else 0
    if size < 80:
        print(f"BOOTSTRAP_ERROR: picker_input too small ({size} bytes)", file=sys.stderr)
        return 1
    print(f"BOOTSTRAP_OK run_dir={run_dir} picker_input={out} bytes={size}")
    print(json.dumps({"run_dir": run_dir, "picker_input": out, "run_id": run_id}))
    if args.no_spawn_picker:
        return 0
    return spawn_picker(run_dir)


if __name__ == "__main__":
    raise SystemExit(main())
