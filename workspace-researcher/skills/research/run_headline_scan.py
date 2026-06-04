import os
import sys
import re
import urllib.parse
import json
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
import dateutil.parser
import subprocess

# Output config
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "/tmp/crypto-run-20260604-055346/research/headlines.json")
TARGET_COUNT = int(os.environ.get("TARGET_COUNT", "10"))

UA = "Mozilla/5.0 (compatible; OpenClawScout/1.0)"
OUTDIR = "/tmp/scout_feeds"
os.makedirs(OUTDIR, exist_ok=True)

# 1. Fetch feeds
feeds = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed",
    "Google News": 'https://news.google.com/rss/search?q=cryptocurrency+OR+blockchain+OR+ethereum+OR+solana+OR+altcoin+OR+%22digital+assets%22+OR+XRP+OR+cardano+-dogecoin+-shiba+-pepe+-floki+-bonk+-wif+-memecoin+-%22meme+coin%22&hl=en-US&gl=US&ceid=US:en',
    "BeInCrypto": "https://beincrypto.com/feed/",
    "The Block": "https://www.theblock.co/rss.xml",
    "CryptoSlate": "https://cryptoslate.com/feed/",
    "CryptoFox Markets": "https://cryptofox.news/rss/markets/",
    "CryptoFox Regulation": "https://cryptofox.news/rss/regulation/"
}

# Source priority mapping for deduplication
SOURCE_PRIORITY = {
    "CoinDesk": 1,
    "CoinTelegraph": 2,
    "Decrypt": 3,
    "The Block": 4,
    "BeInCrypto": 5,
    "CryptoSlate": 6,
    "CryptoFox Markets": 7,
    "CryptoFox Regulation": 8,
    "Google News": 9
}

print("Fetching RSS feeds...")
for name, url in feeds.items():
    safe_name = name.lower().replace(" ", "_")
    dest = os.path.join(OUTDIR, f"{safe_name}.xml")
    try:
        # Use curl to download
        cmd = ["curl", "-sL", "-A", UA, "-o", dest, "--max-time", "12", url]
        subprocess.run(cmd, check=True)
        print(f"Successfully fetched {name}")
    except Exception as e:
        print(f"Error fetching {name}: {e}")

# Clean and decode helpers
def clean_html(text):
    if not text:
        return ""
    # Strip HTML tags
    clean = re.compile(r'<[^>]+>')
    text = clean.sub('', text)
    # Decode XML/HTML entities
    import html
    text = html.unescape(text)
    return text.strip()

def strip_google_redirect(url):
    # Google News links sometimes look like https://news.google.com/rss/articles/...
    # For now, let's keep them or try to extract, but keeping them as is is fine if we can't easily parse.
    # We can try to decode if there's a specific pattern, but usually we just keep it as is.
    return url

# Time boundary: last 24 hours
now = datetime.now(timezone.utc)
time_boundary = now - timedelta(hours=24)

all_items = []

