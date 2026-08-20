#!/usr/bin/env python3
"""Local FEED_DRAIN after Sieve: Scout → Quill → Pixel → WP draft → Telegram card.

No Nexus cron. No nested gateway spawn. Call from _fire_drain_once.py after
bootstrap+Sieve, or from Cursor to unstick a queued job.

Usage:
  python continue_feed_drain.py --project coinnetwork --feed-job-id 127
  python continue_feed_drain.py --run-dir C:/tmp/coinnetwork-run-... --feed-job-id 127
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
sys.path.insert(0, HERE)

import bootstrap_feed_drain as boot  # noqa: E402
import editorial_db as db  # noqa: E402

HOME = os.path.expanduser("~")
PY = sys.executable
RESEARCHER = os.path.join(
    HOME, ".openclaw", "workspace-researcher", "skills", "deep-research", "run_research.py"
)
QUALIFY_CLAIMS = os.path.join(
    HOME, ".openclaw", "workspace-researcher", "skills", "deep-research", "qualify_claims.py"
)
VERIFY_CLAIMS = os.path.join(
    HOME, ".openclaw", "workspace-researcher", "skills", "deep-research", "verify_claims.py"
)
CHECK_CLAIM_CONSISTENCY = os.path.join(
    HOME,
    ".openclaw",
    "workspace-researcher",
    "skills",
    "deep-research",
    "check_claim_consistency.py",
)
AUDIT_ARTICLE_EVIDENCE = os.path.join(
    HOME,
    ".openclaw",
    "workspace-researcher",
    "skills",
    "deep-research",
    "audit_article_evidence.py",
)
WRITER_CHECK = os.path.join(
    HOME, ".openclaw", "workspace-writer", "skills", "article"
)
CREATOR_GEN = os.path.join(
    HOME, ".openclaw", "workspace-creator", "skills", "generate-image", "generate.sh"
)
PUBLISH_SH = os.path.join(
    HOME, ".openclaw", "workspace-wp-publisher", "skills", "wordpress", "publish.sh"
)


def _env() -> dict[str, str]:
    env = boot._openclaw_env()
    env["HOME"] = HOME
    env["PYTHONIOENCODING"] = "utf-8"
    py311 = os.path.join(HOME, r"AppData\Local\Programs\Python\Python311")
    if os.path.isdir(py311):
        env["PATH"] = py311 + os.pathsep + env.get("PATH", "")
    return env


def _run(argv: list[str], *, timeout: int, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    print("EXEC:", " ".join(argv), flush=True)
    return subprocess.run(
        argv,
        timeout=timeout,
        env=_env(),
        cwd=cwd,
        text=True,
        capture_output=False,
    )


def _py(script: str, args: list[str], *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return _run([PY, script, *args], timeout=timeout)


def _bash(script: str, extra: list[str] | None = None, *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    bash = boot._git_bash()
    posix = script.replace("\\", "/")
    if os.name == "nt" and posix[1:3] == ":/":
        posix = "/" + posix[0].lower() + posix[2:]
    cmd = f'bash "{posix}"'
    if extra:
        cmd += " " + " ".join(f'"{a}"' if " " in a else a for a in extra)
    return _run([bash, "-lc", cmd], timeout=timeout)


def _size(path: str) -> int:
    return os.path.getsize(path) if os.path.isfile(path) else 0


def _qualification_ready(validated_path: str, qualified_path: str) -> bool:
    """True when qualified_claims.json covers all sourced_facts ids."""
    if _size(qualified_path) < 40 or _size(validated_path) < 40:
        return False
    try:
        with open(validated_path, encoding="utf-8") as f:
            validated = json.load(f)
        with open(qualified_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("qualification_version") != 1:
        return False
    sourced = validated.get("sourced_facts") or []
    expected = [
        str(x.get("id") or "")
        for x in sourced
        if isinstance(x, dict) and str(x.get("id") or "")
    ]
    claims = doc.get("claims") if isinstance(doc.get("claims"), list) else []
    got = [str(c.get("claim_id") or "") for c in claims if isinstance(c, dict)]
    if sorted(expected) != sorted(got):
        return False
    for c in claims:
        if not isinstance(c, dict):
            return False
        if c.get("decision") not in ("VERIFY", "SKIP"):
            return False
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else None
    if not summary:
        return False
    try:
        if int(summary.get("total", -1)) != len(claims):
            return False
        if int(summary.get("verify", -1)) != sum(
            1 for c in claims if c.get("decision") == "VERIFY"
        ):
            return False
        if int(summary.get("skip", -1)) != sum(
            1 for c in claims if c.get("decision") == "SKIP"
        ):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _verification_ready(
    validated_path: str,
    verification_path: str,
    *,
    qualified_path: str | None = None,
) -> bool:
    """True when verification.json covers VERIFY claims from qualification (or all sourced)."""
    if _size(verification_path) < 40 or _size(validated_path) < 40:
        return False
    try:
        with open(validated_path, encoding="utf-8") as f:
            validated = json.load(f)
        with open(verification_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("verification_version") != 1:
        return False

    expected: list[str] = []
    if qualified_path and _size(qualified_path) >= 40:
        try:
            with open(qualified_path, encoding="utf-8") as f:
                qual = json.load(f)
            if isinstance(qual, dict) and qual.get("status") == "ok":
                expected = [
                    str(c.get("claim_id") or "")
                    for c in (qual.get("claims") or [])
                    if isinstance(c, dict)
                    and c.get("decision") == "VERIFY"
                    and c.get("claim_id")
                ]
        except (OSError, json.JSONDecodeError):
            expected = []
    if not expected and not (qualified_path and _size(qualified_path) >= 40):
        sourced = validated.get("sourced_facts") or []
        expected = [
            str(x.get("id") or "")
            for x in sourced
            if isinstance(x, dict) and str(x.get("id") or "")
        ]

    claims = doc.get("claims") if isinstance(doc.get("claims"), list) else []
    got = {str(c.get("claim_id") or "") for c in claims if isinstance(c, dict)}
    if sorted(expected) != sorted(got):
        return False
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else None
    if not summary:
        return False
    try:
        if int(summary.get("total", -1)) != len(claims):
            return False
    except (TypeError, ValueError):
        return False
    return True


def _consistency_ready(
    verification_path: str,
    consistency_path: str,
) -> bool:
    """True when claim_consistency.json is valid for comparable verification claims."""
    if _size(consistency_path) < 40 or _size(verification_path) < 40:
        return False
    try:
        with open(verification_path, encoding="utf-8") as f:
            verification = json.load(f)
        with open(consistency_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(verification, dict) or verification.get("status") != "ok":
        return False
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("consistency_version") != 1:
        return False

    comparable = {
        str(c.get("claim_id") or "")
        for c in (verification.get("claims") or [])
        if isinstance(c, dict)
        and str(c.get("status") or "") in ("DIRECT", "CORROBORATED", "SINGLE_SOURCE")
        and str(c.get("claim_id") or "")
    }
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else None
    issues = doc.get("issues") if isinstance(doc.get("issues"), list) else None
    if summary is None or issues is None:
        return False
    try:
        claims_compared = int(summary.get("claims_compared", -1))
        pairs_checked = int(summary.get("pairs_checked", -1))
        none = int(summary.get("none", -1))
        potential = int(summary.get("potential_conflict", -1))
        likely = int(summary.get("likely_conflict", -1))
    except (TypeError, ValueError):
        return False
    if claims_compared != len(comparable):
        return False
    if none + potential + likely != pairs_checked:
        return False
    if potential + likely != len(issues):
        return False
    for issue in issues:
        if not isinstance(issue, dict):
            return False
        if issue.get("severity") not in ("POTENTIAL_CONFLICT", "LIKELY_CONFLICT"):
            return False
        ids = issue.get("claim_ids")
        if not isinstance(ids, list) or len(ids) != 2:
            return False
    return True


def _article_audit_ready(article_path: str, audit_path: str) -> bool:
    """True when article_audit.json is a valid Phase-4 audit artifact."""
    if _size(audit_path) < 40 or _size(article_path) < 200:
        return False
    try:
        with open(audit_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(doc, dict) or doc.get("status") != "ok":
        return False
    if doc.get("audit_version") != 1:
        return False
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else None
    claims = doc.get("claims") if isinstance(doc.get("claims"), list) else None
    if summary is None or claims is None:
        return False
    try:
        total = int(summary.get("article_claims", -1))
        supported = int(summary.get("supported", -1))
        single = int(summary.get("single_source_refs", -1))
        unmapped = int(summary.get("unmapped", -1))
        num_mm = int(summary.get("number_mismatches", -1))
        year_mm = int(summary.get("year_mismatches", -1))
        conflict = int(summary.get("conflict_refs", -1))
    except (TypeError, ValueError):
        return False
    if total != len(claims):
        return False
    if supported + single + unmapped + num_mm + year_mm + conflict != total:
        return False
    for row in claims:
        if not isinstance(row, dict):
            return False
        if row.get("audit_status") not in (
            "SUPPORTED",
            "UNMAPPED",
            "NUMBER_MISMATCH",
            "YEAR_MISMATCH",
            "CONFLICT_REF",
            "SINGLE_SOURCE_REF",
        ):
            return False
    return True


def _progress(feed_job_id: int, step: str, *, failed: bool = False, note: str = "") -> None:
    try:
        import drain_progress as progress

        progress.notify(int(feed_job_id), step, failed=failed, note=note)
    except Exception as e:
        print(f"PROGRESS_WARN: {e}", file=sys.stderr)


def _fail(feed_job_id: int, project: str, step: str, reason: str) -> int:
    print(f"CONTINUE_FAIL: {reason}", file=sys.stderr)
    try:
        db.mark_feed_job(feed_job_id, "queued")
        db.clear_drainer_lease(project)
    except Exception:
        pass
    _progress(feed_job_id, step, failed=True, note=f"{reason} Worker will retry. Do not tap Run again.")
    return 1


def _record_step(run_dir: str, name: str, seconds: float) -> None:
    path = os.path.join(run_dir, "publish", "steps.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data: dict = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    steps = data.get("steps") if isinstance(data.get("steps"), dict) else {}
    steps[name] = {"duration_seconds": max(0, int(round(float(seconds))))}
    payload = {"steps": steps}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def _polish_article(manifest: str, article_final: str, validated: str) -> int:
    """Autofix + check up to 3 times so a banned phrase does not re-run Scout/Quill."""
    last_rc = 1
    for attempt in range(1, 4):
        _py(
            os.path.join(HERE, "sync_article_from_raw.py"),
            ["--manifest", manifest],
            timeout=60,
        )
        _py(
            os.path.join(WRITER_CHECK, "autofix_article.py"),
            ["--article", article_final, "--research", validated, "--no-footer"],
            timeout=60,
        )
        chk = _py(
            os.path.join(WRITER_CHECK, "check_article.py"),
            ["--article", article_final, "--research", validated, "--post-sync"],
            timeout=60,
        )
        if chk.returncode == 0:
            return 0
        last_rc = chk.returncode or 1
        print(f"ARTICLE_POLISH retry {attempt}/3", flush=True)
    return last_rc


def _spawn_writer(run_dir: str, project: str) -> int:
    posix = boot._posix_run(run_dir)
    cfg = os.path.join(HOME, ".openclaw", "projects", f"{project}.json").replace("\\", "/")
    tmpl = os.path.join(
        HOME, ".openclaw", "workspace-writer", "templates", "COINOGRAPHY_TEMPLATE.md"
    ).replace("\\", "/")
    msg_path = os.path.join(run_dir, "article", "spawn_writer.txt")
    os.makedirs(os.path.dirname(msg_path), exist_ok=True)
    with open(msg_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"PROJECT_CONFIG: {cfg}\n")
        f.write(f"PROJECT_SLUG: {project}\n")
        f.write(f"TEMPLATE_PATH: {tmpl}\n")
        f.write(f"INPUT_FILE: {posix}/research/validated.json\n")
        f.write(f"OUTPUT_FILE: {posix}/article/raw.md\n")
        f.write(f"<research_dump>{posix}/research/validated.json</research_dump>\n")
        f.write(f"Read template for constraints: {tmpl}\n")
        f.write(
            "Follow workspace-writer/SOUL.md: pre-flight then write clean Markdown "
            "to OUTPUT_FILE. Never dump the article in chat. Run autofix + "
            "check_article.py until PASS. No Telegram. Reply SUCCESS after write.\n"
        )
    cmd = boot._openclaw_agent_cmd(
        "agent",
        "--agent",
        "writer",
        "--timeout",
        "900",
        "--session-key",
        f"agent:writer:{os.path.basename(run_dir)}",
        "--message-file",
        msg_path,
    )
    print(f"SPAWN_WRITER {posix}/article/raw.md", flush=True)
    res = subprocess.run(
        cmd,
        timeout=960,
        env=_env(),
        cwd=os.path.dirname(msg_path),
    )
    if _size(os.path.join(run_dir, "article", "raw.md")) < 200:
        print(f"SPAWN_WRITER_EMPTY rc={res.returncode}", file=sys.stderr)
        return res.returncode or 1
    return 0


def _parse_pick_ids(text: str) -> list[int]:
    m = re.search(r"ids=\[([0-9, ]+)\]", text or "")
    if not m:
        return []
    return [int(x.strip()) for x in m.group(1).split(",") if x.strip().isdigit()]


def continue_drain(
    *,
    run_dir: str,
    project: str,
    feed_job_id: int,
    skip_picker: bool = False,
) -> int:
    run_dir = os.path.abspath(run_dir)
    manifest = os.path.join(run_dir, "manifest.json")
    picks = os.path.join(run_dir, "picker", "picks.json")
    picker_in = os.path.join(run_dir, "picker", "picker_input.json")
    raw = os.path.join(run_dir, "research", "raw.json")
    validated = os.path.join(run_dir, "research", "validated.json")
    article_raw = os.path.join(run_dir, "article", "raw.md")
    article_final = os.path.join(run_dir, "article", "final.md")
    feature = os.path.join(run_dir, "media", "feature.jpg")
    run_id = os.path.basename(run_dir).replace(f"{project}-run-", "")

    db.init_db()
    db.mark_feed_job(feed_job_id, "running")
    db.set_drainer_lease(project)
    db.set_state(project, f"feed_run_dir:{feed_job_id}", run_dir)
    print(f"CONTINUE_START job_id={feed_job_id} run_dir={run_dir}", flush=True)
    start_step = "sieve" if (not skip_picker or _size(picker_in) < 80 or _size(picks) < 80) else "research"
    _progress(feed_job_id, start_step)

    if not skip_picker or _size(picker_in) < 80 or _size(picks) < 80:
        print("=== STEP sieve ===", flush=True)
        _progress(feed_job_id, "sieve")
        rc = _py(
            os.path.join(HERE, "bootstrap_feed_drain.py"),
            [
                "--feed-job-id",
                str(feed_job_id),
                "--project",
                project,
                "--run-dir",
                run_dir,
            ]
            + (["--no-spawn-picker"] if _size(picker_in) >= 80 else [])
            + (["--no-spawn-picker"] if skip_picker and _size(picks) >= 80 else []),
            timeout=1200,
        )
        if rc.returncode != 0 and _size(picks) < 80:
            return _fail(feed_job_id, project, "sieve", "Sieve could not classify this headline.")
        if _size(picks) < 80:
            if boot.spawn_picker(run_dir) != 0:
                return _fail(feed_job_id, project, "sieve", "Sieve spawn failed.")

    print("=== STEP validate_picks ===", flush=True)
    vp = subprocess.run(
        [
            PY,
            os.path.join(HERE, "validate_picks.py"),
            "--picks",
            picks,
            "--picker-input",
            picker_in,
            "--pick-run-id",
            f"{run_id}-pick",
            "--pipeline-run-id",
            run_id,
            "--project",
            project,
            "--classify-only",
        ],
        timeout=60,
        env=_env(),
        text=True,
        capture_output=True,
    )
    sys.stdout.write(vp.stdout or "")
    sys.stderr.write(vp.stderr or "")
    if vp.returncode != 0:
        return _fail(feed_job_id, project, "sieve", "Sieve output did not validate.")
    pick_ids = _parse_pick_ids(vp.stdout or "")
    pick_id = pick_ids[0] if pick_ids else None

    have_research = _size(validated) >= 400 or _size(raw) >= 400
    print("=== STEP research ===", flush=True)
    _progress(feed_job_id, "research")
    t0 = time.perf_counter()
    if have_research and _size(validated) >= 400:
        print("RESEARCH_RESUME validated.json already present", flush=True)
    else:
        research_args = [
            "--input",
            picks,
            "--pick-index",
            "1",
            "--output",
            raw,
            "--self-check",
        ]
        rr = _py(RESEARCHER, research_args, timeout=180)
        if rr.returncode != 0:
            print("RESEARCH_RETRY with extra-search", flush=True)
            rr = _py(RESEARCHER, research_args + ["--extra-search"], timeout=300)
        if rr.returncode != 0:
            _record_step(run_dir, "research", time.perf_counter() - t0)
            return _fail(feed_job_id, project, "research", "Scout research failed.")
        vr = _py(
            os.path.join(HERE, "validate_research.py"),
            ["--manifest", manifest, "--picks", picks, "--pick-index", "1"],
            timeout=60,
        )
        if vr.returncode != 0:
            _record_step(run_dir, "research", time.perf_counter() - t0)
            return _fail(feed_job_id, project, "research", "Scout research did not validate.")
    _record_step(run_dir, "research", time.perf_counter() - t0)

    # Phase 1.5 qualify → V1 verify (audit layer; SKIP/SINGLE_SOURCE do not block Quill).
    qualified = os.path.join(run_dir, "research", "qualified_claims.json")
    verification = os.path.join(run_dir, "research", "verification.json")
    sources_dir = os.path.join(run_dir, "research", "sources")

    print("=== STEP qualify_claims ===", flush=True)
    t0 = time.perf_counter()
    if _qualification_ready(validated, qualified):
        print("QUALIFY_RESUME qualified_claims.json already present", flush=True)
    else:
        qq = _py(
            QUALIFY_CLAIMS,
            ["--validated", validated, "--output", qualified],
            timeout=60,
        )
        if qq.returncode != 0 or not _qualification_ready(validated, qualified):
            _record_step(run_dir, "qualify_claims", time.perf_counter() - t0)
            return _fail(
                feed_job_id,
                project,
                "research",
                "Claim qualification failed to produce a valid qualified_claims.json.",
            )
    _record_step(run_dir, "qualify_claims", time.perf_counter() - t0)

    print("=== STEP verify_claims ===", flush=True)
    t0 = time.perf_counter()
    if _verification_ready(validated, verification, qualified_path=qualified):
        print("VERIFY_RESUME verification.json already present", flush=True)
    else:
        vv = _py(
            VERIFY_CLAIMS,
            [
                "--validated",
                validated,
                "--sources-dir",
                sources_dir,
                "--qualified",
                qualified,
                "--output",
                verification,
            ],
            timeout=300,
        )
        if vv.returncode != 0 or not _verification_ready(
            validated, verification, qualified_path=qualified
        ):
            _record_step(run_dir, "verify_claims", time.perf_counter() - t0)
            return _fail(
                feed_job_id,
                project,
                "research",
                "Claim verification failed to produce a valid verification.json.",
            )
    _record_step(run_dir, "verify_claims", time.perf_counter() - t0)

    # Phase 3 cross-claim consistency (audit-only; issues do not block Quill).
    consistency = os.path.join(run_dir, "research", "claim_consistency.json")
    print("=== STEP check_claim_consistency ===", flush=True)
    t0 = time.perf_counter()
    if _consistency_ready(verification, consistency):
        print("CONSISTENCY_RESUME claim_consistency.json already present", flush=True)
    else:
        cc = _py(
            CHECK_CLAIM_CONSISTENCY,
            [
                "--verification",
                verification,
                "--output",
                consistency,
            ],
            timeout=60,
        )
        if cc.returncode != 0 or not _consistency_ready(verification, consistency):
            _record_step(run_dir, "check_claim_consistency", time.perf_counter() - t0)
            return _fail(
                feed_job_id,
                project,
                "research",
                "Claim consistency check failed to produce a valid claim_consistency.json.",
            )
    _record_step(run_dir, "check_claim_consistency", time.perf_counter() - t0)

    print("=== STEP writer ===", flush=True)
    _progress(feed_job_id, "writer")
    t0 = time.perf_counter()
    have_article = _size(article_raw) >= 200 or _size(article_final) >= 200
    if have_article:
        print("WRITER_RESUME article already on disk — polish only", flush=True)
    elif _spawn_writer(run_dir, project) != 0:
        _record_step(run_dir, "writer", time.perf_counter() - t0)
        return _fail(feed_job_id, project, "writer", "Quill could not write the article.")
    if _polish_article(manifest, article_final, validated) != 0:
        _record_step(run_dir, "writer", time.perf_counter() - t0)
        return _fail(feed_job_id, project, "writer", "Article check failed after autofix.")
    _record_step(run_dir, "writer", time.perf_counter() - t0)

    # Phase 4 article evidence audit (audit-only; findings do not block publish).
    article_audit = os.path.join(run_dir, "research", "article_audit.json")
    print("=== STEP audit_article_evidence ===", flush=True)
    t0 = time.perf_counter()
    if _article_audit_ready(article_final, article_audit):
        print("ARTICLE_AUDIT_RESUME article_audit.json already present", flush=True)
    else:
        aa = _py(
            AUDIT_ARTICLE_EVIDENCE,
            [
                "--article",
                article_final,
                "--verification",
                verification,
                "--validated",
                validated,
                "--consistency",
                consistency,
                "--qualified",
                qualified,
                "--output",
                article_audit,
            ],
            timeout=120,
        )
        if aa.returncode != 0 or not _article_audit_ready(article_final, article_audit):
            _record_step(run_dir, "audit_article_evidence", time.perf_counter() - t0)
            return _fail(
                feed_job_id,
                project,
                "writer",
                "Article evidence audit failed to produce a valid article_audit.json.",
            )
    _record_step(run_dir, "audit_article_evidence", time.perf_counter() - t0)

    print("=== STEP image ===", flush=True)
    _progress(feed_job_id, "image")
    t0 = time.perf_counter()
    creator_json = os.path.join(run_dir, "research", "creator_input.json")
    cfg = os.path.join(HOME, ".openclaw", "projects", f"{project}.json")
    bash = boot._git_bash()
    if _size(feature) >= 40960:
        print("IMAGE_RESUME feature.jpg already present", flush=True)
    else:
        ci = subprocess.run(
            [
                PY,
                os.path.join(HERE, "build_creator_input.py"),
                "--research",
                validated,
                "--output",
                creator_json,
                "--project",
                project,
            ],
            timeout=60,
            env=_env(),
            text=True,
            capture_output=True,
        )
        sys.stdout.write(ci.stdout or "")
        sys.stderr.write(ci.stderr or "")
        ctx = ""
        headline = ""
        for line in (ci.stdout or "").splitlines():
            if line.startswith("IMAGE_CONTEXT:"):
                ctx = line.split(":", 1)[1].strip()
            if line.startswith("HEADLINE:"):
                headline = line.split(":", 1)[1].strip()
        source = os.path.join(run_dir, "media", "source.jpg")
        gen_env = _env()
        gen_env["OUTPUT_PATH"] = feature
        gen_env["PROJECT_CONFIG"] = cfg
        gen_env["PROJECT_SLUG"] = project
        gen_env["STAMP_LOGO"] = "1"
        gen_env["IMAGE_HEADLINE"] = headline
        gen_env["IMAGE_CONTEXT"] = ctx
        if _size(source) > 1000:
            gen_env["REFERENCE_IMAGE"] = source
        gen_sh = CREATOR_GEN.replace("\\", "/")
        gen = subprocess.run(
            [
                bash,
                "-lc",
                f'bash "{gen_sh}" "Original editorial photo. Reference the attached story image for mood only."',
            ],
            timeout=180,
            env=gen_env,
        )
        if gen.returncode != 0 or _size(feature) < 40960:
            _record_step(run_dir, "image", time.perf_counter() - t0)
            return _fail(feed_job_id, project, "image", "Pixel could not make the image.")
    _record_step(run_dir, "image", time.perf_counter() - t0)

    print("=== STEP publish ===", flush=True)
    _progress(feed_job_id, "publish")
    t0 = time.perf_counter()
    pub_env = _env()
    pub_env["PROJECT_SLUG"] = project
    pub_env["PROJECT_CONFIG"] = cfg
    pub_env["PIPELINE_MANIFEST"] = manifest.replace("\\", "/")
    pub_env["CRYPTO_RUN_DIR"] = run_dir.replace("\\", "/")
    pub_sh = PUBLISH_SH.replace("\\", "/")
    art = article_final.replace("\\", "/")
    img = feature.replace("\\", "/")
    pub = subprocess.run(
        [
            bash,
            "-lc",
            f'bash "{pub_sh}" --status draft --project {project} --article "{art}" --image "{img}"',
        ],
        timeout=180,
        env=pub_env,
    )
    if pub.returncode != 0:
        _record_step(run_dir, "publish", time.perf_counter() - t0)
        return _fail(feed_job_id, project, "publish", "WordPress draft failed.")
    _record_step(run_dir, "publish", time.perf_counter() - t0)

    print("=== STEP finalize ===", flush=True)
    _progress(feed_job_id, "finalize")
    t0 = time.perf_counter()
    fin_args = [
        "--manifest",
        manifest,
        "--project",
        project,
        "--drive-upload",
    ]
    if pick_id:
        fin_args.extend(["--pick-id", str(pick_id)])
    fin = _py(os.path.join(HERE, "finalize_story.py"), fin_args, timeout=120)
    _record_step(run_dir, "finalize", time.perf_counter() - t0)
    if fin.returncode != 0:
        return _fail(feed_job_id, project, "finalize", "Could not send the Telegram story card.")

    db.clear_drainer_lease(project)
    _progress(feed_job_id, "done")
    print(f"CONTINUE_OK job_id={feed_job_id} run_dir={run_dir}", flush=True)
    return 0



def main() -> int:
    p = argparse.ArgumentParser(description="Local FEED_DRAIN continue after Sieve")
    p.add_argument("--project", default="coinnetwork")
    p.add_argument("--feed-job-id", type=int, required=True)
    p.add_argument("--run-dir", default="")
    p.add_argument("--skip-picker", action="store_true")
    args = p.parse_args()
    run_dir = (args.run_dir or "").strip()
    if not run_dir:
        run_dir = boot._run_dir_from_env(args.project)
    if not run_dir or not os.path.isdir(run_dir):
        print("CONTINUE_FAIL: no run_dir", file=sys.stderr)
        return 2
    try:
        return continue_drain(
            run_dir=run_dir,
            project=args.project,
            feed_job_id=args.feed_job_id,
            skip_picker=args.skip_picker,
        )
    except Exception as e:
        print(f"CONTINUE_EXCEPTION: {e}", file=sys.stderr)
        _fail(args.feed_job_id, args.project, "sieve", f"Worker crashed: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
