"""Page fetching: plain HTTP first, headless Edge for blocked or JS-only pages."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)
HTTP_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
HTTP_TIMEOUT_SECONDS = 15.0
BROWSER_TIMEOUT_MS = 25_000
MAX_HTML_CHARS = 3_000_000


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    html: str
    method: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.html)


FetchFn = Callable[[str], FetchResult]


def http_fetch(url: str, client: httpx.Client | None = None) -> FetchResult:
    own = client is None
    session = client or httpx.Client(
        headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True
    )
    try:
        resp = session.get(url)
        ctype = resp.headers.get("content-type", "")
        html = resp.text[:MAX_HTML_CHARS] if ("html" in ctype or "xml" in ctype or not ctype) else ""
        return FetchResult(url, str(resp.url), resp.status_code, html, "http")
    except httpx.HTTPError as exc:
        return FetchResult(url, url, 0, "", "http", error=type(exc).__name__)
    finally:
        if own:
            session.close()


class BrowserFetcher:
    """Lazy headless browser. Uses the system Edge install (no download).

    Playwright's sync API is bound to the thread that started it, so one
    instance must stay on one thread and be closed there.
    """

    def __init__(self, channel: str | None = None) -> None:
        self.channel = channel or os.environ.get("NEWSAGENT_RESEARCH_BROWSER_CHANNEL", "msedge")
        self._pw = None
        self._browser = None
        self._context = None
        self.disabled_reason = ""

    def _ensure(self) -> bool:
        if self._context is not None:
            return True
        if self.disabled_reason:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(channel=self.channel, headless=True)
            self._context = self._browser.new_context(
                user_agent=BROWSER_UA, locale="en-US", java_script_enabled=True
            )
            self._context.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_(),
            )
            return True
        except Exception as exc:
            self.disabled_reason = f"{type(exc).__name__}: {exc}"[:300]
            logger.warning("[RESEARCH] browser unavailable: %s", self.disabled_reason)
            self.close()
            return False

    def fetch(self, url: str) -> FetchResult:
        if not self._ensure():
            return FetchResult(url, url, 0, "", "browser", error=self.disabled_reason)
        page = self._context.new_page()
        try:
            resp = page.goto(url, timeout=BROWSER_TIMEOUT_MS, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("networkidle", timeout=6_000)
            except Exception:
                pass
            status = resp.status if resp else 0
            return FetchResult(url, page.url, status, page.content()[:MAX_HTML_CHARS], "browser")
        except Exception as exc:
            return FetchResult(url, url, 0, "", "browser", error=type(exc).__name__)
        finally:
            page.close()

    def close(self) -> None:
        for obj, meth in ((self._context, "close"), (self._browser, "close"), (self._pw, "stop")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:
                    pass
        self._context = self._browser = self._pw = None
