#!/usr/bin/env bash
# =============================================================================
# verify_feeds.sh — Smoke-test all Scout RSS feed URLs
# =============================================================================
# Usage:
#   bash ~/.openclaw/workspace-researcher/skills/research/verify_feeds.sh
#
# Exit 0 if all required feeds return HTTP 200 with RSS items; exit 1 if any fail.
# The Block is optional (Cloudflare); BeInCrypto is the required markets/regulation substitute.
# =============================================================================
set -uo pipefail

TIMEOUT=12
UA="Mozilla/5.0 (compatible; OpenClawScout/1.0)"
FAILED=0
PASSED=0

GOOGLE_NEWS_URL='https://news.google.com/rss/search?q=cryptocurrency+OR+blockchain+OR+ethereum+OR+solana+OR+altcoin+OR+%22digital+assets%22+OR+XRP+OR+cardano+-dogecoin+-shiba+-pepe+-floki+-bonk+-wif+-memecoin+-%22meme+coin%22&hl=en-US&gl=US&ceid=US:en'

has_rss_items() {
    local file="$1"
    grep -qiE '<(item|entry)(\s|>|/)' "$file" 2>/dev/null && return 0
    grep -qi '<item' "$file" 2>/dev/null && return 0
    grep -qi '<entry' "$file" 2>/dev/null && return 0
    return 1
}

count_rss_items() {
    local file="$1"
    local n
    n=$(grep -oiE '<(item|entry)(\s|>|/)' "$file" 2>/dev/null | wc -l)
    if [[ "$n" -gt 0 ]]; then
        echo "$n"
        return
    fi
    n=$(grep -oi '<item' "$file" 2>/dev/null | wc -l)
    if [[ "$n" -gt 0 ]]; then
        echo "$n"
        return
    fi
    grep -oi '<entry' "$file" 2>/dev/null | wc -l
}

check_feed() {
    local name="$1"
    local url="$2"
    local required="${3:-1}"
    local tmp code count

    tmp=$(mktemp)
    trap 'rm -f "$tmp"' RETURN

    code=$(curl -sL -A "$UA" -o "$tmp" -w "%{http_code}" --max-time "$TIMEOUT" "$url" 2>/dev/null) || {
        rm -f "$tmp"
        trap - RETURN
        if [[ "$required" == "1" ]]; then
            echo "FAIL  $name — curl error"
            FAILED=$((FAILED + 1))
        else
            echo "WARN  $name — curl error (optional)"
        fi
        return 0
    }

    if [[ "$code" != "200" ]]; then
        rm -f "$tmp"
        trap - RETURN
        if [[ "$required" == "1" ]]; then
            echo "FAIL  $name — HTTP $code"
            FAILED=$((FAILED + 1))
        else
            echo "WARN  $name — HTTP $code (optional)"
        fi
        return 0
    fi

    if has_rss_items "$tmp"; then
        count=$(count_rss_items "$tmp")
        echo "OK    $name — ~$count items (HTTP $code)"
        PASSED=$((PASSED + 1))
    elif [[ "$required" == "1" ]]; then
        echo "FAIL  $name — HTTP 200 but no RSS items"
        FAILED=$((FAILED + 1))
    else
        echo "WARN  $name — no RSS items (optional)"
    fi

    rm -f "$tmp"
    trap - RETURN
    return 0
}

echo "Scout RSS feed verification"
echo "=========================="

check_feed "CoinDesk"              "https://www.coindesk.com/arc/outboundfeeds/rss/"
check_feed "CoinTelegraph"         "https://cointelegraph.com/rss"
check_feed "Decrypt"               "https://decrypt.co/feed"
check_feed "Google News"           "$GOOGLE_NEWS_URL"
check_feed "The Block"             "https://www.theblock.co/rss.xml" 0
check_feed "BeInCrypto"            "https://beincrypto.com/feed/"
check_feed "CryptoSlate"           "https://cryptoslate.com/feed/"
check_feed "CryptoFox Markets"     "https://cryptofox.news/rss/markets/"
check_feed "CryptoFox Regulation"  "https://cryptofox.news/rss/regulation/"

echo "=========================="
echo "Passed: $PASSED  Failed: $FAILED"

if [[ "$FAILED" -gt 0 ]]; then
    exit 1
fi
echo "All required feeds OK"
exit 0
