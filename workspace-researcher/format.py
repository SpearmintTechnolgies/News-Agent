import json
import os

key_facts = [
    "A South Korean court overturned a six-month partial business suspension imposed on Bithumb (CoinDesk).",
    "Judge Gong Hyeon-jin of the Seoul Administrative Court granted the emergency injunction blocking enforcement (Decrypt & CoinDesk).",
    "Bithumb was accused by South Korea's Financial Intelligence Unit of about 6.65 million violations of anti-money laundering rules, including failures to verify customer identities and to block suspicious transactions (CoinDesk & Decrypt).",
    "The initial suspension was accompanied by a $24.6 million fine (CoinDesk).",
    "The court noted that the suspension of core functions like inter-exchange transactions and external transfers would cause difficulty in attracting new customers, particularly as regulatory changes may soon allow listed and professional investment corporations to particulate (Decrypt).",
    "This comes amid broader regulatory scrutiny following a February incident where Bithumb mistakenly credited hundreds of users with 2,000 BTC instead of 2,000 won in a promotion (Decrypt)."
]

with open("coindesk_bithumb.txt", "r") as f:
    coindesk_text = f.read()

with open("decrypt_bithumb.txt", "r") as f:
    decrypt_text = f.read()

raw_text = coindesk_text + "\n\n" + decrypt_text

output = {
  "topic_theme": "South Korean Court Overturns Bithumb Suspension",
  "primary_keyword": "Bithumb suspension lifted",
  "primary_headline": "Bithumb Scores a Legal Win in South Korea as Six-Month Suspension is Lifted by Local Judge",
  "sources_used": [
    "CoinDesk",
    "Decrypt"
  ],
  "source_urls": [
    "https://www.coindesk.com/policy/2026/05/01/bithumb-scores-a-legal-win-in-south-korea-as-six-month-suspension-is-lifted-by-local-judge",
    "https://decrypt.co/366292/south-korean-court-lifts-bithumbs-six-month-business-suspension"
  ],
  "combined_key_facts": key_facts,
  "aggregated_raw_content": raw_text[:5000] # Ensuring sufficient length
}

print(json.dumps(output, indent=2))
