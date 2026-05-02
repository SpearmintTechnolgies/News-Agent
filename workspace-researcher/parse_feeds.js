const fs = require('fs');

function parse(file, site) {
  try {
    const data = fs.readFileSync(file, 'utf8');
    const regex = /<item>[\s\S]*?<title>(.*?)<\/title>[\s\S]*?<link>(.*?)<\/link>[\s\S]*?<pubDate>(.*?)<\/pubDate>[\s\S]*?<\/item>/g;
    let match;
    let limit = 20;
    while ((match = regex.exec(data)) && limit > 0) {
      if (match[3].includes("29 Apr 2026") || match[3].includes("28 Apr 2026")) {
        console.log(`${site}|${match[1]}|${match[3]}|${match[2]}`);
      }
      limit--;
    }
  } catch(e) {}
}

parse('coindesk.xml', 'CoinDesk');
parse('cointelegraph.xml', 'CoinTelegraph');
parse('decrypt.xml', 'Decrypt');
