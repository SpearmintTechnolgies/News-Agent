# TOOLS.md — Pixel

All runtime instructions live in **SOUL.md**. This file is intentionally minimal to save tokens.

| Item | Path |
|---|---|
| Image skill | `~/.openclaw/workspace-creator/skills/generate-image/generate.sh` |
| Result (success) | `/tmp/<slug>-image-result.txt` |
| Error log | `/tmp/<slug>-image-error.log` |
| Watermark marker | `<SAVE_TO>.watermarked` (written by generate.sh) |

Logo path is resolved from `PROJECT_CONFIG` → `creator.logo_path` inside `generate.sh`.
