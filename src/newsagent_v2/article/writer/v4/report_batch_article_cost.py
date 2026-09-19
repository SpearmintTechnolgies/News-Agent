"""Report Kimi article cost for an existing batch from persisted telemetry only."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO / "src"))

BATCH_ID = "v4f-20260917T105137Z"
ATTEMPTS = REPO / "output" / "make_runs" / BATCH_ID / "attempts"
APPROVAL = REPO / "output" / "approval" / BATCH_ID / "stories"
OUT = REPO / "output" / "capability_tests" / BATCH_ID / "article_cost_report.json"


def main() -> int:
    from newsagent_v2.article.writer.v4.article_cost_telemetry import (
        format_article_cost_block,
        report_batch_from_attempts_root,
    )
    from newsagent_v2.control.__main__ import _load_environ

    environ = _load_environ()
    publishable = set()
    if APPROVAL.is_dir():
        for path in APPROVAL.glob("*.json"):
            publishable.add(path.stem)

    report = report_batch_from_attempts_root(
        ATTEMPTS,
        batch_id=BATCH_ID,
        environ=environ,
        publishable_event_ids=publishable or None,
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")

    print("BATCH ARTICLE COST")
    for key in (
        "batch_id",
        "articles_generated",
        "publishable_articles",
        "kimi_calls",
        "kimi_input_tokens",
        "kimi_output_tokens",
        "kimi_total_tokens",
        "successful_article_tokens",
        "failed_candidate_tokens",
        "successful_article_cost_usd",
        "successful_article_cost_inr",
        "failed_candidate_cost_usd",
        "failed_candidate_cost_inr",
        "total_article_generation_cost_usd",
        "total_article_generation_cost_inr",
        "effective_cost_per_publishable_article_usd",
        "effective_cost_per_publishable_article_inr",
        "pricing_verified",
        "pricing_source",
    ):
        print(f"{key}={report.get(key)}")
    print()
    for row in report.get("articles") or []:
        print(format_article_cost_block(row))
        print()
    print(f"report_path={OUT}")
    print("kimi_calls_caused_by_cost_reporting=0")
    print("vertex_calls_caused_by_cost_reporting=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
