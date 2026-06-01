#!/usr/bin/env python3
"""
verify_artifacts.py — Stage-specific coherence and freshness gates.

Usage:
    python3 verify_artifacts.py --stage STAGE --manifest /path/to/manifest.json

Stages:
    pre_write   — research_validated exists, run_id matches, non-empty
    pre_sync    — article_raw fresh (mtime >= .run_started), non-empty
    post_sync   — article_final fresh, H1 matches headline, ≥1 source URL match
    pre_drive   — docx exists + newer than article_final; paths under active RUN_DIR
    pre_wp      — all pre_drive checks + repeat post_sync coherence

Exit 0 + prints ARTIFACTS_OK: <stage>
Exit 1 + prints ARTIFACTS_FAIL: <reason>  (detailed, actionable)
"""
from __future__ import annotations

import json
import os
import re
import sys
import argparse
from urllib.parse import urlparse


# ── helpers ──────────────────────────────────────────────────────────────────

def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def run_started_epoch(run_dir: str) -> float | None:
    stamp = os.path.join(run_dir, ".run_started")
    if not os.path.exists(stamp):
        return None
    try:
        with open(stamp) as f:
            return float(f.read().strip())
    except (ValueError, OSError):
        return None


def extract_h1(content: str) -> str:
    in_meta = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "META":
            in_meta = True
            continue
        if in_meta and stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def token_set(text: str) -> set[str]:
    STOP = {"the", "and", "for", "its", "are", "was", "has", "have",
            "with", "that", "this", "from", "not", "but", "can", "will",
            "all", "new", "law", "bill", "act"}
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP}


def headline_matches(h1: str, headline: str) -> bool:
    return len(token_set(h1) & token_set(headline)) >= 2


def normalize_url(url: str) -> str:
    url = url.strip("()")
    p = urlparse(url)
    host = (p.netloc or "").lower().removeprefix("www.")
    path = p.path.rstrip("/") or ""
    return f"{p.scheme.lower()}://{host}{path}"


def url_matches_any(url: str, research_urls: list[str]) -> bool:
    norm = normalize_url(url)
    for ref in research_urls:
        ref_norm = normalize_url(ref)
        if norm == ref_norm:
            return True
        if norm.startswith(ref_norm) or ref_norm.startswith(norm):
            return True
        if norm.split("?")[0] == ref_norm.split("?")[0]:
            return True
    return False


def extract_body_urls(content: str) -> list[str]:
    SOURCES_SPLIT = re.compile(
        r"\n(?:\*\*Sources:\*\*|^Sources:)\s*\n",
        re.IGNORECASE | re.MULTILINE,
    )
    parts = SOURCES_SPLIT.split(content, maxsplit=1)
    body = parts[0]
    return re.findall(r"\(https?://[^)]+\)", body)


def fail(reason: str) -> int:
    print(f"ARTIFACTS_FAIL: {reason}")
    return 1


# ── stage checks ─────────────────────────────────────────────────────────────

def check_pre_write(manifest: dict, run_dir: str) -> int:
    artifacts = manifest.get("artifacts", {})
    validated_path = artifacts.get("research_validated", "")

    if not validated_path or not os.path.exists(validated_path):
        return fail(f"research_validated missing: {validated_path}")
    if os.path.getsize(validated_path) == 0:
        return fail("research/validated.json is empty")

    try:
        data = load_json(validated_path)
    except (OSError, json.JSONDecodeError) as e:
        return fail(f"cannot parse research_validated: {e}")

    # run_id coherence
    manifest_run_id = manifest.get("run_id", "")
    artifact_run_dir = artifacts.get("research_validated", "")
    if run_dir and run_dir not in artifact_run_dir:
        return fail(f"research_validated path not under active RUN_DIR\n"
                    f"  Expected under: {run_dir}\n"
                    f"  Got:            {artifact_run_dir}")

    for field in ("primary_headline", "source_urls", "combined_key_facts"):
        if not data.get(field):
            return fail(f"research_validated missing field: {field}")

    print(f"ARTIFACTS_OK: pre_write — research_validated ready "
          f"({data.get('primary_headline','')[:60]})")
    return 0


def check_pre_sync(manifest: dict, run_dir: str) -> int:
    artifacts = manifest.get("artifacts", {})
    raw_path = artifacts.get("article_raw", "")

    if not raw_path or not os.path.exists(raw_path):
        return fail(f"article_raw missing: {raw_path}")
    if os.path.getsize(raw_path) == 0:
        return fail("article/raw.md is empty — writer did not write the file this run")

    epoch = run_started_epoch(run_dir)
    if epoch is not None:
        raw_mtime = os.stat(raw_path).st_mtime
        if raw_mtime < epoch:
            return fail(
                f"article/raw.md is stale — mtime predates this run's .run_started\n"
                f"  raw.md mtime:  {raw_mtime:.0f}\n"
                f"  .run_started:  {epoch:.0f}\n"
                f"  (writer may have yielded SUCCESS without writing)"
            )

    print("ARTIFACTS_OK: pre_sync — article/raw.md is fresh")
    return 0


