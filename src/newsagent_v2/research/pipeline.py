"""Deep research orchestration for one event."""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from newsagent_v2.research.dossier import (
    KIND_NEWS,
    KIND_PRIMARY,
    ORIGIN_CLUSTER,
    ORIGIN_OUTLINK,
    ORIGIN_SEARCH,
    ResearchDossier,
    SourceDoc,
    host_of,
)
from newsagent_v2.research.extract import extract
from newsagent_v2.research.fetch import (
    HTTP_HEADERS,
    HTTP_TIMEOUT_SECONDS,
    BrowserFetcher,
    FetchResult,
    http_fetch,
)
from newsagent_v2.research.primary import is_blocked_host, is_primary_url
from newsagent_v2.research.search import SearchHit, search_news

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    "a an the and or but of to in on for with by at from as is are was were be been has have had "
    "it its this that these those after before over under into amid about new says said will would "
    "could may might than then also just more most per vs via what why how who when where report reports".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9$%.'-]*[a-z0-9%]|[a-z0-9]")


@dataclass
class ResearchConfig:
    max_queries: int = 3
    max_search_fetches: int = 10
    max_outlink_fetches: int = 4
    max_browser_fetches: int = 8
    max_search_age_hours: float = 96.0
    min_search_title_relevance: float = 0.25
    min_relevance: float = 0.35
    min_cluster_relevance: float = 0.3
    min_primary_outlink_relevance: float = 0.3
    browser_retry_below_words: int = 150
    syndication_overlap: float = 0.6
    time_budget_seconds: float = 150.0
    http_workers: int = 6
    use_search: bool = True
    use_browser: bool = True


SearchFn = Callable[[list[str]], list[SearchHit]]
HttpFetchFn = Callable[[str], FetchResult]


def _tokens(text: str) -> set[str]:
    return {
        t.strip(".'-")
        for t in _TOKEN_RE.findall(text.lower())
        if t.strip(".'-") and t.strip(".'-") not in _STOPWORDS and (len(t) > 2 or t.isdigit())
    }


def event_terms(title: str, entities: list[str]) -> set[str]:
    terms = _tokens(title)
    for entity in entities:
        terms |= _tokens(entity)
    return terms


def relevance(terms: set[str], entities: list[str], title: str, paragraphs: list[str]) -> float:
    if not terms:
        return 0.0
    head = " ".join([title] + paragraphs[:4])
    overlap = len(terms & _tokens(head)) / len(terms)
    if entities:
        lowered = head.lower()
        if not any(e.lower() in lowered for e in entities if len(e) >= 3):
            overlap *= 0.5
    return round(overlap, 3)


_PROPER_RE = re.compile(r"(?<=[a-z0-9,;:)’'\"] )((?:[A-Z][\w&’'.-]*|[A-Z]{2,})(?:\s+(?:[A-Z][\w&’'.-]*|of|de|and)){0,3})")
_PROPER_SKIP = frozenset({"The", "This", "That", "He", "She", "It", "They", "We", "His", "Her", "In", "On", "A"})


_CALENDAR = frozenset(
    "january february march april may june july august september october november december "
    "monday tuesday wednesday thursday friday saturday sunday".split()
)


def named_entities(title: str, texts: list[str], limit: int = 5) -> list[str]:
    """Proper names written capitalized mid-sentence in body text that also occur in the headline.

    A token written lowercase at least as often as capitalized ("could",
    "surge") is an ordinary word from an embedded headline, not part of a name.
    """
    title_tokens = _tokens(title)
    body = " ".join(texts)
    lower_counts: dict[str, int] = {}
    cap_counts: dict[str, int] = {}
    for word in re.findall(r"\b[A-Za-z][\w’'-]*", body):
        bucket = lower_counts if word[0].islower() else cap_counts
        bucket[word.lower()] = bucket.get(word.lower(), 0) + 1
    counts: dict[str, int] = {}
    for text in texts:
        for match in _PROPER_RE.finditer(text):
            name = re.sub(r"\s+(?:of|de|and)$", "", match.group(1).strip(" .,'’-"))
            if not name or name in _PROPER_SKIP or name.endswith(("’s", "'s")):
                continue
            words = [w for w in name.split() if w not in {"of", "de", "and"}]
            if len(words) > 3:
                continue
            if any(
                w.lower() in _CALENDAR
                or lower_counts.get(w.lower(), 0) >= cap_counts.get(w.lower(), 0)
                for w in words
            ):
                continue
            if not (_tokens(name) & title_tokens):
                continue
            counts[name] = counts.get(name, 0) + 1
    ranked = sorted(counts, key=lambda n: (-counts[n], -len(n)))
    out: list[str] = []
    for name in ranked:
        if any(name in kept or kept in name for kept in out):
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


