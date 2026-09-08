#!/usr/bin/env python3

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sys
import subprocess
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml
from PIL import Image, ImageDraw, ImageFont
from google import genai
from google.genai import types
from google.oauth2 import service_account


HOME = Path.home() / ".hermes"
CONFIG = HOME / "config.yaml"
ENV_FILE = HOME / ".env"

LOGO = HOME / "branding" / "logo.png"

OUTPUT_DIR = HOME / "image_cache"
TEMP_DIR = HOME / "cache" / "top5"

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
TEMP_DIR.mkdir(parents=True, exist_ok=True)


def load_env():
    env = {}

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            k, v = line.split("=", 1)

            v = v.strip()

            if (
                len(v) >= 2
                and v[0] == v[-1]
                and v[0] in ("'", '"')
            ):
                v = v[1:-1]

            env[k.strip()] = v

    return env


ENV = load_env()

PROJECT_ID = (
    os.environ.get("GOOGLE_CLOUD_PROJECT")
    or ENV.get("GOOGLE_CLOUD_PROJECT")
    or "ayush-api-506812"
)


FLUX_BASE_URL = (
    os.environ.get("LOCAL_FLUX_BASE_URL")
    or ENV.get("LOCAL_FLUX_BASE_URL")
    or ""
).rstrip("/")

TELEGRAM_TOKEN = (
    os.environ.get("TELEGRAM_BOT_TOKEN")
    or ENV.get("TELEGRAM_BOT_TOKEN")
    or ""
)

NANO_BANANA_MODEL = (
    os.environ.get("NANO_BANANA_MODEL")
    or ENV.get("NANO_BANANA_MODEL")
    or "gemini-3.1-flash-image"
)

NANO_BANANA_IMAGE_SIZE = (
    os.environ.get("NANO_BANANA_IMAGE_SIZE")
    or ENV.get("NANO_BANANA_IMAGE_SIZE")
    or "1K"
)

GOOGLE_CREDENTIALS = (
    os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    or ENV.get("GOOGLE_APPLICATION_CREDENTIALS")
    or str(
        HOME
        / "credentials"
        / "google"
        / "service-account.json"
    )
)

NANO_BANANA_REFERENCE_IMAGE = (
    os.environ.get("NANO_BANANA_REFERENCE_IMAGE")
    or ENV.get("NANO_BANANA_REFERENCE_IMAGE")
    or str(
        HOME
        / "branding"
        / "reference.png"
    )
)


def fail(message: str):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_group_mapping():
    """
    Read project -> Telegram chat ID dynamically from Hermes config.

    The existing config stores each project's PROJECT_SLUG
    in its Telegram group systemPrompt.
    """

    if not CONFIG.exists():
        fail("Hermes config.yaml not found")

    cfg = yaml.safe_load(CONFIG.read_text()) or {}

    groups = (
        cfg
        .get("telegram", {})
        .get("accounts", {})
        .get("news", {})
        .get("groups", {})
    )

    mapping = {}

    for chat_id, data in groups.items():

        if not isinstance(data, dict):
            continue

        prompt = str(data.get("systemPrompt", ""))

        m = re.search(
            r"PROJECT_SLUG\s*=\s*([A-Za-z0-9_-]+)",
            prompt,
        )

        if m:
            mapping[m.group(1).lower()] = str(chat_id)

    return mapping


def get_chat_id(project: str) -> str:
    """
    Verified Telegram destination for the current
    Spearmint News Agent production group.
    """

    return os.environ.get(
        "TOP5_CHAT_ID",
        "-1003736953686",
    )









