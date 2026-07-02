# Skill: generate-image

## What This Skill Does
Generates a photorealistic editorial feature image using **Pollinations.ai** (`flux` primary, `zimage` fallback), saves it locally, and optionally watermarks it with the brand logo.

## How to Invoke

```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "<YOUR PROMPT>"
```

**Example:**
```bash
bash ~/.openclaw/workspace-creator/skills/generate-image/generate.sh "Photorealistic 3D gold Bitcoin coin on black reflective surface, warm studio lighting, red candlestick chart softly glowing in background, cinematic crypto editorial, photorealistic, studio lighting, dark background, sharp focus"
```

## Environment Variables (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `POLLINATIONS_API_KEY` | (from openclaw.json env) | Secret key (`sk_...`) from enter.pollinations.ai — **required** |
| `POLLINATIONS_BASE_URL` | `https://gen.pollinations.ai` | Pollinations API base |
| `IMAGE_MODEL` | `flux` | Primary (~0.00175 pollen/image) |
| `IMAGE_MODEL_FALLBACK` | `zimage` | Fallback if primary fails |
| `OUTPUT_PATH` | `/tmp/crypto-feature.jpg` | Save path (orchestrator sets per run) |
| `PROJECT_CONFIG` | — | Project JSON path (resolves `creator.logo_path`) |
| `PROJECT_SLUG` | `crypto` | Per-run slug for result/error file names |
| `STAMP_LOGO` | `1` | Set `0` to skip logo composite |

## Return Values

| Exit Code | Meaning | Where to Read Result |
|-----------|---------|----------------------|
| `0` (success) | Image generated, watermarked, saved | `cat /tmp/<slug>-image-result.txt` |
| `1` (failure) | Something went wrong | `cat /tmp/<slug>-image-error.log` |

## What the Script Handles Automatically
- Pollinations `GET /image/{prompt}?model=flux&width=1024&height=576` (direct binary response)
- Primary `flux` then `zimage` fallback per retry round
- Editorial negative constraints appended to prompt (no text overlay/humans/clipart)
- Up to 3 retry rounds with exponential backoff
- Symlink-safe write via `OUTPUT_PATH` (default `/tmp/crypto-feature.jpg`)
- Per-project logo stamp from `PROJECT_CONFIG` → `creator.logo_path` (100px, bottom-right)
- Writes `${OUTPUT_PATH}.watermarked` marker (required by `publish.sh`)
- **Post-stamp validation:** final JPEG must be ≥40 KB with valid JPEG magic bytes (same gate as WordPress upload); optional quality re-encode if logo stamp shrinks the file slightly
- Expect **~3–10 seconds** per successful generation

## What YOU Must Do (Your Only Job)
1. Read `HEADLINE`, `CATEGORY`, and `SCENE_HINT` from the orchestrator spawn message (not from files).
2. Craft an **article-specific** editorial prompt using SOUL.md rules.
3. Call this script with `OUTPUT_PATH`, `PROJECT_CONFIG`, and `PROJECT_SLUG` set.
4. Return the `SAVE_TO` path on exit 0, or `IMAGE_FAILED:` + error log on exit 1.

## Important Notes
- The prompt must be under 1000 characters.
- Do NOT request readable text overlays, headlines, or typography in the image — describe symbols and shapes instead (Bitcoin B emblem, brand circles, coin renders).
- The image will always be saved to `/tmp/crypto-feature.jpg` (1024×576 resolution).
- Do NOT call Pollinations or any image API directly — always use this script.

---

## Scene Reference

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
