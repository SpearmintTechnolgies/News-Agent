from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "gclid",
    "fbclid",
}


def repair_mojibake(text: str) -> str:
    # Typical UTF-8 bytes incorrectly interpreted as Windows-1252.
    suspicious = ("â€", "â€™", "â€œ", "â€˜", "Â", "Ã")

    if not any(marker in text for marker in suspicious):
        return text

    try:
        repaired = text.encode("cp1252").decode("utf-8")

        # Only accept repair if it reduces suspicious sequences.
        before = sum(text.count(x) for x in suspicious)
        after = sum(repaired.count(x) for x in suspicious)

        if after < before:
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    replacements = {
        "â€™": "'",
        "â€˜": "'",
        "â€œ": '"',
        "â€": '"',
        "â€“": "-",
        "â€”": "-",
        "â€¦": "...",
        "Â": "",
    }

    for broken, fixed in replacements.items():
        text = text.replace(broken, fixed)

    return text


def clean_text(value: str | None) -> str:
    if not value:
        return ""

    text = html.unescape(value)

    text = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    text = re.sub(r"<[^>]+>", " ", text)
    text = repair_mojibake(text)

    return re.sub(r"\s+", " ", text).strip()


def clean_url(url: str) -> str:
    try:
        parsed = urlsplit(url.strip())

        query = [
            (key, value)
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if key.lower() not in TRACKING_PARAMS
            and not key.lower().startswith("utm_")
        ]

        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                urlencode(query, doseq=True),
                "",
            )
        )

    except Exception:
        return url.strip()
