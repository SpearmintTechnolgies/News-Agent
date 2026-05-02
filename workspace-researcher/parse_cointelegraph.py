import urllib.request
from bs4 import BeautifulSoup

url = "https://cointelegraph.com/news/bitcoin-bottom-57k-level-october"

req = urllib.request.Request(
    url, 
    data=None, 
    headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    }
)

try:
    with urllib.request.urlopen(req) as response:
        html = response.read()
        soup = BeautifulSoup(html, 'html.parser')
        article_content = soup.find('article') # Common tag for the main article
        if article_content:
             print(article_content.get_text(separator=' ', strip=True)[:1000])
        else:
             print("Could not find <article> tag. Dumping paragraphs...")
             for p in soup.find_all('p')[:10]:
                 print(p.text)
except Exception as e:
    print(f"Error fetching: {e}")