def build_queries(title: str, entities: list[str], limit: int) -> list[str]:
    clean_title = re.sub(r"[’']s\b", "", title)
    clean_title = re.sub(r"[\"“”‘’'|:—–-]+", " ", clean_title)
    clean_title = re.sub(r"\s+", " ", clean_title).strip()
    queries = [clean_title] if clean_title else []
    named = [e for e in entities if len(e) >= 3][:3]
    if len(named) >= 2:
        queries.append(" ".join(named))
    if named:
        key = [t for t in _tokens(title) if t not in _tokens(" ".join(named))][:3]
        if key:
            queries.append(" ".join(named[:2] + key))
    out: list[str] = []
    for q in queries:
        if q and q.lower() not in {x.lower() for x in out}:
            out.append(q)
    return out[:limit]


def _shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i : i + n]) for i in range(max(0, len(words) - n + 1))}


def _looks_like_domain(name: str) -> bool:
    return bool(re.fullmatch(r"(?:[\w-]+\.)+[a-z]{2,}", name.strip().lower()))


def _url_key(url: str) -> str:
    return url.split("#")[0].split("?")[0].rstrip("/").lower()


def _doc_from_seed(row: dict[str, Any]) -> SourceDoc:
    return SourceDoc(
        url=str(row.get("url") or ""),
        publisher=str(row.get("source") or ""),
        title=str(row.get("title") or ""),
        published_at=str(row.get("published") or ""),
        summary=str(row.get("summary") or ""),
        origin=ORIGIN_CLUSTER,
    )


def _doc_from_hit(hit: SearchHit) -> SourceDoc:
    return SourceDoc(
        url=hit.url,
        publisher=hit.publisher,
        title=hit.title,
        published_at=hit.published_at,
        origin=ORIGIN_SEARCH,
        method=hit.engine,
    )


