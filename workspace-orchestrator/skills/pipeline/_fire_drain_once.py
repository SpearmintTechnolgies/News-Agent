#!/usr/bin/env python3
"""One-shot Windows-friendly feed drain fire (claim + openclaw cron)."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)

import editorial_db as db  # noqa: E402

DEFAULT_PROJECT = "coinnetwork"


def _bootstrap_reason(stdout: str, stderr: str) -> str:
    blob = f"{stderr or ''}\n{stdout or ''}"
    for line in blob.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (
            "PICKER_INPUT_ERROR" in stripped
            or "BOOTSTRAP_ERROR" in stripped
            or "SPAWN_PICKER_EMPTY" in stripped
            or "Unknown agent id" in stripped
        ):
            return stripped[:400]
    return "Sieve failed — worker will retry automatically."


def _run_continue(job: db.FeedJob, project: str, run_dir: str, env: dict[str, str]) -> int:
    cont = subprocess.run(
        [
            sys.executable,
            "-u",
            os.path.join(HERE, "continue_feed_drain.py"),
            "--project",
            project,
            "--feed-job-id",
            str(job.id),
            "--run-dir",
            run_dir,
            "--skip-picker",
        ],
        timeout=7200,
        env=env,
    )
    if cont.returncode != 0:
        print(f"CONTINUE_FAILED rc={cont.returncode} job_id={job.id}", file=sys.stderr)
        return cont.returncode or 1
    try:
        import drain_progress as progress

        progress.clear_attempts(project, job.id)
        progress.notify(
            job.id,
            "done",
            project=project,
            headline=job.headline,
        )
    except Exception as e:
        print(f"PROGRESS_WARN: {e}", file=sys.stderr)
    print(f"DISPATCH_FIRED: project={project} sieve+continue job_id={job.id}")
    return 0


def _fire_failed(job: db.FeedJob, project: str, reason: str) -> None:
    import drain_progress as progress

    n = progress.bump_attempts(project, job.id)
    if n >= 3:
        db.mark_feed_job(job.id, "failed")
        db.clear_drainer_lease(project)
        progress.notify(
            job.id,
            "sieve",
            failed=True,
            note=f"{reason} Stopped after 3 tries. Tap Run this story again.",
            project=project,
            headline=job.headline,
        )
        print(f"FIRE_FAILED_PERM job={job.id} tries={n}", file=sys.stderr)
        return
    db.mark_feed_job(job.id, "queued")
    db.clear_drainer_lease(project)
    progress.notify(
        job.id,
        "sieve",
        note=f"{reason} Retry {n}/3 in about a minute. Do not tap Run again.",
        project=project,
        headline=job.headline,
    )
    db.clear_drainer_lease(project)
    print(f"FIRE_FAILED bootstrap job={job.id} try={n}", file=sys.stderr)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Claim one feed job and fire FEED_DRAIN cron")
    parser.add_argument("--project", default=DEFAULT_PROJECT)
    args = parser.parse_args()
    project = (args.project or DEFAULT_PROJECT).strip() or DEFAULT_PROJECT
    db.init_db()
    reclaimed = db.reclaim_stale_jobs(float(os.environ.get("FEED_JOB_STALE_HOURS", "0.133")))
    if reclaimed:
        print(f"reclaimed={reclaimed}", file=sys.stderr)

    job = db.claim_next_feed_job(project=project)
    if not job:
        print("NO_QUEUED_JOB", file=sys.stderr)
        if project not in db.running_projects():
            db.clear_drainer_lease(project)
        return 2

    db.set_drainer_lease(project)
    try:
        import drain_progress as progress  # noqa: E402

        progress.notify(job.id, "sieve", project=project, headline=job.headline)
    except Exception as e:
        print(f"PROGRESS_WARN: {e}", file=sys.stderr)
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    log_path = env.get("DRAIN_LOG_PATH") or ""

    import bootstrap_feed_drain as boot  # noqa: E402

    saved = (db.get_state(project, f"feed_run_dir:{job.id}") or "").strip()
    if saved:
        saved = boot._normalize_run_dir(saved)
    saved_picks = os.path.join(saved, "picker", "picks.json") if saved else ""
    if saved and os.path.isfile(saved_picks) and os.path.getsize(saved_picks) >= 80:
        print(f"RESUME_RUN_DIR {saved}", file=sys.stderr)
        return _run_continue(job, project, saved, env)

    job_id_text = str(job.id)
    id_names = [
        os.path.join(os.environ.get("TEMP", ""), f"{project}-feed-job-id.txt"),
        os.path.join(r"C:\tmp", f"{project}-feed-job-id.txt"),
        os.path.join("/tmp", f"{project}-feed-job-id.txt"),
    ]
    for dest in id_names:
        if not dest or dest.startswith(os.sep) and os.name == "nt" and not dest.startswith("C:"):
            # still try /tmp — Git bash maps it
            pass
        try:
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(job_id_text)
        except OSError as e:
            print(f"JOB_ID_WRITE_WARN: {dest} {e}", file=sys.stderr)
    # Local bootstrap + Sieve, then local continue (Scout→Quill→Pixel→publish).
    # Do NOT schedule isolated Nexus cron — that dies and the group stays silent.
    bootstrap = [
        sys.executable,
        "-u",
        os.path.join(HERE, "bootstrap_feed_drain.py"),
        "--feed-job-id",
        str(job.id),
        "--project",
        project,
    ]
    print(f"firing: bootstrap+sieve+continue job_id={job.id}", file=sys.stderr)

    def _tee(text: str) -> None:
        if not text:
            return
        sys.stderr.write(text)
        if not text.endswith("\n"):
            sys.stderr.write("\n")
        sys.stderr.flush()
        if log_path:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(text if text.endswith("\n") else text + "\n")
            except OSError:
                pass

    try:
        res = subprocess.run(
            bootstrap,
            timeout=1200,
            env=env,
            text=True,
            capture_output=True,
        )
    except Exception as e:
        print(f"FIRE_EXCEPTION: {e}", file=sys.stderr)
        _fire_failed(job, project, f"Sieve crashed: {e}")
        return 1

    _tee(res.stdout or "")
    _tee(res.stderr or "")
    if res.returncode != 0:
        reason = _bootstrap_reason(res.stdout or "", res.stderr or "")
        _fire_failed(job, project, reason)
        return 1

    import bootstrap_feed_drain as boot  # noqa: E402
    import json

    run_dir = ""
    for line in reversed((res.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and "run_dir" in line:
            try:
                payload = json.loads(line)
                run_dir = boot._normalize_run_dir(str(payload.get("run_dir") or ""))
                if run_dir:
                    break
            except json.JSONDecodeError:
                continue
    if not run_dir:
        run_dir = boot._run_dir_from_env(project)
    picks = os.path.join(run_dir, "picker", "picks.json") if run_dir else ""
    if run_dir and (not os.path.isfile(picks) or os.path.getsize(picks) < 80):
        env_dir = boot._run_dir_from_env(project)
        env_picks = os.path.join(env_dir, "picker", "picks.json") if env_dir else ""
        if env_dir and os.path.isfile(env_picks) and os.path.getsize(env_picks) >= 80:
            run_dir = env_dir
    if not run_dir:
        print("FIRE_FAILED: no run_dir after sieve", file=sys.stderr)
        _fire_failed(job, project, "Sieve finished without a run folder.")
        return 1

    return _run_continue(job, project, run_dir, env)


if __name__ == "__main__":
    raise SystemExit(main())
