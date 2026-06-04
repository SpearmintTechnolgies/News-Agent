# Skill: generate-image

## What This Skill Does
Generates a photorealistic editorial feature image using **Vertex Imagen 4** via the local **Bifrost** gateway, saves it locally, and optionally watermarks it with the brand logo.

## How to Invoke

```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "<YOUR PROMPT>"
```

**Example:**
```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "Medium shot of focused male trader at multi-screen desk, Bitcoin chart on background monitor, dark professional office, editorial press photography, Reuters style, photorealistic, DSLR, 35mm lens, sharp focus, candid"
```

## Environment Variables (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `BIFROST_BASE_URL` | `http://172.30.176.1:8888/v1` | Bifrost OpenAI-compatible API base |
| `IMAGE_MODEL` | `vertex/imagen-4.0-fast-generate-001` | Primary (fast, ~5–8s) |
| `IMAGE_MODEL_FALLBACK` | `vertex/imagen-4.0-generate-001` | Quality fallback if primary fails |
| `STAMP_LOGO` | `1` | Set `0` to skip Coinography logo composite |

## Return Values

| Exit Code | Meaning | Where to Read Result |
|-----------|---------|----------------------|
| `0` (success) | Image generated and saved | `cat /tmp/image-result.txt` |
| `1` (failure) | Something went wrong | `cat /tmp/image-error.log` |

## What the Script Handles Automatically
- Bifrost `POST /v1/images/generations` → Vertex Imagen 4
- Primary model then fast fallback per retry round
- Editorial negative constraints appended to prompt (no text/watermark/illustration)
- Up to 3 retry rounds with exponential backoff
- Single-response base64 decode (no async polling or CDN download)
- Symlink-safe write to `/tmp/crypto-feature.jpg`
- Optional logo stamp (100px, bottom-right)
- Expect **5–10 seconds** per successful generation (Imagen 4 Fast primary)

## What YOU Must Do (Your Only Job)
1. Read the article title and topic from the message you received.
2. Craft a **medium-shot** editorial prompt using the rules in your SOUL.md.
3. Call this script with that prompt.
4. Read the result:
   - If exit code 0: Return `cat /tmp/image-result.txt`
   - If exit code 1: Return `IMAGE_FAILED: ` + `cat /tmp/image-error.log`

## Important Notes
- The prompt must be under 1000 characters.
- Do NOT include any text, logos, or written words in your prompt description.
- The image will always be saved to `/tmp/crypto-feature.jpg` (1024×576 resolution).
- Do NOT call Bifrost or Vertex directly — always use this script.
