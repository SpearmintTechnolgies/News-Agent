#!/usr/bin/env python3
"""page_fetch.py — retrieve page HTML via Playwright (headless Chromium)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class PageFetchResult:
    html: str
    status: int
    final_url: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def page_fetch(
    url: str,
    *,
    timeout_ms: int = 30_000,
    wait_until: str = "domcontentloaded",
) -> PageFetchResult:
    from playwright.sync_api import sync_playwright

    url = url.strip()
    if not url:
        raise ValueError("url is required")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            html = page.content()
            final_url = page.url
            status = response.status if response else 0
        finally:
            browser.close()

    return PageFetchResult(html=html, status=status, final_url=final_url)