class _Researcher:
    def __init__(
        self,
        story: dict[str, Any],
        config: ResearchConfig,
        search_fn: SearchFn | None,
        http_fetch_fn: HttpFetchFn | None,
        browser: BrowserFetcher | None,
    ) -> None:
        self.config = config
        self.started = time.monotonic()
        pack = story.get("article_input") if isinstance(story.get("article_input"), dict) else {}
        self.event_id = str(story.get("event_id") or pack.get("event_id") or "")
        self.title = str(story.get("representative_title") or pack.get("representative_title") or "").strip()
        self.entities = [str(e).strip() for e in (story.get("entities") or pack.get("entities") or []) if str(e).strip()]
        seeds = pack.get("evidence") or story.get("evidence") or []
        self.seeds = [row for row in seeds if isinstance(row, dict) and str(row.get("url") or "").startswith("http")]
        self.terms = event_terms(self.title, self.entities)
        self.search_fn = search_fn
        self.http_fetch_fn = http_fetch_fn
        self.browser = browser
        self.own_browser = browser is None and config.use_browser
        self.browser_uses = 0
        self.seen_urls: set[str] = set()
        self.dossier = ResearchDossier(event_id=self.event_id, title=self.title, entities=list(self.entities))

    def time_left(self) -> float:
        return self.config.time_budget_seconds - (time.monotonic() - self.started)

    # --- fetch + extract -------------------------------------------------
    def _fill(self, doc: SourceDoc, result: FetchResult) -> None:
        doc.status = result.status
        doc.method = result.method if not doc.method else f"{doc.method}+{result.method}"
        if not result.ok:
            return
        ex = extract(result.html, result.final_url or doc.url)
        if ex.word_count >= doc.word_count:
            doc.paragraphs = ex.paragraphs
            doc.links = ex.links
        doc.title = doc.title or ex.title
        doc.published_at = doc.published_at or ex.published_at
        doc.author = doc.author or ex.author
        if ex.sitename and (not doc.publisher or _looks_like_domain(doc.publisher)):
            doc.publisher = ex.sitename
        doc.publisher = doc.publisher or doc.host
        doc.summary = doc.summary or ex.description
        if ex.paywalled:
            doc.rejected_reason = "paywalled"

    def _http(self, url: str, client: httpx.Client) -> FetchResult:
        if self.http_fetch_fn is not None:
            return self.http_fetch_fn(url)
        return http_fetch(url, client)

    def fetch_all(self, docs: list[SourceDoc]) -> None:
        docs = [d for d in docs if d.url]
        if not docs:
            return
        with httpx.Client(headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
            with ThreadPoolExecutor(max_workers=self.config.http_workers) as pool:
                results = list(pool.map(lambda d: self._http(d.url, client), docs))
        for doc, result in zip(docs, results):
            self._fill(doc, result)
        if not self.config.use_browser:
            return
        for doc in docs:
            if doc.word_count >= self.config.browser_retry_below_words and not doc.rejected_reason:
                continue
            if self.browser_uses >= self.config.max_browser_fetches or self.time_left() < 30:
                break
            browser = self._browser()
            if browser is None:
                break
            self.browser_uses += 1
            doc.rejected_reason = ""
            self._fill(doc, browser.fetch(doc.url))

    def _browser(self) -> BrowserFetcher | None:
        if self.browser is None and self.config.use_browser:
            self.browser = BrowserFetcher()
        if self.browser is not None and self.browser.disabled_reason:
            return None
        return self.browser

    # --- selection -------------------------------------------------------
    def _accept(self, doc: SourceDoc) -> None:
        doc.kind = KIND_PRIMARY if is_primary_url(doc.url, self.entities) else KIND_NEWS
        doc.relevance = relevance(self.terms, self.entities, doc.title, doc.paragraphs)
        floor = self.config.min_cluster_relevance if doc.origin == ORIGIN_CLUSTER else self.config.min_relevance
        if doc.kind == KIND_PRIMARY and doc.origin == ORIGIN_OUTLINK:
            floor = min(floor, self.config.min_primary_outlink_relevance)
        if not doc.rejected_reason:
            if not doc.paragraphs:
                doc.rejected_reason = f"no_text(status={doc.status})"
            elif doc.word_count < 60:
                doc.rejected_reason = f"too_short({doc.word_count}w)"
            elif doc.relevance < floor:
                doc.rejected_reason = f"off_topic(relevance={doc.relevance})"
        (self.dossier.rejected if doc.rejected_reason else self.dossier.sources).append(doc)

    def _claim(self, url: str) -> bool:
        key = _url_key(url)
        if not key or key in self.seen_urls or is_blocked_host(url):
            return False
        self.seen_urls.add(key)
        return True

    def dedupe_syndication(self) -> None:
        kept: list[tuple[SourceDoc, set]] = []
        ordered = sorted(
            self.dossier.sources,
            key=lambda d: (d.kind != KIND_PRIMARY, -d.word_count),
        )
        for doc in ordered:
            sh = _shingles(doc.text)
            dup_of = None
            for other, other_sh in kept:
                if not sh or not other_sh:
                    continue
                contain = len(sh & other_sh) / min(len(sh), len(other_sh))
                if contain >= self.config.syndication_overlap:
                    dup_of = other
                    break
            if dup_of is not None:
                doc.rejected_reason = f"syndicated_copy_of={dup_of.host}"
                self.dossier.rejected.append(doc)
            else:
                kept.append((doc, sh))
        self.dossier.sources = [d for d, _ in kept]

    # --- stages ----------------------------------------------------------
    def run(self) -> ResearchDossier:
        try:
            seeds = [_doc_from_seed(r) for r in self.seeds if self._claim(str(r.get("url")))]
            self.fetch_all(seeds)
            self._resolve_entities(seeds)
            for doc in seeds:
                self._accept(doc)

            if self.config.use_search and self.time_left() > 40:
                self._search_stage()
            if self.time_left() > 25:
                self._outlink_stage()
            self.dedupe_syndication()
        finally:
            if self.own_browser and self.browser is not None:
                self.browser.close()
        self.dossier.elapsed_seconds = time.monotonic() - self.started
        logger.info("[RESEARCH] event=%s %s", self.event_id, self.dossier.stats())
        return self.dossier

    def _resolve_entities(self, seeds: list[SourceDoc]) -> None:
        texts = [p for d in seeds for p in d.paragraphs[:12]] + [d.summary for d in seeds if d.summary]
        named = named_entities(self.title, texts)
        if not named:
            named = [e for e in self.entities if any(c.isupper() for c in e)]
        self.entities = named
        self.dossier.entities = list(named)
        self.terms = event_terms(self.title, named)

    def _search_stage(self) -> None:
        queries = build_queries(self.title, self.entities, self.config.max_queries)
        self.dossier.queries = queries
        try:
            hits = self.search_fn(queries) if self.search_fn else search_news(queries)
        except Exception as exc:
            self.dossier.notes.append(f"search_failed:{type(exc).__name__}")
            return
        scored: list[tuple[float, SearchHit]] = []
        for hit in hits:
            age = hit.age_hours()
            if age is not None and age > self.config.max_search_age_hours:
                continue
            score = relevance(self.terms, [], hit.title, [])
            if score >= self.config.min_search_title_relevance:
                scored.append((score, hit))
        scored.sort(key=lambda pair: -pair[0])
        docs: list[SourceDoc] = []
        hosts = {d.host for d in self.dossier.sources}
        for _, hit in scored:
            if len(docs) >= self.config.max_search_fetches:
                break
            doc = _doc_from_hit(hit)
            if doc.host in hosts or not self._claim(hit.url):
                continue
            hosts.add(doc.host)
            docs.append(doc)
        self.fetch_all(docs)
        for doc in docs:
            self._accept(doc)

    def _outlink_stage(self) -> None:
        candidates: list[SourceDoc] = []
        for doc in list(self.dossier.sources):
            for link in doc.links:
                if len(candidates) >= self.config.max_outlink_fetches:
                    break
                if not is_primary_url(link, self.entities) or host_of(link) == doc.host:
                    continue
                if not self._claim(link):
                    continue
                candidates.append(SourceDoc(url=link, origin=ORIGIN_OUTLINK, kind=KIND_PRIMARY))
        self.fetch_all(candidates)
        for doc in candidates:
            self._accept(doc)


def deep_research(
    story: dict[str, Any],
    *,
    config: ResearchConfig | None = None,
    search_fn: SearchFn | None = None,
    http_fetch_fn: HttpFetchFn | None = None,
    browser: BrowserFetcher | None = None,
) -> ResearchDossier:
    """Research one event. ``story`` carries title, entities and cluster evidence rows."""
    return _Researcher(story, config or ResearchConfig(), search_fn, http_fetch_fn, browser).run()
