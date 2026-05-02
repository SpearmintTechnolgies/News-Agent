#!/bin/bash
# Fetch and parse
# Kelp DAO Exploit / Aave Debt
url_cd=$(curl -sL "https://www.coindesk.com/arc/outboundfeeds/rss/" | grep -A 1 "Aave raises nearly 80% of the" | grep "<link>" | sed -e 's/<link>//' -e 's/<\/link>//')
url_ct=$(curl -sL "https://cointelegraph.com/rss" | grep -A 1 "Aave asks Arbitrum" | grep "<link>" | sed 's/.*<link><!\[CDATA\[//' | sed 's/]]><\/link>//' | cut -d'?' -f1)

# Fetch article 1
echo "Fetching Coindesk..."
curl -s "$url_cd" -L --max-time 15 | sed 's/<[^>]*>//g' | tr -s ' \n' | head -c 2000

# Fetch article 2
echo -e "\n\nFetching Cointelegraph..."
curl -s "$url_ct" -L --max-time 15 | sed 's/<[^>]*>//g' | tr -s ' \n' | head -c 2000
