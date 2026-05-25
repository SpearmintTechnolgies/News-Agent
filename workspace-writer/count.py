import re
with open('/tmp/crypto-run-20260525-105830/article/raw.md', 'r') as f:
    text = f.read()

body_match = re.search(r'# .*?\n(.*)\*\*Sources:\*\*', text, re.DOTALL)
if body_match:
    body = body_match.group(1)
    words = re.findall(r'\b\w+\b', body)
    print("Body word count:", len(words))
else:
    print("Could not parse body")
