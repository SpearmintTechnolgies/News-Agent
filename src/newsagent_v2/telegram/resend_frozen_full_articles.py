"""Send FULL frozen articles for the existing Top-5 batch. ZERO Kimi/Vertex/WP."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

BATCH_ID = "v4f-20260917T105137Z"
# Rank order from the completed final-pipeline report.
ORDER = [
    ("event-016", 1),
    ("event-020", 2),
    ("event-032", 3),
    ("event-011", 4),
    ("event-022", 5),
]


def main() -> int:
    load_dotenv(REPO / ".env")
    from newsagent_v2.approval.store import ApprovalStore
    from newsagent_v2.control.__main__ import _load_environ
    from newsagent_v2.telegram.client import TelegramTestClient
    from newsagent_v2.telegram.config import load_telegram_config
    from newsagent_v2.telegram.full_article_delivery import send_full_frozen_article
    from newsagent_v2.telegram.live_http import build_live_transport

    environ = _load_environ()
    config = load_telegram_config(environ)
    client = TelegramTestClient(
        config,
        transport=build_live_transport(config),
        live_send_enabled=True,
    )
    store = ApprovalStore()
    total = len(ORDER)
    results = []
    for event_id, rank in ORDER:
        story = store.read_story(BATCH_ID, event_id)
        if not story:
            results.append({"ok": False, "event_id": event_id, "reason": "missing_story"})
            continue
        article = story.get("article") if isinstance(story.get("article"), dict) else {}
        body = str(article.get("article_body") or "")
        expected = str(story.get("canonical_body_hash") or story.get("article_sha256") or "")
        out = send_full_frozen_article(
            client=client,
            chat_id=config.test_chat_id,
            batch_id=BATCH_ID,
            event_id=event_id,
            rank=rank,
            total=total,
            headline=str(story.get("headline") or article.get("headline") or ""),
            body=body,
            article_type=story.get("article_type_label") or story.get("article_type"),
            expected_hash=expected or None,
            words=story.get("body_words") or story.get("canonical_body_words"),
        )
        if out.get("ok"):
            # Preserve AWAITING_APPROVAL; record full-article delivery metadata only.
            updated = dict(story)
            updated["telegram_image_message_id"] = story.get("telegram_message_id") or story.get(
                "telegram_image_message_id"
            )
            updated["telegram_full_article_message_id"] = out.get("telegram_message_id")
            updated["telegram_full_article_parts"] = out.get("parts")
            updated["telegram_full_article_plain"] = out.get("last_part_plain")
            updated["telegram_message_id"] = out.get("telegram_message_id")
            updated["full_article_sent"] = True
            updated["canonical_body_hash"] = out.get("article_hash") or expected
            store.write_story(BATCH_ID, event_id, updated)
        results.append(out)

    summary = {
        "batch_id": BATCH_ID,
        "existing_bundles_reused": all(r.get("ok") for r in results) and len(results) == 5,
        "full_articles_sent": sum(1 for r in results if r.get("ok")),
        "kimi_calls": 0,
        "vertex_calls": 0,
        "wordpress_calls": 0,
        "results": [
            {
                "event_id": r.get("event_id"),
                "ok": r.get("ok"),
                "message_id": r.get("telegram_message_id"),
                "parts": r.get("parts"),
                "article_hash": r.get("article_hash"),
                "reason": r.get("reason"),
            }
            for r in results
        ],
    }
    out_path = REPO / "output" / "capability_tests" / BATCH_ID / "full_article_resend.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if summary["full_articles_sent"] == 5 else 1


if __name__ == "__main__":
    raise SystemExit(main())
