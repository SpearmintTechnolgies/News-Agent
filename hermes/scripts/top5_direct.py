#!/usr/bin/env python3

from __future__ import annotations

import json
import re
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from google import genai
from google.genai import types


HOME = Path.home() / ".hermes"
ENV_FILE = HOME / ".env"

SEARX_URL = "https://paulgo.io"
PROJECT = "memecoinist"

FLUX_SCRIPT = HOME / "scripts" / "top5_pipeline.py"


def load_env():
    data = {}

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()

            if (
                not line
                or line.startswith("#")
                or "=" not in line
            ):
                continue

            key, value = line.split("=", 1)

            data[key.strip()] = (
                value.strip()
                .strip('"')
                .strip("'")
            )

    return data


ENV = load_env()

PROJECT_ID = (
    ENV.get(
        "GOOGLE_CLOUD_PROJECT",
        "ayush-api-506812",
    )
)

CREDENTIALS = ENV.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "",
)

# Resolve the actual existing credential file.
# Linux paths are case-sensitive, so never trust a stale
# /home/rupesh vs /home/Rupesh path.
_default_credential = (
    HOME
    / "credentials"
    / "google"
    / "service-account.json"
)

if (
    not CREDENTIALS
    or not Path(CREDENTIALS).is_file()
):
    CREDENTIALS = str(_default_credential)

if not Path(CREDENTIALS).is_file():
    _real_candidates = list(
        Path("/home").glob(
            "*/.hermes/credentials/google/service-account.json"
        )
    )

    if _real_candidates:
        CREDENTIALS = str(
            _real_candidates[0]
        )

TELEGRAM_TOKEN = ENV.get(
    "TELEGRAM_BOT_TOKEN",
    "",
)




def search_news():
    """
    Local RSS research.

    No SearXNG.
    No search API.
    No LLM.
    No Vertex usage.

    Pull a small number of current crypto headlines locally,
    deduplicate them, and pass only compact evidence to Gemini.
    """

    import email.utils
    import time
    import xml.etree.ElementTree as ET

    feeds = [
        (
            "CoinDesk",
            "https://www.coindesk.com/arc/outboundfeeds/rss/",
        ),
        (
            "Cointelegraph",
            "https://cointelegraph.com/rss",
        ),
        (
            "Decrypt",
            "https://decrypt.co/feed",
        ),
    ]

    now = time.time()

    seen_titles = set()
    seen_urls = set()
    results = []

    for source_name, feed_url in feeds:

        try:
            req = Request(
                feed_url,
                headers={
                    "User-Agent":
                        "Spearmint-Top5/1.0",
                    "Accept":
                        "application/rss+xml, application/xml, text/xml",
                },
            )

            with urlopen(
                req,
                timeout=15,
            ) as response:

                raw = response.read()

            root = ET.fromstring(raw)

        except Exception as exc:

            print(
                f"RSS failed: {source_name}: {exc}"
            )

            continue

        # RSS <item>
        items = root.findall(".//item")

        for item in items[:10]:

            title = (
                item.findtext(
                    "title",
                    default="",
                )
                .strip()
            )

            link = (
                item.findtext(
                    "link",
                    default="",
                )
                .strip()
            )

            description = (
                item.findtext(
                    "description",
                    default="",
                )
                .strip()
            )

            pub = (
                item.findtext(
                    "pubDate",
                    default="",
                )
                .strip()
            )

            if not title or not link:
                continue

            # --------------------------------------------------
            # Only recent items.
            # Keep a generous 36-hour window so a temporary
            # feed delay does not make the workflow empty.
            # --------------------------------------------------

            if pub:

                try:
                    dt = email.utils.parsedate_to_datetime(
                        pub
                    )

                    age = (
                        now
                        - dt.timestamp()
                    )

                    if age > 36 * 3600:
                        continue

                except Exception:
                    pass

            # --------------------------------------------------
            # Local dedupe.
            # --------------------------------------------------

            title_key = (
                " ".join(
                    title.lower().split()
                )
            )

            title_key = title_key[:220]

            url_key = (
                link
                .split("#", 1)[0]
                .rstrip("/")
                .lower()
            )

            if title_key in seen_titles:
                continue

            if url_key in seen_urls:
                continue

            seen_titles.add(title_key)
            seen_urls.add(url_key)

            # Compact evidence only.
            # Strip excessive RSS description text.
            clean_description = (
                " ".join(
                    description.split()
                )
            )[:350]

            results.append({
                "title": title[:150],
                "url": link[:450],
                "content": clean_description[:220],
                "source": source_name,
            })

    if not results:
        raise RuntimeError(
            "No recent RSS stories available"
        )

    # --------------------------------------------------
    # SOURCE BALANCE
    #
    # Do not let one publisher dominate the evidence.
    # This is local filtering, so it costs 0 tokens.
    # --------------------------------------------------

    source_counts = {}
    balanced = []

    for item in results:
        source = item.get(
            "source",
            "unknown"
        )

        count = source_counts.get(
            source,
            0
        )

        # Maximum 4 items from any one publisher.
        if count >= 4:
            continue

        source_counts[source] = count + 1
        balanced.append(item)

    # Maximum 12 compact evidence items.
    return balanced[:9]






