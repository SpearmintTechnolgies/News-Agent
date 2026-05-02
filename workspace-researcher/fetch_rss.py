import urllib.request
import xml.etree.ElementTree as ET
import re

feeds = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed"
}

for name, url in feeds.items():
    print(f"--- {name} ---")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        xml_data = urllib.request.urlopen(req, timeout=20).read()
        try:
            root = ET.fromstring(xml_data)
        except ET.ParseError:
            # Maybe it has some weird encoding or leading whitespace
            xml_data = re.sub(b'^[^<]+', b'', xml_data)
            root = ET.fromstring(xml_data)
            
        items = root.findall(".//item")
        for item in items[:15]:
            title = item.findtext("title")
            link = item.findtext("link")
            pubDate = item.findtext("pubDate")
            print(f"TITLE: {title}\nLINK: {link}\nDATE: {pubDate}\n")
    except Exception as e:
        print(f"Error fetching {name}: {e}")
