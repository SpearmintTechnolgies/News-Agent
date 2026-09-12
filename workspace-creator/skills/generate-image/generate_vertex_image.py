#!/usr/bin/env python3
"""Generate one editorial image via Vertex Gemini (Nano Banana).

Reads GOOGLE_CLOUD_API_KEY / VERTEX_PROJECT_ID / VERTEX_LOCATION from the
environment or ~/.openclaw/gcp/.env.vertex. Never prints secrets.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path.home() / ".openclaw" / "gcp" / ".env.vertex"


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("GOOGLE_CLOUD_API_KEY", "VERTEX_PROJECT_ID", "VERTEX_LOCATION"):
        if os.environ.get(k):
            out[k] = os.environ[k].strip()
    return out


def model_id(spec: str) -> str:
    spec = (spec or "").strip()
    if spec.startswith("vertex/"):
        spec = spec.split("/", 1)[1]
    if spec.startswith("google/"):
        spec = spec.split("/", 1)[1]
    return spec


def write_jpeg(raw: bytes, dest: Path) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if raw[:2] == b"\xff\xd8":
        dest.write_bytes(raw)
        return dest.stat().st_size
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(raw))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(dest, format="JPEG", quality=92, optimize=True)
        return dest.stat().st_size
    except Exception:
        dest.write_bytes(raw)
        return dest.stat().st_size


def _reference_part(path: Path) -> dict | None:
    if not path.is_file() or path.stat().st_size < 4096:
        return None
    raw = path.read_bytes()
    mime = "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    elif raw[:4] == b"RIFF":
        mime = "image/webp"
    return {
        "inlineData": {
            "mimeType": mime,
            "data": base64.b64encode(raw).decode("ascii"),
        }
    }


def generate(
    model: str,
    prompt: str,
    out_path: Path,
    timeout: int,
    aspect: str,
    reference: Path | None = None,
) -> int:
    env = load_env()
    api_key = env.get("GOOGLE_CLOUD_API_KEY", "")
    project = env.get("VERTEX_PROJECT_ID", "")
    location = env.get("VERTEX_LOCATION", "global") or "global"
    if not api_key or not project:
        print("TRANSIENT: missing GOOGLE_CLOUD_API_KEY or VERTEX_PROJECT_ID", file=sys.stderr)
        return 2

    mid = model_id(model)
    url = (
        f"https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}"
        f"/publishers/google/models/{mid}:generateContent"
    )
    parts: list[dict] = []
    ref_part = _reference_part(reference) if reference else None
    if ref_part:
        parts.append(ref_part)
        parts.append({"text": prompt})
    else:
        parts.append({"text": prompt})
    body = {
        "contents": {"role": "user", "parts": parts},
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {"aspectRatio": aspect},
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:400]
        print(f"TRANSIENT: vertex HTTP {e.code} {err}", file=sys.stderr)
        return 2
    except TimeoutError:
        print(f"TRANSIENT: vertex timeout after {timeout}s", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"TRANSIENT: vertex error {type(e).__name__}: {e}", file=sys.stderr)
        return 2

    parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    for part in parts:
        inline = part.get("inlineData") or part.get("inline_data") or {}
        data = inline.get("data")
        if not data:
            continue
        raw = base64.b64decode(data)
        if len(raw) < 4096:
            print(f"TRANSIENT: image too small ({len(raw)} bytes)", file=sys.stderr)
            return 2
        size = write_jpeg(raw, out_path)
        print(f"OK:{size}")
        return 0

    finish = ((payload.get("candidates") or [{}])[0].get("finishReason"))
    print(f"TRANSIENT: no image in response finish={finish}", file=sys.stderr)
    return 2


def main() -> int:
    p = argparse.ArgumentParser(description="Vertex Nano Banana image generate")
    p.add_argument("--model", required=True)
    p.add_argument("--prompt", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--aspect", default="16:9")
    p.add_argument("--reference", default="", help="Source story hero image to remix")
    args = p.parse_args()
    ref = Path(args.reference) if args.reference.strip() else None
    return generate(args.model, args.prompt, Path(args.out), args.timeout, args.aspect, ref)


if __name__ == "__main__":
    raise SystemExit(main())
