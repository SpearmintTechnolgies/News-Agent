from __future__ import annotations

import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser


def parse_date(entry):
    raw = entry.get("published") or entry.get("updated") or ""
    if not raw:
        return None

    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def probe(name: str, url: str) -> None:
    print("=" * 72)
    print(f"SOURCE: {name}")
    print(f"URL:    {url}")

    try:
        parsed = feedparser.parse(url)
    except Exception as exc:
        print(f"STATUS: ERROR - {type(exc).__name__}: {exc}")
        return

    entries = list(parsed.entries)

    print(f"ENTRIES: {len(entries)}")
    print(f"BOZO:    {bool(getattr(parsed, 'bozo', False))}")

    if getattr(parsed, "bozo", False):
        exc = getattr(parsed, "bozo_exception", None)
        if exc:
            print(f"PARSER:  {type(exc).__name__}: {exc}")

    if not entries:
        print("STATUS:  FAILED/EMPTY")
        return

    dated = []

    for entry in entries:
        dt = parse_date(entry)
        if dt:
            dated.append(dt)

    if dated:
        newest = max(dated)
        age = datetime.now(timezone.utc) - newest
        print(f"NEWEST:  {newest.isoformat()}")
        print(f"AGE_H:   {age.total_seconds() / 3600:.1f}")
    else:
        print("NEWEST:  UNKNOWN")

    print("STATUS:  FEED RETURNED ENTRIES")
    print()
    print("SAMPLE TITLES:")

    for entry in entries[:3]:
        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()
        print(f" - {title}")
        print(f"   {link}")


def main():
    if len(sys.argv) != 3:
        print('Usage: python feed_probe.py "Source Name" "https://feed-url"')
        raise SystemExit(2)

    probe(sys.argv[1], sys.argv[2])


if __name__ == "__main__":
    main()
