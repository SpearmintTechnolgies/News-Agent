"""Probe Groq request shape after research."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from newsagent_v2.article.writer.v4.compile import compile_v4_article
from newsagent_v2.article.writer.v4.writer import build_v4_writer
from newsagent_v2.discovery.event_clusterer import EventReport, NewsEvent

FACT_A = "Northwind Payments disclosed that 12400 customer records were exposed."
FACT_B = "A spoofed government-domain email reached company staff."
FACT_C = "Security staff began notifying affected users after the disclosure."
HTML = f"""<html><body><article><p>{FACT_A}</p><p>{FACT_B}</p><p>{FACT_C}</p>
<p>The company described the incident as limited to retail payments customer files.</p>
<p>Investigators confirmed identity documents and payment history were involved.</p>
</article></body></html>""".encode()

captured: dict = {}


def fake_fetch(url: str):
    return 200, "text/html; charset=utf-8", HTML, url


def fake_groq(url, *, headers=None, json=None, timeout=None):
    captured["body"] = json

    class R:
        status_code = 200
        headers = {}

        def json(self):
            return {
                "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                "usage": {},
            }

    return R()


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    event = NewsEvent(
        event_id="e1",
        canonical_title="Northwind",
        topic="security",
        entities=frozenset({"Northwind Payments"}),
        reports=[
            EventReport(
                report_id="r1",
                source="TestWire",
                source_id="tw",
                source_authority=0.95,
                headline="H",
                url="https://example.test/a",
                published_at=now,
                retrieved_at=now,
                description=f"{FACT_A} {FACT_B} {FACT_C}",
                entities=["Northwind Payments"],
                raw_item_id="x",
            )
        ],
    )
    evidence = [
        {
            "source": r.source,
            "source_id": r.source_id,
            "source_authority": r.source_authority,
            "url": r.url,
            "title": r.headline,
            "published": r.published_at,
            "summary": r.description,
        }
        for r in event.reports
    ]
    story = {
        "event_id": event.event_id,
        "representative_title": event.canonical_title,
        "article_input": {
            "event_id": event.event_id,
            "representative_title": event.canonical_title,
            "evidence": evidence,
        },
        "evidence": evidence,
    }
    with (
        patch("newsagent_v2.article.enrich.default_fetch", fake_fetch),
        patch("newsagent_v2.providers.groq_editorial._default_http_post", fake_groq),
    ):
        writer = build_v4_writer(
            environ={"GROQ_API_KEY": "x"}, enable_failover=False, max_calls=4
        )
        compile_v4_article(
            story,
            writer=writer,
            attempts_root=Path("output/_probe"),
            rank=1,
            research=True,
        )

    body = captured.get("body") or {}
    for m in body.get("messages") or []:
        if m.get("role") != "user":
            continue
        c = m.get("content")
        if not isinstance(c, str):
            continue
        try:
            u = json.loads(c)
        except json.JSONDecodeError:
            print(c[:2000])
            return
        Path("output/_probe_user_packet.json").parent.mkdir(parents=True, exist_ok=True)
        Path("output/_probe_user_packet.json").write_text(
            json.dumps(u, indent=2), encoding="utf-8"
        )
        print("keys", list(u.keys()))
        facts = u.get("authorized_facts") or []
        print("fact count", len(facts))
        for f in facts[:12]:
            print("-", f if isinstance(f, str) else json.dumps(f)[:200])


if __name__ == "__main__":
    main()


