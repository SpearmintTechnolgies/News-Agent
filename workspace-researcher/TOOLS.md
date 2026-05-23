# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

## What Goes Here

Things like:

- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific

## Examples

```markdown
### Cameras

- living-room → Main area, 180° wide angle
- front-door → Entrance, motion-triggered

### SSH

- home-server → 192.168.1.100, user: admin

### TTS

- Preferred voice: "Nova" (warm, slightly British)
- Default speaker: Kitchen HomePod
```

## Why Separate?

Skills are shared. Your setup is yours. Keeping them apart means you can update skills without losing your notes, and share skills without leaking your infrastructure.

## RSS sources (Scout Step 1)

Scout fetches **8 feeds** for broad cryptocurrency coverage (not Bitcoin-only). Verify anytime:

```bash
bash ~/.openclaw/workspace-researcher/skills/research/verify_feeds.sh
```

| Source | URL |
|--------|-----|
| CoinDesk | `https://www.coindesk.com/arc/outboundfeeds/rss/` |
| CoinTelegraph | `https://cointelegraph.com/rss` |
| Decrypt | `https://decrypt.co/feed` |
| Google News | Broad OR query (see `SOUL.md` Step 1); excludes meme tickers (`-dogecoin -memecoin -shiba -pepe`, etc.) |
| The Block | `https://www.theblock.co/rss.xml` — if curl gets 403/Cloudflare, use **BeInCrypto** `https://beincrypto.com/feed/` instead |
| CryptoSlate | `https://cryptoslate.com/feed/` |
| CryptoFox Markets | `https://cryptofox.news/rss/markets/` |
| CryptoFox Regulation | `https://cryptofox.news/rss/regulation/` |

**Selection policy:** Prefer regulation, ETFs, hacks, major L1/L2 (ETH, SOL, XRP, etc.). Deprioritize meme-coin-only headlines unless cross-covered by major outlets. See `SOUL.md` Step 2.

## Article History Tool

Use this tool to prevent publishing duplicate topics. You must check a candidate URL before fully researching it.

**Path:** `~/.openclaw/workspace-researcher/skills/history/article_history.sh`

**Usage:**
```bash
bash ~/.openclaw/workspace-researcher/skills/history/article_history.sh check "https://coindesk.com/example"
```
- Returns `EXISTS` if it has been published in the last 7 days.
- Returns `NOT_FOUND` if it's safe to use.

---

Add whatever helps you do your job. This is your cheat sheet.
