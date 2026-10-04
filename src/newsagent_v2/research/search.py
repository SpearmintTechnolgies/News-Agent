"""Free news search: Bing News RSS and Google News RSS."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, quote_plus, urlparse

import feedparser
import httpx

from newsagent_v2.research.fetch import HTTP_HEADERS

logger = logging.getLogger(__name__)

_GN_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_GN_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_GN_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


@dataclass
class SearchHit:
    url: str
    title: str
    publisher: str
    published_at: str
    engine: str

    def age_hours(self, now: datetime | None = None) -> float | None:
        if not self.published_at:
            return None
        try:
            ts = datetime.fromisoformat(self.published_at)
        except ValueError:
            return None
        ref = now or datetime.now(timezone.utc)
        return (ref - ts).total_seconds() / 3600.0


def _rss_date(entry: dict) -> str:
    raw = entry.get("published") or entry.get("updated") or ""
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def bing_news(query: str, client: httpx.Client, limit: int = 10) -> list[SearchHit]:
    url = f"https://www.bing.com/news/search?q={quote_plus(query)}&format=rss&setlang=en-US"
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        logger.info("[RESEARCH] bing search failed: %s", type(exc).__name__)
        return []
    hits: list[SearchHit] = []
    for entry in feedparser.parse(resp.text).entries[:limit]:
        link = str(entry.get("link") or "")
        target = parse_qs(urlparse(link).query).get("url", [""])[0] or link
        if not target.startswith("http"):
            continue
        hits.append(
            SearchHit(
                url=target,
                title=str(entry.get("title") or ""),
                publisher=str(entry.get("news_source") or entry.get("source", {}).get("title", "") or ""),
                published_at=_rss_date(entry),
                engine="bing_news",
            )
        )
    return hits


def _decode_google_link(link: str, client: httpx.Client) -> str:
    gid = urlparse(link).path.rstrip("/").split("/")[-1]
    if not gid:
        return ""
    try:
        page = client.get(f"https://news.google.com/rss/articles/{gid}").text
        sg, ts = _GN_SG_RE.search(page), _GN_TS_RE.search(page)
        if not (sg and ts):
            return ""
        inner = json.dumps(
            [
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                 "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                gid,
                int(ts.group(1)),
                sg.group(1),
            ]
        )
        resp = client.post(
            _GN_BATCH_URL,
            data={"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        payload = json.loads(resp.text.split("\n\n")[1])[:-2]
        decoded = json.loads(payload[0][2])[1]
        return decoded if isinstance(decoded, str) and decoded.startswith("http") else ""
    except (httpx.HTTPError, ValueError, IndexError, TypeError):
        return ""


def google_news(
    query: str,
    client: httpx.Client,
    limit: int = 8,
    window_days: int = 3,
) -> list[SearchHit]:
    url = (
        f"https://news.google.com/rss/search?q={quote_plus(query)}+when:{window_days}d"
        "&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = client.get(url)
    except httpx.HTTPError as exc:
        logger.info("[RESEARCH] google news search failed: %s", type(exc).__name__)
        return []
    hits: list[SearchHit] = []
    for entry in feedparser.parse(resp.text).entries[:limit]:
        target = _decode_google_link(str(entry.get("link") or ""), client)
        if not target:
            continue
        title = str(entry.get("title") or "")
        publisher = str((entry.get("source") or {}).get("title") or "")
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        hits.append(SearchHit(target, title, publisher, _rss_date(entry), "google_news"))
    return hits


def search_news(queries: list[str], *, per_query: int = 8, timeout: float = 15.0) -> list[SearchHit]:
    hits: list[SearchHit] = []
    seen: set[str] = set()
    with httpx.Client(headers=HTTP_HEADERS, timeout=timeout, follow_redirects=True) as client:
        for query in queries:
            for engine in (bing_news, google_news):
                for hit in engine(query, client, per_query):
                    key = hit.url.split("#")[0].split("?")[0].rstrip("/").lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append(hit)
    return hits
