# Skill: generate-image

## What This Skill Does
Generates a photorealistic editorial feature image using **Vertex Nano Banana** (Gemini image models), with Pollinations FLUX as last-resort fallback. Saves a 16:9 JPEG and optionally watermarks it with the brand logo.

## How to Invoke

```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "<YOUR PROMPT>"
```

**Example:**
```bash
OUTPUT_PATH="$RUN_DIR/media/feature.jpg" PROJECT_SLUG="coinnetwork" \
IMAGE_HEADLINE="Robinhood Turns to Bitstamp as UK Crypto Trading Begins" \
REFERENCE_IMAGE="$RUN_DIR/media/source.jpg" \
  bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "Remix the source news photo. Same subject and setting, fresh 16:9 cinematic editorial."
```

## Environment Variables (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `IMAGE_MODEL` | `vertex/gemini-3.1-flash-lite-image` | Nano Banana 2 Lite (primary) |
| `IMAGE_MODEL_FALLBACK1` | `vertex/gemini-2.5-flash-image` | Legacy Nano Banana |
| `IMAGE_MODEL_FALLBACK2` | `vertex/gemini-3.1-flash-image` | Nano Banana 2 (slower) |
| `IMAGE_MODEL_FALLBACK3` | `pollinations/flux-realism` | Last-resort fallback |
| `OUTPUT_PATH` | `/tmp/crypto-feature.jpg` | Save path (orchestrator sets per run) |
| `PROJECT_CONFIG` | — | Project JSON path (resolves `creator.logo_path`) |
| `PROJECT_SLUG` | `crypto` | Per-run slug for result/error file names |
| `STAMP_LOGO` | `1` | Set `0` to skip logo composite |
| `REFERENCE_IMAGE` | auto `media/source.jpg` | Source story hero to remix |
| `IMAGE_HEADLINE` | — | Story headline when remixing |

Auth comes from `~/.openclaw/gcp/.env.vertex` (`GOOGLE_CLOUD_API_KEY`, `VERTEX_PROJECT_ID`). Do not pass keys on the command line.

## Return Values

| Exit Code | Meaning | Where to Read Result |
|-----------|---------|----------------------|
| `0` (success) | Image generated, watermarked, saved | `cat /tmp/<slug>-image-result.txt` |
| `1` (failure) | Something went wrong | `cat /tmp/<slug>-image-error.log` |

## What the Script Handles Automatically
- Vertex `generateContent` → Nano Banana 2 Lite, then legacy Nano Banana, then Nano Banana 2
- Pollinations FLUX only if all Vertex models fail
- Editorial negative constraints appended to prompt (no text overlay)
- JPEG 16:9, optional logo stamp
- Writes `${OUTPUT_PATH}.watermarked` marker (required by `publish.sh`)
- Writes `$RUN_DIR/publish/image-cost.json` for Telegram card cost footer
- **Post-stamp validation:** final JPEG must be ≥40 KB with valid JPEG magic bytes

## What YOU Must Do (Your Only Job)
1. Read `HEADLINE`, `SCENE_HINT`, and `SOURCE_IMAGE` from the spawn message.
2. Prompt must use the **source story photo as reference only** (mood, palette, subject). Create a new original image. Not a copy of the source. Not a generic 3D coin.
3. Call this script with `OUTPUT_PATH`, `PROJECT_CONFIG`, `PROJECT_SLUG`, `IMAGE_HEADLINE`, and `REFERENCE_IMAGE` when SOURCE_IMAGE is set. `generate.sh` also auto-uses `$RUN_DIR/media/source.jpg`.
4. Return the `SAVE_TO` path on exit 0, or `IMAGE_FAILED:` + error log on exit 1.

## Important Notes
- The prompt must be under 1000 characters.
- Do NOT request readable text overlays, headlines, or typography in the image.
- Do NOT call Vertex or Pollinations directly — always use this script.

---

## Scene Reference

**Source photo wins.** If `SOURCE_IMAGE` / `media/source.jpg` exists, remix that image. Use the tables below only when there is no source photo.

Read this when crafting a prompt (kept here instead of SOUL so it is loaded only when needed).

### Scene Templates (pick closest match)

| Story Type | Scene to Describe |
|---|---|
| Price movement up (BTC/ETH/SOL rally) | Photorealistic 3D gold coin for the relevant asset on black reflective surface, green candlestick chart glow in background, warm golden studio lighting |
| Price movement down / crash / liquidations | Same asset as 3D coin, dramatic red backlight, bearish red candlestick chart dominating background, dark cinematic mood |
| Regulation / government (country-specific) | Country flag fills background, relevant crypto coin or symbol as 3D render in foreground, single ray of studio light |
| Fork / protocol upgrade / L1-L2 news | Abstract glowing 3D blockchain cube or hexagonal network node cluster, electric blue-teal glow, deep black space background |
| ETF / institutional / BlackRock / Fidelity | Brand logo or wordmark as focal element on clean dark background, gold Bitcoin or Ethereum coin beside it, dramatic accent lighting |
| Exchange news (Binance, Coinbase, Kraken) | Exchange brand color accent, app interface or exchange symbol on phone screen close-up, dark background — no human hands unless unavoidable |
| DeFi exploit / hack / security breach | Broken neon padlock or shattered glowing shield, red-orange glow, dark cyberpunk circuit board texture background |
| Stablecoin / payments (Mastercard, Visa, SWIFT) | Physical payment card or dollar-stablecoin 3D coin close-up, clean dark product shot, subtle brand color accent |
| General crypto adoption / market overview | Cluster of photorealistic gold crypto coins (BTC, ETH, SOL) on dark reflective surface, warm studio lighting |
| Memecoin specific | 3D render of the memecoin character or logo (Doge, Pepe, Shiba) against neon electric background, bold vibrant colors |

### Category slug → scene hint

The article's `category` field (from `validated.json`) is a **WordPress category slug**. Map it to the closest scene template above:

| Category slug | Scene template to use |
|---|---|
| `bitcoin` | Price movement — gold Bitcoin 3D coin |
| `ethereum`, `ethlatest-news` | Price movement — Ethereum 3D coin (silver/blue) |
| `xrp`, `ripple` | Price movement — XRP 3D coin |
| `altcoin`, `bnb-chain` | General market / relevant altcoin 3D coin |
| `etf` | ETF / institutional — brand logo + gold coin |
| `policy-and-regulations`, `sec`, `sec-vs-cryptocurrency`, `politics` | Regulation — flag backdrop + coin/symbol |
| `exploits` | DeFi exploit / hack — broken neon padlock/shield |
| `defi`, `dapp`, `dao`, `decentralized`, `blockchain` | Fork / protocol — glowing 3D blockchain cube/network |
| `adoption` | General adoption — coin cluster |
| `cryptomarket-news`, `cryptomarket-analysis` | Price movement / market overview |
| `nfts` | Abstract digital art — glowing NFT/artwork motif |
| `ai` | Abstract digital — neural/AI network glow + coin |
| `airdrop` | General market — coins with motion/giveaway energy |

If the slug is unknown, free-text match on the headline, then use the Universal Fallback in `SOUL.md`.
