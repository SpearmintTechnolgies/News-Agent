#!/usr/bin/env python3
"""
build_creator_input.py — Extract minimal fields for image prompt crafting.

Usage:
    python3 build_creator_input.py --research <validated.json> --output <creator_input.json>

Output (creator_input.json):
    {
      "headline": "XRP Faces Leverage Test As $1.44B ETF Demand Meets Sell-Off",
      "category": "xrp",
      "primary_keyword": "XRP Leverage Reset",
      "project": "coinography",
      "scene_hint": "Price movement — XRP 3D coin"  # derived from category
    }

    Total: ~150 chars vs 7000+ chars of full validated.json
"""

import argparse
import json
import os
import sys

# Minimal category → scene hint mapping (subset for efficiency)
SCENE_HINTS = {
    "bitcoin": "Price movement — gold Bitcoin 3D coin",
    "btc": "Price movement — gold Bitcoin 3D coin",
    "ethereum": "Price movement — Ethereum 3D coin (silver/blue)",
    "eth": "Price movement — Ethereum 3D coin (silver/blue)",
    "ethlatest-news": "Price movement — Ethereum 3D coin (silver/blue)",
    "xrp": "Price movement — XRP 3D coin",
    "ripple": "Price movement — XRP 3D coin",
    "solana": "Price movement — Solana 3D coin (purple gradient)",
    "sol": "Price movement — Solana 3D coin (purple gradient)",
    "altcoin": "General market — relevant altcoin 3D coin",
    "bnb-chain": "General market — BNB coin 3D",
    "etf": "ETF / institutional — brand logo + gold coin",
    "policy-and-regulations": "Regulation — flag backdrop + coin/symbol",
    "sec": "Regulation — US flag backdrop + coin/symbol",
    "sec-vs-cryptocurrency": "Regulation — US flag backdrop + coin/symbol",
    "politics": "Regulation — flag backdrop + coin/symbol",
    "exploits": "DeFi exploit — broken neon padlock/shield",
    "defi": "Protocol — glowing 3D blockchain cube/network",
    "dapp": "Protocol — glowing 3D blockchain cube/network",
    "dao": "Protocol — glowing 3D blockchain cube/network",
    "blockchain": "Protocol — glowing 3D blockchain cube/network",
    "adoption": "General adoption — coin cluster",
    "nfts": "Abstract digital — glowing NFT/artwork motif",
    "ai": "Abstract digital — neural/AI network glow + coin",
    "memecoin": "Memecoin — 3D character/logo (Doge/Pepe/Shiba)",
    "airdrop": "General market — coins with motion energy",
    "cryptomarket-news": "Price movement / market overview",
    "cryptomarket-analysis": "Price movement / market overview",
}


def get_scene_hint(category: str) -> str:
    """Get scene hint from category slug, with fallback."""
    category_lower = (category or "").lower().strip()
    if category_lower in SCENE_HINTS:
        return SCENE_HINTS[category_lower]
    # Try partial match
    for key, hint in SCENE_HINTS.items():
        if key in category_lower or category_lower in key:
            return hint
    return "Universal — gold crypto coin on dark reflective surface"


def main():
    parser = argparse.ArgumentParser(description="Build minimal creator input from research")
    parser.add_argument("--research", required=True, help="Path to validated.json")
    parser.add_argument("--output", required=True, help="Output path for creator_input.json")
    parser.add_argument("--project", default="coinography", help="Project slug")
    args = parser.parse_args()

    # Read validated.json
    try:
        with open(args.research, "r", encoding="utf-8") as f:
            research = json.load(f)
    except Exception as e:
        print(f"ERROR: Failed to read research file: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract only what creator needs
    headline = research.get("primary_headline", "")
    category = research.get("category", "")
    primary_keyword = research.get("primary_keyword", "")

    if not headline or not category:
        print(f"ERROR: Missing required fields (headline={bool(headline)}, category={bool(category)})", file=sys.stderr)
        sys.exit(1)

    # Build minimal output
    creator_input = {
        "headline": headline,
        "category": category,
        "primary_keyword": primary_keyword,
        "project": args.project,
        "scene_hint": get_scene_hint(category),
    }

    # Write output
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(creator_input, f, indent=2)

    print(f"CREATOR_INPUT_BUILT: {len(json.dumps(creator_input))} chars -> {args.output}")
    # Machine-readable lines for orchestrator shell capture
    print(f"HEADLINE: {headline}")
    print(f"CATEGORY: {category}")
    print(f"SCENE_HINT: {creator_input['scene_hint']}")


if __name__ == "__main__":
    main()
