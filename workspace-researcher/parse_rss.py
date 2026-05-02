import urllib.request
import xml.etree.ElementTree as ET

urls = {
    "CoinDesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "CoinTelegraph": "https://cointelegraph.com/rss",
    "Decrypt": "https://decrypt.co/feed"
}

for name, url in urls.items():
    print(f"--- {name} ---")
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            tree = ET.parse(response)
            root = tree.getroot()
            items = root.findall('.//item')[:10]
            for item in items:
                title = item.find('title').text
                link_elem = item.find('link')
                link = link_elem.text if link_elem is not None else "No link text"
                print(f"TITLE: {title}")
                print(f"LINK: {link}")
    except Exception as e:
        print(f"Error: {e}")