print("Parsing feeds...")
for name in feeds.keys():
    safe_name = name.lower().replace(" ", "_")
    xml_path = os.path.join(OUTDIR, f"{safe_name}.xml")
    if not os.path.exists(xml_path):
        continue
    
    try:
        # Parse XML file. Some might have encoding issues, so we read as binary/utf-8 or fallback
        with open(xml_path, 'rb') as f:
            xml_content = f.read()
        
        # Parse xml
        root = ET.fromstring(xml_content)
        items = root.findall('.//item') or root.findall('.//entry')
        
        feed_item_count = 0
        for item in items:
            title_node = item.find('title')
            link_node = item.find('link')
            pub_date_node = item.find('pubDate') or item.find('{http://www.w3.org/2005/Atom}published') or item.find('{http://www.w3.org/2005/Atom}updated') or item.find('published') or item.find('updated')
            desc_node = item.find('description') or item.find('summary') or item.find('{http://www.w3.org/2005/Atom}summary')
            
            headline = clean_html(title_node.text) if title_node is not None and title_node.text else ""
            
            # Extract link
            url = ""
            if link_node is not None:
                if link_node.text:
                    url = link_node.text.strip()
                elif 'href' in link_node.attrib:
                    url = link_node.attrib['href'].strip()
            
            if not url:
                # Try finding link via atom namespace or children
                for child in item:
                    if 'link' in child.tag or child.tag.endswith('link'):
                        if child.text:
                            url = child.text.strip()
                        elif 'href' in child.attrib:
                            url = child.attrib['href'].strip()
                        if url:
                            break
            
            url = strip_google_redirect(url)
            
            # Pub Date
            pub_date_str = ""
            if pub_date_node is not None and pub_date_node.text:
                pub_date_str = pub_date_node.text.strip()
            
            # Parse Pub Date
            parsed_date = None
            if pub_date_str:
                try:
                    parsed_date = dateutil.parser.parse(pub_date_str)
                    if parsed_date.tzinfo is None:
                        parsed_date = parsed_date.replace(tzinfo=timezone.utc)
                    else:
                        parsed_date = parsed_date.astimezone(timezone.utc)
                except Exception:
                    pass
            
            # Description / summary
            summary = ""
            if desc_node is not None and desc_node.text:
                summary = clean_html(desc_node.text)
            else:
                # Try content encoded or similar
                content_encoded = item.find('{http://purl.org/rss/1.0/modules/content/}encoded')
                if content_encoded is not None and content_encoded.text:
                    summary = clean_html(content_encoded.text)
            
            # Trim summary to 280 chars
            if summary:
                summary = summary[:280]
                if len(summary) == 280:
                    summary += "..."
            
            # Filters
            if not headline or not url:
                continue
            
            if url.startswith("mailto:") or url.startswith("javascript:"):
                continue
            
            # Date filter: within 24 hours. If pub date parsing fails, keep only if it is within first 5 items of feed
            keep_by_date = False
            if parsed_date:
                if parsed_date >= time_boundary:
                    keep_by_date = True
            else:
                if feed_item_count < 5:
                    keep_by_date = True
            
            if not keep_by_date:
                continue
            
            # Meme coin filter
            # Deprioritize or skip items that are primarily meme-coin price pumps
            meme_words = ["dogecoin", "shiba", "pepe", "floki", "bonk", "wif", "memecoin", "meme coin", "doge", "shib"]
            headline_lower = headline.lower()
            is_meme = any(w in headline_lower for w in meme_words)
            
            # If it is a meme coin topic, check if it's seriously covered by other feeds (we'll handle cross-coverage later, but let's flag it)
            # For simplicity, let's keep all non-meme items, and we'll drop meme ones unless they have multiple sources.
            
            all_items.append({
                "headline": headline,
                "url": url,
                "pub_date": parsed_date.isoformat() if parsed_date else datetime.now(timezone.utc).isoformat(),
                "parsed_date_obj": parsed_date if parsed_date else datetime.now(timezone.utc),
                "source": name,
                "summary": summary,
                "is_meme": is_meme,
                "corroborating_sources": []
            })
            feed_item_count += 1

    except Exception as e:
        print(f"Error parsing feed {name}: {e}")

print(f"Total raw items gathered: {len(all_items)}")

# Let's clean up duplicate URLs first
unique_url_items = {}
for item in all_items:
    url = item["url"]
    if url not in unique_url_items:
        unique_url_items[url] = item
    else:
        # Keep the one with higher priority source
        existing = unique_url_items[url]
        if SOURCE_PRIORITY.get(item["source"], 99) < SOURCE_PRIORITY.get(existing["source"], 99):
            # Move corroborating sources if any
            item["corroborating_sources"].extend(existing["corroborating_sources"])
            if existing["source"] != item["source"]:
                item["corroborating_sources"].append({"source": existing["source"], "url": existing["url"]})
            unique_url_items[url] = item
        else:
            if existing["source"] != item["source"]:
                existing["corroborating_sources"].append({"source": item["source"], "url": item["url"]})

items_after_url_dedupe = list(unique_url_items.values())
print(f"After URL deduplication: {len(items_after_url_dedupe)}")

# Group by near-identical headlines (>= 80% token overlap)
def get_tokens(text):
    text_clean = re.sub(r'[^\w\s]', '', text.lower())
    tokens = set(text_clean.split())
    # Remove some common stop words
    stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "with", "by", "of", "is", "are", "was", "were", "be", "been", "has", "have", "had"}
    tokens = tokens - stop_words
    return tokens

