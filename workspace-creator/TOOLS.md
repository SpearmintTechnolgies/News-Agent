# TOOLS.md — Pixel's Environment Config

This file contains the specific setup details for your image generation environment. The `generate.sh` skill script reads its credentials from its own embedded config, but these values are here for your reference and for any direct curl calls.

---

## Image Generation — Leonardo AI

| Key | Value |
|---|---|
| **API Base URL** | `https://cloud.leonardo.ai/api/rest/v1` |
| **API Key** | `dddd08ff-d8c3-4fec-98d9-9e8c060f4619` |
| **Model ID** | `b2614463-296c-462a-9586-aafdb8f00e36` |
| **Image Size** | `1024 × 576` (16:9 landscape, editorial format) |
| **Output Path** | `/tmp/crypto-feature.jpg` |
| **Result File** | `/tmp/image-result.txt` (contains path on success) |
| **Error Log** | `/tmp/image-error.log` (contains reason on failure) |

---

## Brand Assets

| Asset | Path |
|---|---|
| **Logo** | `~/.openclaw/assets/logo.png` |

---

## Skill Script

The hardened bash skill that handles all API interaction, retries, polling, and validation:

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