def fetch_story_reference(
    article_url: str,
    index: int,
) -> Path | None:
    """
    Find the image for THIS exact news story.

    Priority:
      1. Publisher RSS image
      2. Publisher article metadata
      3. Jina reader fallback

    The image is used only as Nano Banana visual reference.
    """

    if not article_url:
        return None

    parsed = urlparse(
        article_url
    )

    host = parsed.netloc.lower()

    title_hint = (
        Path(parsed.path)
        .name
        .replace("-", " ")
        .replace("_", " ")
        .lower()
    )

    rss_candidates = []

    # Publisher RSS feeds.
    if "coindesk.com" in host:
        rss_candidates = [
            "https://www.coindesk.com/arc/outboundfeeds/rss/"
        ]

    elif "cointelegraph.com" in host:
        rss_candidates = [
            "https://cointelegraph.com/rss",
            "https://cointelegraph.com/?format=rss",
        ]

    elif "decrypt.co" in host:
        rss_candidates = [
            "https://decrypt.co/feed",
            "https://decrypt.co/rss",
        ]

    # --------------------------------------------------
    # Helper: download image and verify it.
    # --------------------------------------------------

    def download_image(
        image_url: str,
        label: str,
    ) -> Path | None:
        """
        Download a real story image and normalize it to JPEG.

        This prevents AVIF/WebP bytes from being mislabeled as JPEG,
        which can cause Nano Banana INVALID_ARGUMENT responses.
        """

        if not image_url:
            return None

        image_url = html.unescape(
            image_url.strip()
        )

        image_url = urljoin(
            article_url,
            image_url,
        )

        parsed_image = urlparse(
            image_url
        )

        if parsed_image.scheme not in (
            "http",
            "https",
        ):
            return None

        raw_output = (
            TEMP_DIR
            / f"story_reference_{index}_{label}.download"
        )

        final_output = (
            TEMP_DIR
            / f"story_reference_{index}_{label}.jpg"
        )

        try:

            cmd = [
                "curl",
                "-L",
                "-f",
                "-sS",
                "--max-time",
                "30",
                "-A",
                "Mozilla/5.0",
                "-H",
                "Accept: image/avif,image/webp,image/apng,"
                "image/svg+xml,image/*,*/*;q=0.8",
                "-o",
                str(raw_output),
                image_url,
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=35,
            )

            if result.returncode != 0:
                raw_output.unlink(
                    missing_ok=True
                )
                return None

            if not raw_output.exists():
                return None

            if raw_output.stat().st_size < 10_000:
                raw_output.unlink(
                    missing_ok=True
                )
                return None

            # Decode whatever the publisher actually returned.
            with Image.open(
                raw_output
            ) as source_image:

                width, height = source_image.size

                if width < 600 or height < 300:
                    raw_output.unlink(
                        missing_ok=True
                    )
                    return None

                source_image = source_image.convert(
                    "RGB"
                )

                # Always save real JPEG bytes.
                source_image.save(
                    final_output,
                    "JPEG",
                    quality=95,
                    optimize=True,
                    progressive=True,
                )

                source_format = (
                    source_image.format
                    or "UNKNOWN"
                )

            raw_output.unlink(
                missing_ok=True
            )

            # Re-open and verify the resulting file.
            with Image.open(
                final_output
            ) as verify_image:

                verify_width, verify_height = (
                    verify_image.size
                )

                verify_format = (
                    verify_image.format
                )

                if verify_format != "JPEG":
                    final_output.unlink(
                        missing_ok=True
                    )
                    return None

            print(
                f"STORY {index}: reference image loaded",
                flush=True,
            )

            print(
                f"STORY {index}: normalized "
                f"{verify_width}x{verify_height} JPEG",
                flush=True,
            )

            print(
                f"STORY {index}: reference={final_output}",
                flush=True,
            )

            return final_output

        except Exception as exc:

            raw_output.unlink(
                missing_ok=True
            )

            final_output.unlink(
                missing_ok=True
            )

            print(
                f"STORY {index}: image normalization failed: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

            return None


    # --------------------------------------------------
    # 1. RSS-FIRST
    # --------------------------------------------------

    for feed_url in rss_candidates:

        try:

            req = Request(
                feed_url,
                headers={
                    "User-Agent":
                        "Mozilla/5.0 "
                        "(X11; Linux x86_64) "
                        "AppleWebKit/537.36 "
                        "Chrome/131 Safari/537.36",
                    "Accept":
                        "application/rss+xml,"
                        "application/atom+xml,"
                        "application/xml,text/xml,*/*;q=0.8",
                },
                method="GET",
            )

            with urlopen(
                req,
                timeout=20,
            ) as response:

                raw_feed = response.read(
                    6_000_000
                )

            root = ET.fromstring(
                raw_feed
            )

        except Exception:
            continue

        ns = {
            "media":
                "http://search.yahoo.com/mrss/",
            "content":
                "http://purl.org/rss/1.0/modules/content/",
            "atom":
                "http://www.w3.org/2005/Atom",
        }

        entries = []

        # RSS items.
        entries.extend(
            root.findall(
                ".//item"
            )
        )

        # Atom entries.
        entries.extend(
            root.findall(
                ".//atom:entry",
                ns,
            )
        )

        for entry in entries:

            def text_of(
                tag: str,
            ) -> str:

                node = entry.find(
                    tag,
                    ns,
                )

                return (
                    (node.text or "").strip()
                    if node is not None
                    else ""
                )

            item_link = text_of(
                "link"
            )

            # Atom link href.
            if not item_link:

                node = entry.find(
                    "atom:link",
                    ns,
                )

                if node is not None:
                    item_link = (
                        node.attrib.get(
                            "href",
                            "",
                        )
                        .strip()
                    )

            item_title = text_of(
                "title"
            )

            normalized_article_url = (
                article_url.rstrip("/")
                .lower()
            )

            normalized_item_link = (
                item_link.rstrip("/")
                .lower()
            )

            link_match = (
                normalized_article_url
                == normalized_item_link
            )

            # URL can differ by tracking parameters.
            if (
                not link_match
                and normalized_item_link
                and (
                    normalized_article_url
                    in normalized_item_link
                    or normalized_item_link
                    in normalized_article_url
                )
            ):
                link_match = True

            # Title/path similarity fallback.
            normalized_title = re.sub(
                r"[^a-z0-9]+",
                " ",
                item_title.lower(),
            ).strip()

            normalized_hint = re.sub(
                r"[^a-z0-9]+",
                " ",
                title_hint,
            ).strip()

            title_match = (
                normalized_hint
                and len(normalized_hint) > 12
                and (
                    normalized_hint in normalized_title
                    or normalized_title in normalized_hint
                )
            )

            if not (
                link_match
                or title_match
            ):
                continue

            candidates = []

            # media:content / media:thumbnail
            for tag in (
                "media:content",
                "media:thumbnail",
            ):

                for node in entry.findall(
                    tag,
                    ns,
                ):

                    image_url = node.attrib.get(
                        "url"
                    )

                    if image_url:
                        candidates.append(
                            image_url
                        )

            # enclosure
            for node in entry.findall(
                "enclosure"
            ):

                image_url = node.attrib.get(
                    "url"
                )

                content_type = (
                    node.attrib.get(
                        "type",
                        "",
                    )
                    .lower()
                )

                if (
                    image_url
                    and (
                        "image/" in content_type
                        or image_url.lower().endswith(
                            (
                                ".jpg",
                                ".jpeg",
                                ".png",
                                ".webp",
                            )
                        )
                    )
                ):
                    candidates.append(
                        image_url
                    )

            # Atom enclosure links.
            for node in entry.findall(
                "atom:link",
                ns,
            ):

                rel = node.attrib.get(
                    "rel",
                    "",
                )

                if rel != "enclosure":
                    continue

                image_url = node.attrib.get(
                    "href"
                )

                if image_url:
                    candidates.append(
                        image_url
                    )

            # content:encoded may contain the lead image.
            encoded = text_of(
                "content:encoded"
            )

            if encoded:

                candidates.extend(
                    re.findall(
                        r'<img[^>]+src=["\']([^"\']+)',
                        encoded,
                        flags=re.I,
                    )
                )

            # de-duplicate.
            unique = []

            for candidate in candidates:

                candidate = html.unescape(
                    candidate
                )

                if candidate not in unique:
                    unique.append(
                        candidate
                    )

            for image_number, image_url in enumerate(
                unique,
                start=1,
            ):

                result = download_image(
                    image_url,
                    f"{index}_{image_number}",
                )

                if result:
                    print(
                        f"STORY {index}: source=RSS",
                        flush=True,
                    )
                    return result

    # --------------------------------------------------
    # 2. DIRECT ARTICLE METADATA FALLBACK
    # --------------------------------------------------

    try:

        req = Request(
            article_url,
            headers={
                "User-Agent":
                    "Googlebot/2.1 "
                    "(compatible; news reference fetch)",
                "Accept":
                    "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            method="GET",
        )

        with urlopen(
            req,
            timeout=18,
        ) as response:

            page = response.read(
                1_500_000
            ).decode(
                "utf-8",
                errors="ignore",
            )

        meta_candidates = []

        meta_candidates.extend(
            re.findall(
                r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                page,
                flags=re.I,
            )
        )

        meta_candidates.extend(
            re.findall(
                r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                page,
                flags=re.I,
            )
        )

        meta_candidates.extend(
            re.findall(
                r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
                page,
                flags=re.I,
            )
        )

        for image_number, image_url in enumerate(
            meta_candidates,
            start=1,
        ):

            result = download_image(
                image_url,
                f"meta_{image_number}",
            )

            if result:
                print(
                    f"STORY {index}: source=ARTICLE_METADATA",
                    flush=True,
                )
                return result

    except HTTPError as exc:

        print(
            f"STORY {index}: article returned HTTP {exc.code}; "
            "continuing without article fetch",
            flush=True,
        )

    except Exception as exc:

        print(
            f"STORY {index}: article image fallback failed: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

    # --------------------------------------------------
    # 3. NO STORY IMAGE FOUND
    # --------------------------------------------------

    print(
        f"STORY {index}: no story-specific image found",
        flush=True,
    )

    return None



def nano_banana_generate(
    prompt: str,
    story_reference: Path | None = None,
) -> bytes:
    """
    Generate one original image with Nano Banana 2.

    Inputs:
      - optional story-specific real-news reference image
      - story-specific visual prompt

    The exact MemeCoinist logo and headline are NEVER sent to
    Nano Banana. They are applied locally afterward.
    """

    cred_path = Path(
        GOOGLE_CREDENTIALS
    )

    if not cred_path.exists():
        raise RuntimeError(
            "Google credentials missing: "
            + str(cred_path)
        )

    os.environ[
        "GOOGLE_APPLICATION_CREDENTIALS"
    ] = str(cred_path)

    credentials = (
        service_account.Credentials
        .from_service_account_file(
            str(cred_path),
            scopes=[
                "https://www.googleapis.com/auth/cloud-platform"
            ],
        )
    )

    client = genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location="global",
        credentials=credentials,
    )

    contents = []

    # --------------------------------------------------
    # GLOBAL MEMECOINIST STYLE REFERENCE
    # --------------------------------------------------

    global_reference = Path(
        NANO_BANANA_REFERENCE_IMAGE
    )

    if global_reference.exists():

        try:

            global_bytes = (
                global_reference.read_bytes()
            )

            global_mime = "image/png"

            contents.append(
                types.Part.from_bytes(
                    data=global_bytes,
                    mime_type=global_mime,
                )
            )

            print(
                "NANO BANANA: global brand reference loaded",
                flush=True,
            )

        except Exception as exc:

            print(
                "NANO BANANA: global brand reference skipped: "
                + str(exc),
                flush=True,
            )


    # --------------------------------------------------
    # STORY-SPECIFIC REAL NEWS REFERENCE
    # --------------------------------------------------

    if (
        story_reference is not None
        and story_reference.exists()
    ):
        try:
            reference_bytes = (
                story_reference.read_bytes()
            )

            contents.append(
                types.Part.from_bytes(
                    data=reference_bytes,
                    mime_type="image/jpeg",
                )
            )

            print(
                "NANO BANANA: story-specific reference loaded",
                flush=True,
            )

        except Exception as exc:
            print(
                "NANO BANANA: story reference skipped: "
                + str(exc),
                flush=True,
            )

    # --------------------------------------------------
    # FINAL PROMPT
    # --------------------------------------------------

    complete_prompt = f"""

BRAND ART DIRECTION:

Use the global reference image ONLY as the MemeCoinist visual
art-direction reference.

Match its overall:
- clean minimalist presentation
- premium dark editorial feel
- strong single focal subject
- generous negative space
- restrained composition
- sophisticated blue / cyan / purple accent treatment
- polished lighting
- modern financial-media aesthetic
- clear mobile-friendly hierarchy

Do NOT copy:
- its text
- its letters
- its logo
- its exact objects
- its exact composition
- its exact camera angle
- its watermark
- its publisher branding

Create a new original scene specifically for the current news story.

The brand reference controls STYLE.
The story reference controls REAL-WORLD STORY CONTEXT.
The news prompt controls WHAT HAPPENED.

Do not turn every image into a generic crypto graphic.

Create ONE completely new original premium financial-news editorial image.

EXACT NEWS STORY VISUAL:
{prompt}

Use the supplied story image only as a visual reference for the real-world
subject, environment, objects, context and overall visual information.

Do NOT copy the supplied image.
Do NOT trace it.
Do NOT reproduce its exact composition.
Do NOT reproduce its camera framing.
Do NOT reproduce its text.
Do NOT reproduce its logo.
Do NOT reproduce its watermark.
Do NOT reproduce publisher branding.

The final scene must specifically communicate the news story.

MINIMALIST PREMIUM ART DIRECTION:

Use ONE dominant hero subject.

Use only ONE or TWO supporting visual elements.

Keep the composition clean and sophisticated with generous intentional
negative space.

Avoid visual clutter.

Do NOT fill the scene with many coins, random symbols, excessive circuitry,
floating objects, excessive particles, giant data walls, busy trading
screens or unrelated decorative elements.

Every visible object must support the story.

Use selective high-quality detail rather than excessive detail everywhere.

Prioritize:
realistic materials,
subtle metal grain,
controlled reflections,
realistic glass,
natural shadows,
soft atmospheric depth,
fine surface imperfections,
credible scale,
clean silhouettes,
strong focal hierarchy.

Use cinematic directional lighting, subtle rim lighting, realistic shadow
falloff, restrained volumetric atmosphere and natural reflections.

Use a sophisticated dark financial-editorial palette with restrained blue,
graphite, black, white highlights and one story-appropriate accent.

Use cinematic 16:9 composition, realistic lens perspective, natural depth
of field and strong subject separation.

The image should feel like a premium global financial-news publication:
minimalist, elegant, realistic, polished, credible, cinematic and
publication-ready.

DO NOT create generic cryptocurrency artwork.

ABSOLUTE TEXT BAN:

The generated artwork itself MUST contain ZERO text.

Do not generate:
words,
letters,
numbers,
sentences,
headlines,
captions,
titles,
labels,
signage,
logos,
brand names,
watermarks,
UI text,
screen text,
ticker text,
price text,
chart labels,
document text,
fake statistics,
fake writing,
gibberish,
pseudo-writing,
or symbols that resemble writing.

Do not put writing on screens, phones, buildings, documents, coins,
products, clothing, signs, charts, dashboards, interfaces or background
objects.

If the reference contains any text, IGNORE it completely.

Do not generate MemeCoinist branding.

Do not generate the headline.

The exact MemeCoinist logo and exact headline are added LOCALLY after
generation.

Generate ONE clean original visual artwork only.
"""

    contents.append(
        complete_prompt
    )

    # --------------------------------------------------
    # GENERATION HELPER
    # --------------------------------------------------

    def generate_at_size(
        image_size: str,
    ):
        return client.models.generate_content(
            model=NANO_BANANA_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=[
                    "IMAGE"
                ],
                image_config=types.ImageConfig(
                    aspect_ratio="16:9",
                    image_size=image_size,
                ),
            ),
        )

    # --------------------------------------------------
    # PRIMARY: 2K
    # --------------------------------------------------

    response = None
    last_429 = None

    for attempt, wait_seconds in enumerate(
        [0, 5, 15],
        start=1,
    ):

        if wait_seconds:
            print(
                f"NANO BANANA: 2K retry wait {wait_seconds}s...",
                flush=True,
            )
            time.sleep(wait_seconds)

        try:
            print(
                f"NANO BANANA: 2K attempt {attempt}/3...",
                flush=True,
            )

            response = generate_at_size(
                "2K"
            )

            print(
                "NANO BANANA: 2K generation succeeded",
                flush=True,
            )

            last_429 = None
            break

        except Exception as exc:

            text = str(exc)

            if (
                "429" in text
                or "RESOURCE_EXHAUSTED" in text
                or "rate limit" in text.lower()
                or "quota" in text.lower()
            ):

                last_429 = exc

                print(
                    f"NANO BANANA: 2K unavailable "
                    f"(attempt {attempt}/3): 429 RESOURCE_EXHAUSTED",
                    flush=True,
                )

                continue

            raise

    # --------------------------------------------------
    # FALLBACK: 1K
    # --------------------------------------------------

    if response is None:

        print(
            "NANO BANANA: falling back to 1K...",
            flush=True,
        )

        last_1k = None

        for attempt, wait_seconds in enumerate(
            [0, 5, 15],
            start=1,
        ):

            if wait_seconds:
                print(
                    f"NANO BANANA: 1K retry wait {wait_seconds}s...",
                    flush=True,
                )
                time.sleep(wait_seconds)

            try:

                print(
                    f"NANO BANANA: 1K attempt {attempt}/3...",
                    flush=True,
                )

                response = generate_at_size(
                    "1K"
                )

                print(
                    "NANO BANANA: 1K fallback succeeded",
                    flush=True,
                )

                last_1k = None
                break

            except Exception as exc:

                text = str(exc)

                if (
                    "429" in text
                    or "RESOURCE_EXHAUSTED" in text
                    or "rate limit" in text.lower()
                    or "quota" in text.lower()
                ):

                    last_1k = exc

                    print(
                        f"NANO BANANA: 1K unavailable "
                        f"(attempt {attempt}/3): 429 RESOURCE_EXHAUSTED",
                        flush=True,
                    )

                    continue

                raise

        if response is None:
            raise RuntimeError(
                "Nano Banana unavailable at both 2K and 1K"
            ) from last_1k or last_429

    # --------------------------------------------------
    # EXTRACT IMAGE
    # --------------------------------------------------

    if not response.candidates:
        raise RuntimeError(
            "Nano Banana returned no candidates"
        )

    parts = (
        response.candidates[0]
        .content
        .parts
    )

    for part in parts:

        inline = getattr(
            part,
            "inline_data",
            None,
        )

        if inline is not None:

            data = getattr(
                inline,
                "data",
                None,
            )

            if data:
                print(
                    "NANO BANANA: image received",
                    flush=True,
                )

                return bytes(data)

    raise RuntimeError(
        "Nano Banana returned no image data"
    )



def flux_generate(prompt: str) -> bytes:
    """
    Call the local FLUX.2 Klein 9B API and return PNG bytes.
    """

    if not FLUX_BASE_URL:
        raise RuntimeError(
            "LOCAL_FLUX_BASE_URL missing"
        )

    payload = {
        "prompt": str(prompt),
        "size": "1536x1536",
        "num_inference_steps": 8,
        "guidance_scale": 4.0,
        "n": 1,
    }

    req = Request(
        FLUX_BASE_URL.rstrip("/")
        + "/v1/images/generations",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Spearmint-Top5/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=300) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Local FLUX HTTP {exc.code}: {detail[:1000]}"
        )
    except URLError as exc:
        raise RuntimeError(
            f"Local FLUX connection failed: {exc}"
        )

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Local FLUX returned invalid JSON: {exc}"
        )

    data = result.get("data") or []

    if not data:
        raise RuntimeError(
            "Local FLUX returned no image data"
        )

    b64 = data[0].get("b64_json")

    if not b64:
        raise RuntimeError(
            "Local FLUX response missing b64_json"
        )

    try:
        return base64.b64decode(b64)
    except Exception as exc:
        raise RuntimeError(
            f"Invalid FLUX base64: {exc}"
        )



