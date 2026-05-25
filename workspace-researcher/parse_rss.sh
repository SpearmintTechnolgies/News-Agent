#!/bin/bash
set -ex

# Function to parse an RSS feed
parse_feed() {
    local filename="$1"
    local source_name="$2"
    xmllint --xpath "//item" "$filename" 2>/dev/null | while IFS= read -r item; do
        echo "Processing item: $item"
        title=$(echo "$item" | xmllint --xpath "string(.//title)" - 2>/dev/null | sed 's/<!\[CDATA\[//g;s/\]\]>//g;s/&apos;/'"'"'/g;s/&quot;/"/g;s/&amp;/\&/g;s/&lt;/</g;s/&gt;/>/g')
        link=$(echo "$item" | xmllint --xpath "string(.//link)" - 2>/dev/null | sed 's/<!\[CDATA\[//g;s/\]\]>//g')
        pubdate=$(echo "$item" | xmllint --xpath "string(.//pubDate)" - 2>/dev/null | head -n 1)

        if [[ -n "$title" && -n "$link" && -n "$pubdate" ]]; then
            echo "$source_name|$title|$link|$pubdate"
        else
            echo "Skipping item due to missing title, link, or pubdate."
        fi
    done
}

# Clean up previous parsed files
rm -f *_parsed.txt all_articles.txt

# Process each feed
parse_feed coindesk.xml "CoinDesk" > coindesk_parsed.txt &
parse_feed cointelegraph.xml "CoinTelegraph" > cointelegraph_parsed.txt &
parse_feed decrypt.xml "Decrypt" > decrypt_parsed.txt &
parse_feed google_news.xml "Google News" > google_news_parsed.txt &
parse_feed beincrypto.xml "BeInCrypto" > beincrypto_parsed.txt &
parse_feed theblock.xml "The Block" > theblock_parsed.txt &
parse_feed cryptoslate.xml "CryptoSlate" > cryptoslate_parsed.txt &
parse_feed cryptofox_markets.xml "CryptoFox Markets" > cryptofox_markets_parsed.txt &
parse_feed cryptofox_regulation.xml "CryptoFox Regulation" > cryptofox_regulation_parsed.txt &

wait

# Concatenate all parsed data
cat *_parsed.txt > all_articles.txt
