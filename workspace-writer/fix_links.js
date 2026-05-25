import fs from 'fs';

let content = fs.readFileSync('/tmp/crypto-run-20260525-103551/article/raw.md', 'utf8');

// The instruction was: "exactly 2 distinct source anchors, in hook/first H2 only (URLs from source_urls in validated.json)."
// We only have ONE source URL in validated.json:
// https://www.coindesk.com/markets/2026/05/25/hype-funds-attract-millions-as-investors-dump-bitcoin-and-ether-etfs
// Oh wait, if there's only one URL in source_urls, the instruction "exactly 2 distinct source anchors ... (URLs from source_urls in validated.json)" is slightly contradictory unless we use the same URL twice or there are multiple URLs. The instruction says "distinct source anchors" which could mean distinct *anchor texts* linking to the same URL, OR it could mean distinct *URLs* from the array. Given there's only one URL, we have to link to it twice with distinct anchors, or maybe "distinct" meant unique URLs. Wait, "distinct source anchors" usually refers to the text. "Use exactly 2 distinct source anchors"
// Let's use two distinct anchor texts, both linking to the only available URL in source_urls. 

// We already have:
// [recent flow data](https://www.coindesk.com/markets/2026/05/25/hype-funds-attract-millions-as-investors-dump-bitcoin-and-ether-etfs) in hook
// [HYPE spot products](https://www.coindesk.com/markets/2026/05/25/hype-funds-attract-millions-as-investors-dump-bitcoin-and-ether-etfs) in first H2
// They are in the hook and first H2. They are distinct anchors (different text). Both point to the source URL.
// But wait, the prompt says "REPAIR REQUIRED — Your article has too many or duplicate source links. Use exactly 2 distinct source anchors, in hook/first H2 only. (URLs from source_urls in validated.json)."
// If it meant "distinct URLs", and there is only 1 URL in the source_urls array, then it's impossible to have 2 distinct URLs.
// BUT I can just make sure they are in the hook and first H2, and no other links exist in the body.
// The previous run might have had links in the conclusion or other sections, or identical anchor text, or a link to twitter.

// Let's verify no other links exist:
console.log(content.match(/\[.*?\]\(http.*?\)/g));