def send_telegram_photo(
    chat_id: str,
    photo_path: Path,
    caption: str,
):
    """
    Reliable Telegram sendPhoto implementation.

    Uses curl multipart upload instead of a hand-built
    multipart body.

    No Gemini.
    No FLUX.
    """

    if not TELEGRAM_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN missing"
        )

    if not photo_path.exists():
        raise RuntimeError(
            f"Telegram image missing: {photo_path}"
        )

    # Telegram captions have a 1024-character limit.
    # Keep the complete beginning of the caption.
    caption = str(caption).strip()

    if len(caption) > 1000:
        caption = caption[:997] + "..."

    cmd = [
        "curl",
        "-fsS",
        "--max-time",
        "90",
        "-X",
        "POST",
        (
            "https://api.telegram.org/bot"
            + TELEGRAM_TOKEN
            + "/sendPhoto"
        ),
        "-F",
        f"chat_id={chat_id}",
        "-F",
        f"photo=@{photo_path};type=image/jpeg",
        "-F",
        f"caption={caption}",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=100,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Telegram sendPhoto execution failed: {exc}"
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Telegram curl failed: "
            + (
                result.stderr.strip()
                or result.stdout.strip()
                or "unknown curl error"
            )
        )

    raw = result.stdout.strip()

    if not raw:
        raise RuntimeError(
            "Telegram returned empty response"
        )

    try:
        response = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(
            "Telegram returned invalid JSON: "
            + raw[:500]
        )

    if not response.get("ok"):
        raise RuntimeError(
            "Telegram sendPhoto rejected request: "
            + str(
                response.get(
                    "description",
                    raw[:500],
                )
            )
        )

    return response