def is_near_duplicate(item1, item2):
    tokens1 = get_tokens(item1["headline"])
    tokens2 = get_tokens(item2["headline"])
    if not tokens1 or not tokens2:
        return False
    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)
    overlap = len(intersection) / min(len(tokens1), len(tokens2))
    return overlap >= 0.8

# Same-story dedupe (using near-identical headlines)
deduped_items = []
for item in items_after_url_dedupe:
    # If it's meme coin, we only keep it if there is at least one corroborating source of high priority
    # Let's do that check in same-story dedupe
    
    # Check if we have a match in already deduped_items
    found_duplicate = False
    for existing in deduped_items:
        if is_near_duplicate(item, existing):
            found_duplicate = True
            # Merge corroborating sources (up to 2)
            if len(existing["corroborating_sources"]) < 2:
                existing["corroborating_sources"].append({"source": item["source"], "url": item["url"]})
            # If current item has higher source priority, swap them but keep the corroborating sources
            if SOURCE_PRIORITY.get(item["source"], 99) < SOURCE_PRIORITY.get(existing["source"], 99):
                # swap primary details but preserve the corroborating sources we're collecting
                old_existing_source = existing["source"]
                old_existing_url = existing["url"]
                
                existing["headline"] = item["headline"]
                existing["url"] = item["url"]
                existing["pub_date"] = item["pub_date"]
                existing["parsed_date_obj"] = item["parsed_date_obj"]
                existing["source"] = item["source"]
                existing["summary"] = item["summary"]
                existing["is_meme"] = item["is_meme"]
                
                # Make sure we don't exceed 2 corroborating sources
                if old_existing_source != existing["source"]:
                    if len(existing["corroborating_sources"]) < 2:
                        existing["corroborating_sources"].append({"source": old_existing_source, "url": old_existing_url})
            break
            
    if not found_duplicate:
        deduped_items.append(item)

print(f"After headline deduplication: {len(deduped_items)}")

# Meme-only filter: Drop if is_meme is True, UNLESS there is at least 1 corroborating source from a major outlet
final_candidates = []
for item in deduped_items:
    if item["is_meme"]:
        # Check if cross-covered
        if len(item["corroborating_sources"]) >= 1:
            final_candidates.append(item)
        else:
            # Drop meme topic without coverage
            print(f"Dropping meme-only topic: {item['headline']}")
            continue
    else:
        final_candidates.append(item)

print(f"After meme filter: {len(final_candidates)}")

# Step 4 - Persistent dedup gate (URL history)
surviving_candidates = []
for item in final_candidates:
    url = item["url"]
    try:
        # Run history check
        cmd = ["bash", "/home/bhard/.openclaw/workspace-researcher/skills/history/article_history.sh", "check", url]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        status = res.stdout.strip()
        if "NOT_FOUND" in status:
            surviving_candidates.append(item)
        else:
            print(f"Skipping already processed URL: {url}")
    except Exception as e:
        print(f"Error checking history for {url}: {e}")
        # Default to keeping if check fails, to be safe
        surviving_candidates.append(item)

print(f"After history check: {len(surviving_candidates)}")

# Order by pub_date desc
surviving_candidates.sort(key=lambda x: x["parsed_date_obj"], reverse=True)

# Step 5 - Trim to TARGET_COUNT and write JSON
final_list = []
for idx, item in enumerate(surviving_candidates[:TARGET_COUNT]):
    final_list.append({
        "candidate_index": idx + 1,
        "headline": item["headline"],
        "url": item["url"],
        "pub_date": item["pub_date"],
        "source": item["source"],
        "summary": item["summary"],
        "corroborating_sources": item["corroborating_sources"][:2]
    })

output_data = {
    "status": "ok",
    "mode": "headline_scan",
    "scanned_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "target_count": TARGET_COUNT,
    "candidate_count": len(final_list),
    "candidates": final_list
}

os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
    json.dump(output_data, f, indent=2, ensure_ascii=False)

print(f"Wrote {len(final_list)} candidates to {OUTPUT_FILE}")
