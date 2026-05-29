import xml.etree.ElementTree as ET

for feed_file in ['coindesk.xml', 'cointelegraph.xml', 'cryptoslate.xml', 'beincrypto.xml']:
    print(f"=== {feed_file} ===")
    try:
        tree = ET.parse(feed_file)
        root = tree.getroot()
        items = root.findall('.//item')
        for item in items[:5]:
            title = item.find('title')
            pub_date = item.find('pubDate')
            link = item.find('link')
            print(f"Title: {title.text if title is not None else 'N/A'}")
            print(f"PubDate: {pub_date.text if pub_date is not None else 'N/A'}")
            print(f"Link: {link.text if link is not None else 'N/A'}")
            print("-" * 20)
    except Exception as e:
        print(f"Error parsing {feed_file}: {e}")