def call_gemini(results):
    """
    ONE very small Gemini Flash-Lite call.

    Deliberately uses plain text instead of JSON because
    this is more robust and avoids JSON escaping/truncation.
    """

    if not Path(CREDENTIALS).is_file():
        raise RuntimeError(
            "Vertex credential file not found: "
            + CREDENTIALS
        )

    os.environ[
        "GOOGLE_APPLICATION_CREDENTIALS"
    ] = CREDENTIALS

    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location="global",
    )

    evidence = []

    for i, item in enumerate(
        results,
        start=1,
    ):
        evidence.append(
            f"SOURCE_ID={i}\n"
            f"PUBLISHER={item.get('source', '')}\n"
            f"TITLE={item.get('title', '')}\n"
            f"SNIPPET={item.get('content', '')}"
        )

    evidence_text = "\n\n".join(evidence)

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    prompt = f"""
Crypto newsroom editor.

DATE: {today}

Using ONLY the supplied evidence, select exactly FIVE
important DISTINCT current crypto/blockchain stories.

Return exactly FIVE blocks using this exact plain-text format:

STORY 1
HEADLINE: short factual headline, maximum 9 words
SUMMARY: exactly 2 short factual sentences
CATEGORY: short category
ASSET: main asset/company/protocol or N/A
TAGS: exactly 3 short comma-separated tags
SOURCE_ID: integer 1-9
VISUAL: 18-25 words describing the specific visual event
END

Then STORY 2, STORY 3, STORY 4, STORY 5.

Rules:

Do not use JSON.
Do not use markdown.
Do not use quotation marks.
Do not explain reasoning.
Do not ask questions.
Do not research again.
Do not invent facts.
Do not invent sources.
Do not duplicate the same event.

VISUAL must describe the actual story.
Do not include text, letters, numbers, logos, branding,
watermarks, typography, charts, UI or labels in VISUAL.

SOURCE_ID must correspond to the supplied evidence.

SUPPLIED EVIDENCE:

{evidence_text}
"""

    print(
        "TOP5 DIRECT: ONE Gemini Flash-Lite call...",
        flush=True,
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
        config=types.GenerateContentConfig(
            # No structured JSON schema.
            # Plain text is intentionally used for reliability.
            thinking_config=types.ThinkingConfig(
                thinking_budget=0
            ),
            max_output_tokens=850,
            temperature=0.1,
        ),
    )

    raw = (
        response.text
        or ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "Gemini returned empty text"
        )

    # --------------------------------------------------
    # LOCAL PLAIN-TEXT PARSER
    # --------------------------------------------------

    blocks = re.split(
        r"(?m)^STORY\s+[1-5]\s*$",
        raw,
    )

    stories = []

    for block in blocks:

        block = block.strip()

        if not block:
            continue

        block = re.sub(
            r"(?m)^END\s*$",
            "",
            block,
        ).strip()

        fields = {}

        for line in block.splitlines():

            line = line.strip()

            match = re.match(
                r"^(HEADLINE|SUMMARY|CATEGORY|ASSET|TAGS|SOURCE_ID|VISUAL):\s*(.*)$",
                line,
            )

            if not match:
                continue

            key = match.group(1).lower()
            value = match.group(2).strip()

            fields[key] = value

        required = [
            "headline",
            "summary",
            "category",
            "asset",
            "tags",
            "source_id",
            "visual",
        ]

        if not all(
            key in fields
            for key in required
        ):
            continue

        try:
            source_id = int(
                fields["source_id"]
            )
        except ValueError:
            continue

        if not (
            1 <= source_id <= len(results)
        ):
            continue

        tags = [
            x.strip()
            for x in fields["tags"].split(",")
            if x.strip()
        ][:3]

        stories.append({
            "headline":
                fields["headline"],
            "summary":
                fields["summary"],
            "category":
                fields["category"],
            "asset":
                fields["asset"],
            "tags":
                tags,
            "source_id":
                source_id,
            "visual_core":
                fields["visual"],
        })

    if len(stories) != 5:
        raise RuntimeError(
            "Gemini plain-text parser found "
            f"{len(stories)}/5 valid stories"
        )

    # --------------------------------------------------
    # BUILD FINAL STORY DATA
    # --------------------------------------------------

    by_id = {
        i: item
        for i, item in enumerate(
            results,
            start=1,
        )
    }

    final_stories = []

    for story in stories:

        source = by_id[
            story["source_id"]
        ]

        visual_core = (
            story["visual_core"]
            .replace("\n", " ")
            .strip()
        )

        # High-detail prompt is generated LOCALLY.
        # This costs ZERO Vertex output tokens.
        visual_prompt = (
            f"{visual_core}. "
            "Premium global financial-news editorial artwork "
            "with one dominant focal subject directly representing "
            "the verified event. Strong foreground, midground and "
            "background layering with meaningful supporting elements. "
            "Realistic materials, fine micro-textures, polished "
            "surfaces, believable scale, atmospheric depth, "
            "controlled reflections, cinematic directional lighting, "
            "subtle volumetric light, natural rim lighting, deep "
            "realistic shadows, sophisticated restrained color "
            "palette, dramatic but credible mood, high-end financial "
            "journalism aesthetic, cinematic camera perspective, "
            "strong subject separation, careful visual hierarchy, "
            "detailed composition and mobile-friendly framing. "
            "Absolutely no text, letters, numbers, typography, "
            "headlines, captions, labels, logos, brand marks, "
            "watermarks, signage, UI, fake charts, fake statistics, "
            "fake documents or invented branding."
        )

        final_stories.append({
            "headline":
                story["headline"].strip(),
            "summary":
                story["summary"].strip(),
            "category":
                story["category"].strip(),
            "asset":
                story["asset"].strip(),
            "tags":
                story["tags"],
            "source_url":
                source["url"],
            "visual_prompt":
                visual_prompt,
        })

    return {
        "project": PROJECT,
        "stories": final_stories,
    }