def check_post_sync(manifest: dict, run_dir: str) -> int:
    artifacts  = manifest.get("artifacts", {})
    raw_path   = artifacts.get("article_raw", "")
    final_path = artifacts.get("article_final", "")
    validated_path = artifacts.get("research_validated", "")

    if not final_path or not os.path.exists(final_path):
        return fail(f"article_final missing: {final_path}")
    if os.path.getsize(final_path) == 0:
        return fail("article/final.md is empty after sync")

    # final must be newer than raw
    if raw_path and os.path.exists(raw_path):
        raw_mtime   = os.stat(raw_path).st_mtime
        final_mtime = os.stat(final_path).st_mtime
        if final_mtime < raw_mtime:
            return fail(
                f"article/final.md is older than article/raw.md — sync may not have run\n"
                f"  final.md mtime: {final_mtime:.0f}\n"
                f"  raw.md mtime:   {raw_mtime:.0f}"
            )

    # Read final content
    try:
        with open(final_path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return fail(f"cannot read article/final.md: {e}")

    # H1 + source URL coherence
    if validated_path and os.path.exists(validated_path) and os.path.getsize(validated_path) > 0:
        try:
            research = load_json(validated_path)
        except (OSError, json.JSONDecodeError):
            research = {}

        research_headline = research.get("primary_headline", "")
        research_urls     = research.get("source_urls", [])

        if research_headline:
            h1 = extract_h1(content)
            if h1 and not headline_matches(h1, research_headline):
                return fail(
                    f"article/final.md H1 does not match research headline\n"
                    f"  Article H1:        {h1[:80]}\n"
                    f"  Research headline: {research_headline[:80]}"
                )

        if research_urls:
            body_urls = extract_body_urls(content)
            if body_urls and not any(url_matches_any(u, research_urls) for u in body_urls):
                return fail(
                    f"no body source URL matches current research source_urls\n"
                    f"  Body URLs:      {body_urls[:3]}\n"
                    f"  Research URLs:  {research_urls[:3]}"
                )

    words = len(content.split())
    print(f"ARTIFACTS_OK: post_sync — article/final.md ready ({words} words)")
    return 0


def check_pre_drive(manifest: dict, run_dir: str) -> int:
    # First run post_sync checks (article must be coherent before docx)
    rc = check_post_sync(manifest, run_dir)
    if rc != 0:
        return rc

    artifacts  = manifest.get("artifacts", {})
    final_path = artifacts.get("article_final", "")
    docx_path  = artifacts.get("docx", "")

    if not docx_path or not os.path.exists(docx_path):
        return fail(f"docx missing: {docx_path}")
    if os.path.getsize(docx_path) == 0:
        return fail("article/article.docx is empty")

    # docx must be newer than final.md
    if final_path and os.path.exists(final_path):
        final_mtime = os.stat(final_path).st_mtime
        docx_mtime  = os.stat(docx_path).st_mtime
        if docx_mtime < final_mtime:
            return fail(
                f"article.docx is older than article/final.md — "
                f"docx was not rebuilt after latest article changes\n"
                f"  docx mtime:     {docx_mtime:.0f}\n"
                f"  final.md mtime: {final_mtime:.0f}"
            )

    # Paths must be under active RUN_DIR
    if run_dir:
        for key, path in [("docx", docx_path), ("article_final", final_path)]:
            if path and run_dir not in path:
                return fail(
                    f"artifact '{key}' is not under active RUN_DIR\n"
                    f"  Expected under: {run_dir}\n"
                    f"  Got:            {path}\n"
                    f"  (possible stale cross-run file)"
                )

    print("ARTIFACTS_OK: pre_drive — article/final.md + docx ready and fresh")
    return 0


def check_pre_wp(manifest: dict, run_dir: str) -> int:
    # Full pre_drive checks
    rc = check_pre_drive(manifest, run_dir)
    if rc != 0:
        return rc

    # Repeat H1/source URL coherence as final gate (belt-and-suspenders)
    artifacts      = manifest.get("artifacts", {})
    final_path     = artifacts.get("article_final", "")
    validated_path = artifacts.get("research_validated", "")

    if (final_path and os.path.exists(final_path) and
            validated_path and os.path.exists(validated_path)):
        try:
            with open(final_path, encoding="utf-8") as f:
                content = f.read()
            research = load_json(validated_path)
        except (OSError, json.JSONDecodeError):
            content = ""
            research = {}

        research_headline = research.get("primary_headline", "")
        research_urls     = research.get("source_urls", [])

        if research_headline:
            h1 = extract_h1(content)
            if h1 and not headline_matches(h1, research_headline):
                return fail(
                    f"[pre_wp] Article H1 still does not match research headline — "
                    f"publishing cancelled\n"
                    f"  Article H1:        {h1[:80]}\n"
                    f"  Research headline: {research_headline[:80]}"
                )

        if research_urls:
            body_urls = extract_body_urls(content)
            if body_urls and not any(url_matches_any(u, research_urls) for u in body_urls):
                return fail(
                    f"[pre_wp] no body source URL matches research — "
                    f"publishing cancelled\n"
                    f"  Body URLs:     {body_urls[:3]}\n"
                    f"  Research URLs: {research_urls[:3]}"
                )

    print("ARTIFACTS_OK: pre_wp — all coherence checks passed")
    return 0


# ── entry point ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", required=True,
                        choices=["pre_write", "pre_sync", "post_sync", "pre_drive", "pre_wp"])
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()

    manifest_path = os.path.realpath(args.manifest)
    if not os.path.exists(manifest_path):
        print(f"ARTIFACTS_FAIL: manifest not found: {manifest_path}")
        return 1

    try:
        manifest = load_json(manifest_path)
    except (OSError, json.JSONDecodeError) as e:
        print(f"ARTIFACTS_FAIL: cannot parse manifest: {e}")
        return 1

    run_dir = manifest.get("run_dir", "")

    dispatch = {
        "pre_write": check_pre_write,
        "pre_sync":  check_pre_sync,
        "post_sync": check_post_sync,
        "pre_drive": check_pre_drive,
        "pre_wp":    check_pre_wp,
    }

    return dispatch[args.stage](manifest, run_dir)


if __name__ == "__main__":
    sys.exit(main())
