# TOOLS.md — Pixel's Environment Config

This file contains the specific setup details for your image generation environment. Auth and GCP credentials live in **Bifrost** (`gemini-vertex` key); the skill script has no secrets embedded.

---

## Image Generation — Vertex Imagen via Bifrost

| Key | Value |
|---|---|
| **Bifrost Base URL** | `http://YOUR_BIFROST_HOST:8888/v1` |
| **Primary Model** | `vertex/imagen-4.0-fast-generate-001` (~5–8s) |
| **Quality Fallback** | `vertex/imagen-4.0-generate-001` |
| **Image Size** | `1024 × 576` (16:9 landscape, editorial format) |
| **Output Path** | `/tmp/crypto-feature.jpg` |
| **Result File** | `/tmp/image-result.txt` (contains path on success) |
| **Error Log** | `/tmp/image-error.log` (contains reason on failure) |

GCP project/region (for reference): see `openclaw.json` env (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION=us-central1`).

---

## Brand Assets

| Asset | Path |
|---|---|
| **Logo** | `~/.openclaw/assets/logo.png` |

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

- Exit `0` → success → read `/tmp/image-result.txt` for the file path
- Exit `1` → failure → read `/tmp/image-error.log` for the reason

---

## OpenClaw Workspace

| Item | Path |
|---|---|
| **This workspace** | `~/.openclaw/workspace-creator/` |
| **Agent sessions** | `~/.openclaw/agents/creator/sessions/` |
