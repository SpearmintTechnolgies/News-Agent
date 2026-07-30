 the system # TOOLS.md — Pixel's Environment Config

This file contains the specific setup details for your image generation environment. Auth and GCP credentials live in **Bifrost** (`gemini-vertex` key); the skill script has no secrets embedded.

---

## Image Generation — Vertex Imagen via Bifrost

| Key | Value |
|---|---|
| **Bifrost Base URL** | `http://172.30.176.1:8888/v1` |
| **Primary Model** | `vertex/imagen-4.0-fast-generate-001` (~5–8s) |
| **Quality Fallback** | `vertex/imagen-4.0-generate-001` |
| **Image Size** | `1024 × 576` (16:9 landscape, editorial format) |
| **Output Path** | `/tmp/crypto-feature.jpg` |
| **Result File** | `/tmp/<slug>-image-result.txt` (contains path on success; legacy mirror `/tmp/image-result.txt`) |
| **Error Log** | `/tmp/<slug>-image-error.log` (contains reason on failure; legacy mirror `/tmp/image-error.log`) |

GCP project/region (for reference): see `openclaw.json` env (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=us-central1`).

---

## Brand Assets

Per-project watermark logos (relative to `~/.openclaw/`, set in `projects/<slug>.json` → `creator.logo_path`):

| Project | Config key | Default path |
|---|---|---|
| **Coinography** | `creator.logo_path` | `assets/logo.png` |
| **MemeCoinist** | `creator.logo_path` | `assets/logo-memecoinist.png` |

`generate.sh` resolves the logo from the active project config. If `STAMP_LOGO=1` (default) and the configured logo is missing, generation **fails** with a `WATERMARK:` error.

---

## Skill Script

The hardened bash skill that handles all API interaction, retries, and validation:

```
~/.openclaw/workspace-creator/skills/generate-image/generate.sh
```

**Usage:**
```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "<YOUR CRAFTED PROMPT>"
```

- Exit `0` → success → read `/tmp/<slug>-image-result.txt` for the file path (or use the `OUTPUT_PATH` you passed)
- Exit `1` → failure → read `/tmp/<slug>-image-error.log` for the reason

---

## OpenClaw Workspace

| Item | Path |
|---|---|
| **This workspace** | `~/.openclaw/workspace-creator/` |
| **Agent sessions** | `~/.openclaw/agents/creator/sessions/` |
