#!/usr/bin/env python3
import argparse, requests, sys

# MUST be set before any other matplotlib import — required for headless/server use
import matplotlib
matplotlib.use('Agg')

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from datetime import datetime

def generate(coin_id, days, output_path):
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    try:
        res = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"ERROR: Network error fetching CoinGecko data: {e}")
        sys.exit(1)

    if res.status_code != 200:
        print(f"ERROR: CoinGecko returned {res.status_code} for coin '{coin_id}'")
        sys.exit(1)

    data = res.json()
    prices = data.get("prices", [])
    if not prices:
        print(f"ERROR: No price data returned for coin '{coin_id}'")
        sys.exit(1)

    dates  = [datetime.fromtimestamp(p[0] / 1000) for p in prices]
    values = [p[1] for p in prices]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(dates, values, color="#F7931A", linewidth=2)
    ax.fill_between(dates, values, alpha=0.08, color="#F7931A")
    ax.set_title(f"{coin_id.capitalize()} Price — Last {days} Days", fontsize=15, fontweight="bold")
    ax.set_xlabel("Date")
    ax.set_ylabel("Price (USD)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
    plt.xticks(rotation=45)
    ax.grid(True, linestyle="--", alpha=0.3)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    plt.tight_layout()

    # Explicitly save as PNG format
    plt.savefig(output_path, dpi=150, bbox_inches="tight", format="png")
    plt.close()

    # Verify output was actually written
    import os
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 1000:
        print(f"ERROR: Output file missing or too small after save: {output_path}")
        sys.exit(1)

    print(f"SUCCESS: {output_path}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--coin",   required=True)
    p.add_argument("--days",   type=int, default=30)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    generate(args.coin, args.days, args.output)
