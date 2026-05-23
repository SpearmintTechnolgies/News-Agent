# Skill: generate-image

## What This Skill Does
Generates a photorealistic editorial feature image using the Leonardo AI API (PhotoReal v2 + Vision XL + STOCK_PHOTO), downloads it locally, and optionally watermarks it with the brand logo.

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
| `LEONARDO_API_KEY` | (built-in fallback) | Leonardo bearer token |
| `STAMP_LOGO` | `1` | Set `0` to skip Coinography logo composite |
| `USE_PHOTOREAL` | `1` | Set `0` to force non-PhotoReal fallback |
| `LEONARDO_MODEL_ID` | Vision XL | Override Leonardo model UUID |
| `LEONARDO_PRESET_STYLE` | `STOCK_PHOTO` | PhotoReal preset style |

## Return Values

| Exit Code | Meaning | Where to Read Result |
|-----------|---------|----------------------|
| `0` (success) | Image generated and saved | `cat /tmp/image-result.txt` |
| `1` (failure) | Something went wrong | `cat /tmp/image-error.log` |

## What the Script Handles Automatically
- Leonardo Vision XL + PhotoReal v2 + alchemy + STOCK_PHOTO
- Strong negative prompt (anatomy, text, illustration)
- `enhancePrompt: false` (keeps your scene prompt faithful)
- Fallback to Diffusion XL + CINEMATIC if PhotoReal returns 422
- Retries, polling (up to 60s), symlink-safe download
- Optional logo stamp (100px, bottom-right)

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
- Do NOT call the Leonardo API directly — always use this script.
