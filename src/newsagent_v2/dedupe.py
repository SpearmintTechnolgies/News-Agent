from __future__ import annotations
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit
from .models import NewsItem

STOP = {
    "the","a","an","to","of","in","on","for","and","or","is","are","as","at","by",
    "with","from","after","amid","over","into","its","it"
}

def normalize_title(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    words = [w for w in words if w not in STOP]
    return " ".join(words)

def canonical_url(url: str) -> str:
    p = urlsplit(url)
    clean = urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", ""))
    return clean

def fingerprint(item: NewsItem) -> str:
    base = normalize_title(item.title)
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    seen_urls: set[str] = set()
    seen_fp: set[str] = set()
    out: list[NewsItem] = []

    for item in items:
        item.fingerprint = fingerprint(item)
        cu = canonical_url(item.url)

        if cu in seen_urls or item.fingerprint in seen_fp:
            continue

        seen_urls.add(cu)
        seen_fp.add(item.fingerprint)
        out.append(item)

    return out