def send_text(
    chat_id: str,
    text: str,
):
    if not TELEGRAM_TOKEN:
        return

    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )

    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
    }).encode("utf-8")

    req = Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(
            req,
            timeout=30,
        ):
            pass
    except Exception:
        pass


def validate_item(item):
    required = [
        "headline",
        "summary",
        "category",
        "asset",
        "tags",
        "source_url",
        "visual_prompt",
    ]

    for field in required:

        if field not in item:
            raise ValueError(
                f"Missing field: {field}"
            )

    url = str(
        item["source_url"]
    ).strip()

    if not url.startswith(
        ("https://", "http://")
    ):
        raise ValueError(
            f"Invalid source URL: {url}"
        )

    headline = str(
        item["headline"]
    ).strip()

    if not headline:
        raise ValueError(
            "Empty headline"
        )

    # Avoid giant AI-generated image text.
    if len(
        headline.split()
    ) > 14:
        raise ValueError(
            "Headline exceeds 14 words"
        )



def apply_logo_and_headline(
    raw_path: Path,
    headline: str,
    index: int,
) -> Path:
    """
    Local deterministic compositor.

    FLUX generates the artwork only.
    This function adds:
      1. exact MemeCoinist transparent logo
      2. exact supplied headline

    No AI generation is involved.
    """

    if not raw_path.exists():
        raise RuntimeError(
            f"Generated image missing: {raw_path}"
        )

    if not LOGO.exists():
        raise RuntimeError(
            f"MemeCoinist logo missing: {LOGO}"
        )

    image = Image.open(raw_path).convert("RGBA")

    # --------------------------------------------------
    # Exact transparent logo.
    # The source PNG already contains alpha.
    # Do NOT create any background rectangle.
    # --------------------------------------------------

    logo = Image.open(LOGO).convert("RGBA")

    # Keep logo visually compact while preserving aspect ratio.
    target_width = max(
        170,
        min(
            250,
            image.width // 5,
        ),
    )

    scale = target_width / logo.width
    target_height = max(
        1,
        int(
            logo.height * scale
        ),
    )

    logo = logo.resize(
        (
            target_width,
            target_height,
        ),
        Image.Resampling.LANCZOS,
    )

    # Top-left placement.
    logo_x = 28
    logo_y = 22

    image.alpha_composite(
        logo,
        (
            logo_x,
            logo_y,
        ),
    )

    # --------------------------------------------------
    # Exact headline.
    # Render locally so spelling is deterministic.
    # No AI typography.
    # --------------------------------------------------

    draw = ImageDraw.Draw(
        image,
        "RGBA",
    )

    headline = " ".join(
        str(headline)
        .split()
    ).strip()

    # Use a system font if available.
    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    font_path = None

    for candidate in font_candidates:
        if Path(candidate).exists():
            font_path = candidate
            break

    if font_path is None:
        raise RuntimeError(
            "No usable headline font found"
        )

    # Dynamic font size for mobile readability.
    font_size = max(
        34,
        min(
            58,
            image.width // 16,
        ),
    )

    font = ImageFont.truetype(
        font_path,
        font_size,
    )

    # --------------------------------------------------
    # Wrap headline locally.
    # Keep it compact and readable.
    # --------------------------------------------------

    max_width = int(
        image.width * 0.84
    )

    words = headline.split()

    lines = []
    current = ""

    for word in words:

        candidate = (
            word
            if not current
            else current + " " + word
        )

        bbox = draw.textbbox(
            (0, 0),
            candidate,
            font=font,
            stroke_width=0,
        )

        width = bbox[2] - bbox[0]

        if (
            width <= max_width
            or not current
        ):
            current = candidate
        else:
            lines.append(current)
            current = word

    if current:
        lines.append(current)

    # Maximum 3 lines.
    lines = lines[:3]

    # --------------------------------------------------
    # Place headline in lower safe area.
    # Subtle shadow only — NO dark box.
    # --------------------------------------------------

    spacing = max(
        8,
        font_size // 5,
    )

    line_heights = []

    for line in lines:
        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font,
            stroke_width=0,
        )
        line_heights.append(
            bbox[3] - bbox[1]
        )

    total_height = (
        sum(line_heights)
        + spacing * max(
            0,
            len(lines) - 1,
        )
    )

    x = 36
    y = image.height - total_height - 38

    for line, line_height in zip(
        lines,
        line_heights,
    ):

        # Very subtle shadow for readability.
        draw.text(
            (
                x + 2,
                y + 2,
            ),
            line,
            font=font,
            fill=(0, 0, 0, 180),
        )

        # Exact readable headline.
        draw.text(
            (
                x,
                y,
            ),
            line,
            font=font,
            fill=(255, 255, 255, 255),
        )

        y += line_height + spacing

    # --------------------------------------------------
    # Save final JPEG.
    # --------------------------------------------------

    final_name = (
        f"memecoinist_{index}_"
        f"{int(time.time() * 1000)}.jpg"
    )

    final_path = (
        OUTPUT_DIR / final_name
    )

    image.convert(
        "RGB"
    ).save(
        final_path,
        "JPEG",
        quality=95,
        optimize=True,
        progressive=True,
    )

    return final_path