def run():
    print(
        "TOP5 DIRECT: searching..."
    )

    results = search_news()

    print(
        f"TOP5 DIRECT: {len(results)} search results"
    )

    print(
        "TOP5 DIRECT: ONE Gemini Flash-Lite call..."
    )

    try:
        payload = call_gemini(
            results
        )
    except Exception as exc:
        print(
            "TOP5 DIRECT GEMINI ERROR:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )
        raise

    print(
        "TOP5 DIRECT: Gemini response OK"
    )

    # The existing local pipeline handles:
    # FLUX generation
    # exact logo
    # exact headline
    # Telegram delivery
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".json",
        prefix="top5_",
        delete=False,
    ) as f:

        json.dump(
            payload,
            f,
            ensure_ascii=False,
        )

        temp_path = f.name

    try:

        print(
            "TOP5 DIRECT: starting image pipeline..."
        )

        completed = subprocess.run(
            [
                str(
                    HOME
                    / "hermes-agent"
                    / "venv"
                    / "bin"
                    / "python"
                ),
                str(FLUX_SCRIPT),
                temp_path,
            ],
            env={
                **os.environ,
                "TOP5_PROJECT": PROJECT,
                "LOCAL_FLUX_BASE_URL":
                    ENV.get(
                        "LOCAL_FLUX_BASE_URL",
                        "",
                    ),
                "TELEGRAM_BOT_TOKEN":
                    TELEGRAM_TOKEN,
            },
            timeout=1800,
        )

        if completed.returncode != 0:
            raise RuntimeError(
                f"Image/Telegram pipeline "
                f"failed with code "
                f"{completed.returncode}"
            )

        print(
            "TOP5 DIRECT: COMPLETE"
        )

    finally:

        try:
            Path(temp_path).unlink()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        print(
            "TOP5 DIRECT ERROR:",
            type(exc).__name__,
            str(exc),
            file=sys.stderr,
        )
        raise
