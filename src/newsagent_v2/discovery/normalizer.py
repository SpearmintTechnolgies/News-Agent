"""Deterministic news content normalization."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .raw_news_item import RawNewsItem


# Tracking parameters to remove from URLs
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
    "ref",
    "mc_cid",
    "mc_eid",
}

# Stop words for title normalization
STOP_WORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "as", "at", "by", "with", "from", "after", "amid", "over", "into", "its",
    "it", "this", "that", "their", "be", "been", "being", "have", "has", "had",
    "do", "does", "did", "will", "would", "could", "should", "may", "might",
}

# Common mojibake patterns
MOJIBAKE_PATTERNS = {
    "â€™": "'",
    "â€˜": "'",
    "â€œ": '"',
    "â€": '"',
    "â€“": "-",
    "â€”": "-",
    "â€¦": "...",
    "Â": "",
}


def repair_mojibake(text: str) -> str:
    """Repair UTF-8 mojibake."""
    suspicious = ("â€", "â€™", "â€œ", "â€˜", "Â", "Ã")

    if not any(marker in text for marker in suspicious):
        return text

    try:
        repaired = text.encode("cp1252").decode("utf-8")
        before = sum(text.count(x) for x in suspicious)
        after = sum(repaired.count(x) for x in suspicious)
        if after < before:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    # Fallback: manual replacements
    for broken, fixed in MOJIBAKE_PATTERNS.items():
        text = text.replace(broken, fixed)

    return text


def clean_text(text: str | None) -> str:
    """Clean and normalize text content.

    - Unescape HTML entities
    - Remove HTML tags
    - Repair mojibake
    - Normalize whitespace
    """
    if not text:
        return ""

    text = html.unescape(str(text))

    # Remove script/style tags and content
    text = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ", text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)

    # Repair mojibake
    text = repair_mojibake(text)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()

    return text


def normalize_title(title: str | None) -> str:
    """Normalize headline for comparison.

    - Lowercase
    - Remove punctuation
    - Remove stop words
    """
    if not title:
        return ""

    title = str(title).lower()
    # Remove punctuation except hyphens
    title = re.sub(r"[^\w\s-]", " ", title)
    # Split into words
    words = re.findall(r"[a-z0-9-]+", title)
    # Remove stop words
    words = [w for w in words if w not in STOP_WORDS]
    return " ".join(words)


def canonicalize_url(url: str | None) -> str:
    """Create canonical URL by removing tracking parameters.

    - Normalize scheme and netloc to lowercase
    - Remove tracking query parameters
    - Strip trailing slashes from path
    """
    if not url:
        return ""

    try:
        parsed = urlsplit(str(url).strip())

        # Filter query parameters
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")
        ]

        return urlunsplit((
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/"),
            urlencode(query, doseq=True) if query else "",
            "",
        ))
    except Exception:
        return str(url).strip() if url else ""


def normalize_source_name(name: str | None) -> str:
    """Normalize source name."""
    if not name:
        return ""
    return str(name).strip()


def normalize_timestamp(ts: str | datetime | None) -> str | None:
    """Normalize timestamp to ISO format.

    Handles various input formats from RSS feeds.
    """
    if ts is None:
        return None

    if isinstance(ts, datetime):
        return ts.isoformat()

    ts_str = str(ts).strip()
    if not ts_str:
        return None

    # Try to parse common formats
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(ts_str)
        if dt:
            return dt.isoformat()
    except Exception:
        pass

    # Try ISO format directly
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.isoformat()
    except Exception:
        pass

    # Return as-is if we can't parse
    return ts_str


# Common stop words to exclude from entities
ENTITY_STOP_WORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
    'from', 'as', 'is', 'was', 'are', 'be', 'been', 'have', 'has', 'had', 'do', 'does',
    'did', 'will', 'would', 'could', 'should', 'may', 'might', 'must', 'shall', 'can',
    'said', 'says', 'say', 'according', 'after', 'before', 'during', 'over', 'under',
    'more', 'most', 'some', 'many', 'much', 'such', 'what', 'when', 'where', 'who',
    'why', 'how', 'all', 'any', 'both', 'each', 'few', 'other', 'another', 'it', 'its',
    'this', 'that', 'these', 'those', 'about', 'up', 'out', 'if', 'then', 'so',
})


def extract_entities(headline: str, description: str = "") -> list[str]:
    """Extract probable named entities from text using deterministic rules.

    - Acronyms/tickers (uppercase 2-12 chars)
    - Capitalized words (potential proper nouns)
    - Stop word filtering
    - Alias normalization
    """
    entities: set[str] = set()
    text = f"{headline} {description}"

    # Find words
    words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", text)

    for word in words:
        # Skip short words
        if len(word) < 2:
            continue

        # Normalize: lowercase
        normalized = word.lower()

        # Skip stop words
        if normalized in ENTITY_STOP_WORDS:
            continue

        # Skip standalone common words
        if normalized in {'new', 'last', 'first', 'one', 'two', 'three', 'year', 'years',
                         'day', 'days', 'week', 'weeks', 'month', 'months', 'time'}:
            continue

        # Acronyms/tickers (uppercase)
        if word.isupper() and 2 <= len(word) <= 12:
            entities.add(normalized)
            continue

        # Named entities (capitalized, not sentence start)
        if word[0].isupper() and len(word) >= 3:
            # Clean up apostrophes for names like O'Leary
            # Normalize apostrophe variations
            clean = normalized.replace("'", "").replace("`", "")
            if clean and clean not in ENTITY_STOP_WORDS:
                entities.add(clean)

    return sorted(entities)


def extract_topics(headline: str, description: str = "") -> list[str]:
    """Extract topic tags based on deterministic keyword matching."""
    text = f"{headline} {description}".lower()
    topics: set[str] = set()

    # Topic patterns
    topic_patterns = {
        "bitcoin": [r"\bbitcoin\b", r"\bbtc\b"],
        "ethereum": [r"\bethereum\b", r"\beth\b"],
        "regulation": [r"\bsec\b", r"\bregulator", r"\bregulation", r"\bcourt\b"],
        "defi": [r"\bdefi\b", r"\bdecentralized\s+finance\b"],
        "etf": [r"\betf\b", r"\bexchange\s+traded\s+fund"],
        "stablecoin": [r"\bstablecoin", r"\busdt\b", r"\busdc\b"],
        "security": [r"\bhack", r"\bbreach", r"\bexploit", r"\bphishing"],
        "market": [r"\bprice", r"\btrading", r"\bvolume"],
        "institutional": [r"\binstitutional", r"\bcorporate\s+treasury"],
        "mining": [r"\bmining", r"\bminer"],
    }

    for topic, patterns in topic_patterns.items():
        for pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                topics.add(topic)
                break

    return sorted(topics)


class NewsNormalizer:
    """Deterministic news normalization engine."""

    def normalize_item(
        self,
        raw: dict[str, Any],
        source_metadata: dict[str, Any] | None = None,
    ) -> RawNewsItem:
        """Normalize a raw item into RawNewsItem.

        Args:
            raw: Raw collected data (e.g., from RSS parser)
            source_metadata: Source configuration metadata
        """
        # Extract raw values
        raw_title = str(raw.get("title") or "")
        raw_link = str(raw.get("link") or raw.get("url") or "")
        raw_summary = str(raw.get("summary") or raw.get("description") or "")
        raw_published = raw.get("published") or raw.get("updated")

        # Clean and normalize
        headline = clean_text(raw_title)
        description = clean_text(raw_summary)
        original_url = raw_link
        canonical_url = canonicalize_url(raw_link)
        published_at = normalize_timestamp(raw_published)

        # Get source info
        source_info = source_metadata or {}

        # Create item
        item = RawNewsItem(
            headline=headline,
            description=description,
            original_url=original_url,
            canonical_url=canonical_url,
            published_at=published_at,
            raw_title=raw_title,
            raw_description=raw_summary,
            source=source_info.get("name", "unknown"),
            source_id=source_info.get("source_id", "unknown"),
            source_type=source_info.get("source_type", "rss"),
            source_role=source_info.get("role", "discovery"),
            source_authority=float(source_info.get("authority", 0.5)),
            raw_metadata=dict(raw),
        )

        # Enrich
        item.entities = extract_entities(headline, description)
        item.topics = extract_topics(headline, description)
        item.keywords = normalize_title(headline).split()

        # Compute fingerprint
        item.compute_fingerprint()

        return item

    def normalize_batch(
        self,
        raw_items: list[dict[str, Any]],
        source_metadata: dict[str, Any] | None = None,
    ) -> list[RawNewsItem]:
        """Normalize a batch of items."""
        return [self.normalize_item(item, source_metadata) for item in raw_items]