def process(payload):
    if not isinstance(
        payload,
        dict,
    ):
        fail(
            "Payload must be a JSON object"
        )

    project = payload.get(
        "project"
    )

    items = payload.get(
        "stories"
    )

    if not project:
        fail("Missing project")

    if not isinstance(
        items,
        list,
    ):
        fail("stories must be a list")

    if len(items) != 5:
        fail(
            f"Expected exactly 5 stories, got {len(items)}"
        )

    if not TELEGRAM_TOKEN:
        fail(
            "TELEGRAM_BOT_TOKEN missing"
        )

    if not LOGO.exists():
        fail(
            f"MemeCoinist logo missing: {LOGO}"
        )

    chat_id = get_chat_id(
        project
    )

    sent = 0

    for index, item in enumerate(
        items,
        start=1,
    ):

        try:
            validate_item(item)

            headline = str(
                item["headline"]
            ).strip()

            summary = str(
                item["summary"]
            ).strip()

            category = str(
                item["category"]
            ).strip()

            asset = str(
                item["asset"]
            ).strip()

            tags = item["tags"]

            if isinstance(
                tags,
                list,
            ):
                tags_text = ", ".join(
                    str(x).strip()
                    for x in tags[:5]
                )
            else:
                tags_text = str(tags)

            source_url = str(
                item["source_url"]
            ).strip()

            visual_prompt = str(
                item["visual_prompt"]
            ).strip()

            # Force no text into FLUX.
            flux_prompt = (
                visual_prompt
                + " "
                "Generate only the visual artwork. "
                "Absolutely no text, letters, numbers, "
                "typography, logos, brand marks, "
                "watermarks, labels, signage, UI, "
                "or fake branding anywhere in the image. "
                "Do not recreate or imitate any logo."
            )

            story_reference = fetch_story_reference(
                source_url,
                index,
            )

            print(
                "STORY REFERENCE MODE:",
                "ARTICLE IMAGE"
                if story_reference is not None
                else "GLOBAL STYLE FALLBACK",
                flush=True,
            )

            # Story-specific article image is preferred.
            # Global reference remains a style fallback.
            raw = nano_banana_generate(
                flux_prompt,
                story_reference,
            )

            raw_path = (
                TEMP_DIR
                / f"raw_{index}_{int(time.time())}.jpg"
            )

            # Nano Banana returns a high-resolution 16:9 image.
            # Downsample locally to the exact production size.
            raw_image = Image.open(
                BytesIO(raw)
            ).convert("RGB")

            raw_image = raw_image.resize(
                (1600, 900),
                Image.Resampling.LANCZOS,
            )

            raw_image.save(
                raw_path,
                "JPEG",
                quality=96,
                optimize=True,
            )

            # LOCAL LOGO ONLY.
            # Headline overlay intentionally DISABLED.
            final_path = apply_logo_and_headline(
                raw_path,
                "",
                index,
            )

            caption = (
                f"**{headline}**\n\n"
                f"{summary}\n\n"
                f"**Category:** {category}\n"
                f"**Asset:** {asset} | "
                f"**Tags:** {tags_text}\n\n"
                f"Source: {source_url}"
            )

            send_telegram_photo(
                chat_id,
                final_path,
                caption,
            )

            sent += 1

            # Telegram flood protection.
            if index < len(items):
                pass

        except Exception as exc:

            # Do not retry expensive generation.
            print(
                f"Story {index} failed: "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

            # Keep processing remaining stories.
            continue

    print(
        f"TOP5_RESULT sent={sent}/5"
    )

    if sent < 5:

        send_text(
            chat_id,
            (
                f"⚠️ /top5 completed "
                f"{sent}/5 stories. "
                f"Some images could not be generated."
            ),
        )

        raise SystemExit(2)


def main():
    if len(sys.argv) > 1:
        path = Path(
            sys.argv[1]
        )
        payload = json.loads(
            path.read_text()
        )
    else:
        payload = json.loads(
            sys.stdin.read()
        )

    process(payload)


if __name__ == "__main__":
    main()
