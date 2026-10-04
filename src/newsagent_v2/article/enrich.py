"""
Deterministic HTTP evidence enrichment.

Fetches selected Top-5 source pages and extracts research-only factual
snippets. Does not call an LLM. Source text is not article copy.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from newsagent_v2.article.extract_clean import clean_extracted_article_text
from newsagent_v2.article.qa.textutil import NUMBER_TOKEN_RE, split_sentences, word_count

FetchFn = Callable[[str], tuple[int, str, bytes, str]]

USER_AGENT = "NewsAgentV2-evidence/1.0"
MAX_HTML_BYTES = 1_500_000
FETCH_TIMEOUT_SECONDS = 20
MAX_SOURCES_PER_EVENT = 2
MAX_SNIPPETS_PER_SOURCE = 12
MAX_EXTRACTED_CHARS = 3500
MIN_SNIPPET_WORDS = 8
MIN_DISTINCT_FACTS = 8
MIN_EXTRACTED_WORDS = 80
BLOCKED_STATUS_CODES = frozenset({401, 403, 429})
TIME_RE = re.compile(
    r"\b(20\d{2}|january|february|march|april|may|june|july|august|"
    r"september|october|november|december|monday|tuesday|wednesday|"
    r"thursday|friday|saturday|sunday|yesterday|today|overnight|"
    r"q[1-4]|utc|\d{1,2}:\d{2})\b",
    re.IGNORECASE,
)
ENTITY_RE = re.compile(r"\b([A-Z][A-Za-z0-9&.-]*(?:\s+[A-Z][A-Za-z0-9&.-]*)+)\b")
EXPLOIT_RE = re.compile(r"\b(hack|exploit|stolen|bounty|minted|bridge|drained)\b", re.I)
MARKET_RE = re.compile(r"\b(etf|inflow|outflow|price|billion|million|volume|flow)\b", re.I)
REGULATORY_RE = re.compile(r"\b(regulator|sec|cftc|wto|law|rule|license|directive)\b", re.I)
STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
STATUS_SUFFICIENT = "SUFFICIENT_EVIDENCE"

SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "nav",
        "footer",
        "aside",
        "noscript",
        "iframe",
        "form",
        "svg",
        "button",
        "input",
    }
)
BOILERPLATE_RE = re.compile(
    r"(cookie|subscribe|newsletter|sign[\s-]?up|related stories|"
    r"advert|advertisement|promo|paywall|privacy policy|terms of service|"
    r"share this|follow us|all rights reserved|enable javascript|"
    r"continue reading|log in to comment|"
    r"espa[nñ]ol\s+sections|"
    r"\b(bitcoin|defi|ethereum|nfts?)\b.{0,40}\b(regulation|web3|business|ecosystem)\b|"
    r"skip to (main )?content|menu\s+close|accept (all )?cookies)",
    re.IGNORECASE,
)
_META_DESC_RE = re.compile(
    r'<meta\b[^>]*\b(?:name|property)\s*=\s*["\'](?:og:description|twitter:description|description)["\'][^>]*>',
    re.IGNORECASE,
)
_META_CONTENT_RE = re.compile(
    r'\bcontent\s*=\s*["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_NAV_CHROME_RE = re.compile(
    r"("
    r"espa[nñ]ol\s+sections|"
    r"^(?:home|news|markets|videos?|podcasts?)\s+(?:home|news|markets)|"
    r"\bsections?\b.{0,20}\b(?:bitcoin|defi|ethereum|nfts?|web3)\b"
    r")",
    re.IGNORECASE,
)
ATTRIBUTION_RE = re.compile(
    r"\b(said|says|according to|reported|announced|stated|told|wrote|confirmed)\b",
    re.IGNORECASE,
)
PAYWALL_RE = re.compile(
    r"(subscribe to continue|this article is for subscribers|paywall|"
    r"please log in to read|become a subscriber)",
    re.IGNORECASE,
)
ARTICLE_LD_TYPES = frozenset(
    {"newsarticle", "article", "blogposting", "reportagenewsarticle"}
)


class _TextCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_stack: list[str] = []
        self._chunks: list[str] = []
        self._ld: list[str] = []
        self._in_ld = False
        self._ld_buf = ""
        self.title: str | None = None
        self._in_title = False
        self._title_buf = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        mapping = {str(k).lower(): (v or "") for k, v in attrs}
        cls_id = f"{mapping.get('class', '')} {mapping.get('id', '')}"
        if tag in SKIP_TAGS or BOILERPLATE_RE.search(cls_id):
            self._skip_stack.append(tag)
            if tag == "script" and "ld+json" in (mapping.get("type") or "").lower():
                self._in_ld = True
                self._ld_buf = ""
            return
        if tag == "script" and "ld+json" in (mapping.get("type") or "").lower():
            self._in_ld = True
            self._ld_buf = ""
            self._skip_stack.append(tag)
            return
        if tag == "title":
            self._in_title = True
            self._title_buf = ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld:
            self._in_ld = False
            if self._ld_buf.strip():
                self._ld.append(self._ld_buf)
            self._ld_buf = ""
        if tag == "title":
            self._in_title = False
            if self._title_buf.strip() and not self.title:
                self.title = self._title_buf.strip()
        if self._skip_stack and tag == self._skip_stack[-1]:
            self._skip_stack.pop()
            return
        if self._skip_stack:
            return

    def handle_data(self, data: str) -> None:
        if self._in_ld:
            self._ld_buf += data
            return
        if self._in_title:
            self._title_buf += data
            return
        if self._skip_stack:
            return
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)

    def visible_text(self) -> str:
        return " ".join(self._chunks)

    def json_ld_bodies(self) -> list[str]:
        bodies: list[str] = []
        for block in self._ld:
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                continue
            bodies.extend(_ld_article_bodies(payload))
        return bodies


def _meta_descriptions(html: str) -> list[str]:
    """Pull og/twitter/meta description when body extract is JS-nav chrome only."""
    found: list[str] = []
    seen: set[str] = set()
    for tag in _META_DESC_RE.findall(html or ""):
        match = _META_CONTENT_RE.search(tag)
        if not match:
            continue
        text = " ".join(str(match.group(1) or "").split()).strip()
        if word_count(text) < MIN_SNIPPET_WORDS:
            continue
        if _NAV_CHROME_RE.search(text) or BOILERPLATE_RE.search(text):
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(text)
    return found


def _looks_like_nav_chrome(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return True
    if _NAV_CHROME_RE.search(cleaned):
        return True
    # Dense keyword menus with almost no verbs/punctuation.
    tokens = cleaned.split()
    if len(tokens) <= 16 and cleaned.count(" ") >= 3 and cleaned.count(".") == 0:
        titleish = sum(1 for t in tokens if t[:1].isupper())
        if titleish >= max(3, len(tokens) // 2):
            return True
    return False


def _ld_article_bodies(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, list):
        for item in node:
            found.extend(_ld_article_bodies(item))
        return found
    if not isinstance(node, dict):
        return found
    types = node.get("@type") or node.get("type") or ""
    if isinstance(types, list):
        type_blob = " ".join(str(item).lower() for item in types)
    else:
        type_blob = str(types).lower()
    if any(name in type_blob for name in ARTICLE_LD_TYPES):
        body = node.get("articleBody") or node.get("description")
        if isinstance(body, str) and body.strip():
            found.append(body.strip())
    for key in ("@graph", "mainEntity", "article"):
        if key in node:
            found.extend(_ld_article_bodies(node[key]))
    return found


def default_fetch(url: str) -> tuple[int, str, bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200) or 200)
            content_type = str(response.headers.get("Content-Type") or "")
            data = response.read(MAX_HTML_BYTES + 1)
            final_url = str(response.geturl() or url)
    except HTTPError as exc:
        body = exc.read(4096) if exc.fp else b""
        return int(exc.code or 0), "", body, url
    except (URLError, TimeoutError, OSError):
        return 0, "", b"", url
    if len(data) > MAX_HTML_BYTES:
        return status, content_type, data[:MAX_HTML_BYTES], final_url
    return status, content_type, data, final_url


def _decode_html(data: bytes, content_type: str) -> str:
    charset = "utf-8"
    lowered = content_type.lower()
    if "charset=" in lowered:
        charset = lowered.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
    try:
        return data.decode(charset)
    except LookupError:
        return data.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def extract_factual_snippets(html: str) -> dict[str, Any]:
    parser = _TextCollector()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return {
            "title": None,
            "snippets": [],
            "extracted_text": "",
            "extraction_method": "parse_failed",
            "research_only": True,
        }
    snippets: list[str] = []
    method = "paragraphs"
    for body in parser.json_ld_bodies():
        method = "jsonld_articleBody"
        snippets.extend(_select_snippets(body))
    if len(snippets) < 3:
        visible = parser.visible_text()
        extra = _select_snippets(visible)
        if extra:
            if method != "jsonld_articleBody":
                method = "visible_text"
            snippets.extend(extra)
    # JS-heavy publishers often leave only nav chrome in visible text while
    # still shipping a usable meta/og description — prefer that over menus.
    meta_bits = _meta_descriptions(html)
    if meta_bits and (
        not snippets or all(_looks_like_nav_chrome(item) for item in snippets)
    ):
        method = "meta_description"
        snippets = list(meta_bits) + [s for s in snippets if not _looks_like_nav_chrome(s)]
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in snippets:
        key = re.sub(r"\s+", " ", item.lower())
        if key in seen or BOILERPLATE_RE.search(item) or _looks_like_nav_chrome(item):
            continue
        seen.add(key)
        cleaned.append(item)
        if len(cleaned) >= MAX_SNIPPETS_PER_SOURCE:
            break
    text = clean_extracted_article_text(" ".join(cleaned))
    cleaned = _select_snippets(text) if text else []
    text = " ".join(cleaned) if cleaned else text
    if _looks_like_nav_chrome(text):
        text = ""
        cleaned = []
    if not text and meta_bits:
        method = "meta_description"
        cleaned = [bit for bit in meta_bits if not _looks_like_nav_chrome(bit)]
        text = " ".join(cleaned)
    if len(text) > MAX_EXTRACTED_CHARS:
        text = text[:MAX_EXTRACTED_CHARS].rsplit(" ", 1)[0]
        cleaned = _select_snippets(text)
        text = " ".join(cleaned) if cleaned else text
    return {
        "title": parser.title,
        "snippets": cleaned,
        "extracted_text": text,
        "extraction_method": method if text else "empty_or_js_only_body",
        "research_only": True,
    }


def _select_snippets(text: str) -> list[str]:
    out: list[str] = []
    for sentence in split_sentences(text):
        if word_count(sentence) < MIN_SNIPPET_WORDS:
            continue
        if BOILERPLATE_RE.search(sentence) or _looks_like_nav_chrome(sentence):
            continue
        if NUMBER_TOKEN_RE.search(sentence) or ATTRIBUTION_RE.search(sentence) or word_count(sentence) >= 12:
            out.append(sentence.strip())
        if len(out) >= MAX_SNIPPETS_PER_SOURCE:
            break
    return out


def _source_rank(row: dict[str, Any]) -> tuple[int, float]:
    role = str(row.get("source_role") or "")
    if role == "primary_evidence":
        bucket = 0
    elif str(row.get("source_type") or "") in {"regulator", "company", "official"}:
        bucket = 0
    elif role == "newsroom" or str(row.get("source_type") or "") == "newsroom":
        bucket = 1
    else:
        bucket = 2
    try:
        authority = float(row.get("source_authority") or 0.0)
    except (TypeError, ValueError):
        authority = 0.0
    return (bucket, -authority)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def enrich_evidence_list(
    evidence: list[dict[str, Any]],
    *,
    event_id: str,
    fetch: FetchFn | None = None,
    now: str | None = None,
    max_sources: int | None = None,
) -> list[dict[str, Any]]:
    fetcher = fetch or default_fetch
    stamp = now or _utc_now()
    source_limit = MAX_SOURCES_PER_EVENT if max_sources is None else max(0, int(max_sources))
    ranked = sorted([row for row in evidence if isinstance(row, dict)], key=_source_rank)
    fetched = 0
    out: list[dict[str, Any]] = []
    for row in ranked:
        item = dict(row)
        url = item.get("url")
        if fetched < source_limit and isinstance(url, str) and url.startswith("http"):
            status, content_type, data, final_url = fetcher(url)
            fetched += 1
            item["retrieved_at_utc"] = stamp
            item["event_id"] = event_id
            item["research_only"] = True
            if status in BLOCKED_STATUS_CODES:
                item["extraction_method"] = f"fetch_blocked_{status}"
                item["extracted_text"] = ""
                item["factual_snippets"] = []
                item["access_blocked"] = True
            elif status == 0:
                item["extraction_method"] = "fetch_timeout_or_connection_error"
                item["extracted_text"] = ""
                item["factual_snippets"] = []
            elif status == 200 and data:
                html = _decode_html(data, content_type)
                if PAYWALL_RE.search(html):
                    item["extraction_method"] = "access_or_subscription_block"
                    item["extracted_text"] = ""
                    item["factual_snippets"] = []
                    item["access_blocked"] = True
                else:
                    extracted = extract_factual_snippets(html)
                    extracted_text = str(extracted.get("extracted_text") or "").strip()
                    snippets = list(extracted.get("snippets") or [])
                    # Prefer RSS/title summary over nav-chrome "extracts".
                    summary = str(item.get("summary") or "").strip()
                    if (not extracted_text or _looks_like_nav_chrome(extracted_text)) and summary:
                        extracted_text = summary
                        snippets = _select_snippets(summary) or [summary]
                        item["extraction_method"] = "summary_fallback"
                    else:
                        item["extraction_method"] = (
                            extracted.get("extraction_method")
                            if extracted_text
                            else "empty_or_js_only_body"
                        )
                    item["extracted_text"] = extracted_text
                    item["factual_snippets"] = snippets
                    if extracted.get("title") and not item.get("title"):
                        item["title"] = extracted["title"]
                    item["resolved_url"] = final_url
            else:
                item["extraction_method"] = f"fetch_failed_{status}"
                item["extracted_text"] = ""
                item["factual_snippets"] = []
        # Paywalled / blocked / empty fetches: still keep summary as research text.
        if not str(item.get("extracted_text") or "").strip():
            summary = str(item.get("summary") or "").strip()
            if summary:
                item["extracted_text"] = summary
                item["factual_snippets"] = _select_snippets(summary) or [summary]
                if not item.get("extraction_method") or str(item.get("extraction_method")).startswith(
                    ("fetch_", "access_", "empty_")
                ):
                    item["extraction_method"] = "summary_fallback"
        out.append(item)
    # Keep original order by URL after enrichment of ranked copies.
    by_url = {str(row.get("url")): row for row in out}
    ordered = []
    seen_urls: set[str] = set()
    for original in evidence:
        if not isinstance(original, dict):
            continue
        url = str(original.get("url"))
        ordered.append(by_url.get(url, dict(original)))
        seen_urls.add(url)
    for row in out:
        url = str(row.get("url"))
        if url not in seen_urls:
            ordered.append(row)
    return ordered


def evidence_sufficiency(article_input: dict[str, Any]) -> dict[str, Any]:
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    sources = []
    extracted_parts: list[str] = []
    facts: list[str] = []
    attributions = 0
    primary = False
    independent: set[str] = set()
    for row in evidence:
        if not isinstance(row, dict):
            continue
        source = str(row.get("source") or "")
        if source:
            sources.append(source)
            host = urlparse(str(row.get("url") or "")).netloc.lower()
            independent.add(host or source)
        if str(row.get("source_role") or "") == "primary_evidence":
            primary = True
        extracted = row.get("extracted_text") if isinstance(row.get("extracted_text"), str) else ""
        snippets = row.get("factual_snippets") if isinstance(row.get("factual_snippets"), list) else []
        summary = row.get("summary") if isinstance(row.get("summary"), str) else ""
        title = row.get("title") if isinstance(row.get("title"), str) else ""
        blob = " ".join([extracted] + [str(s) for s in snippets if isinstance(s, str)] + [summary, title])
        extracted_parts.append(extracted)
        for sentence in split_sentences(blob):
            if word_count(sentence) < MIN_SNIPPET_WORDS:
                continue
            if BOILERPLATE_RE.search(sentence):
                continue
            if NUMBER_TOKEN_RE.search(sentence) or ATTRIBUTION_RE.search(sentence) or word_count(sentence) >= 12:
                facts.append(sentence)
            if ATTRIBUTION_RE.search(sentence):
                attributions += 1
    extracted_words = word_count(" ".join(extracted_parts))
    numbers = set()
    time_facts = 0
    entities: set[str] = set()
    blob_all = " ".join(extracted_parts + facts)
    for blob in extracted_parts + facts:
        for match in NUMBER_TOKEN_RE.finditer(blob):
            numbers.add(match.group(0).lower())
        time_facts += len(TIME_RE.findall(blob))
        for match in ENTITY_RE.finditer(blob):
            entities.add(match.group(1))
    distinct_facts = []
    seen: set[str] = set()
    for fact in facts:
        key = re.sub(r"\s+", " ", fact.lower())
        if key in seen:
            continue
        seen.add(key)
        distinct_facts.append(fact)
    independent_with_extract = 0
    for row in evidence:
        if isinstance(row, dict) and str(row.get("extracted_text") or "").strip():
            independent_with_extract += 1
    independent_count = max(len(independent), independent_with_extract)
    inferred = "other"
    if EXPLOIT_RE.search(blob_all):
        inferred = "exploit"
    elif MARKET_RE.search(blob_all):
        inferred = "market"
    elif REGULATORY_RE.search(blob_all) or primary:
        inferred = "regulatory"
    base = (
        len(distinct_facts) >= MIN_DISTINCT_FACTS
        and extracted_words >= MIN_EXTRACTED_WORDS
        and (len(numbers) >= 1 or attributions >= 1)
        and independent_with_extract >= 1
    )
    if inferred == "market":
        sufficient = base and len(numbers) >= 2 and time_facts >= 1
    elif inferred == "exploit":
        sufficient = base and len(numbers) >= 1 and attributions >= 1 and time_facts >= 1
    elif inferred == "regulatory":
        sufficient = base and (primary or attributions >= 1)
    else:
        sufficient = base
    status = STATUS_SUFFICIENT if sufficient else STATUS_INSUFFICIENT
    return {
        "status": status,
        "source_count": len({item for item in sources if item}),
        "extracted_evidence_words": extracted_words,
        "distinct_fact_count": len(distinct_facts),
        "number_count": len(numbers),
        "attribution_count": attributions,
        "primary_source_present": primary,
        "independent_source_count": independent_count,
        "independent_extracted_source_count": independent_with_extract,
        "entity_count": len(entities),
        "chronology_time_fact_count": time_facts,
        "inferred_story_type": inferred,
    }


def enrich_story(
    story: dict[str, Any],
    *,
    fetch: FetchFn | None = None,
    now: str | None = None,
    max_sources: int | None = None,
) -> dict[str, Any]:
    row = dict(story)
    article_input = dict(row.get("article_input") or {})
    event_id = str(row.get("event_id") or article_input.get("event_id") or "")
    evidence = article_input.get("evidence") if isinstance(article_input.get("evidence"), list) else []
    article_input["evidence"] = enrich_evidence_list(
        [dict(item) for item in evidence if isinstance(item, dict)],
        event_id=event_id,
        fetch=fetch,
        now=now,
        max_sources=max_sources,
    )
    article_input["browse"] = False
    article_input["fetch_fulltext"] = True
    article_input["evidence_is_research_only"] = True
    sufficiency = evidence_sufficiency(article_input)
    article_input["evidence_sufficiency"] = sufficiency
    row["article_input"] = article_input
    row["evidence_sufficiency"] = sufficiency
    if sufficiency["status"] == STATUS_INSUFFICIENT:
        row["skip_reason"] = STATUS_INSUFFICIENT
    return row


def enrich_stories(
    stories: list[dict[str, Any]],
    *,
    fetch: FetchFn | None = None,
    now: str | None = None,
) -> list[dict[str, Any]]:
    return [enrich_story(story, fetch=fetch, now=now) for story in stories]
