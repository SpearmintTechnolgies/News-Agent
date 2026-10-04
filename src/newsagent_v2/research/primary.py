"""Primary-source classification for regulators, governments and issuers."""

from __future__ import annotations

import re

from newsagent_v2.research.dossier import host_of

PRIMARY_HOST_SUFFIXES = (
    ".gov",
    ".gov.uk",
    ".gov.sg",
    ".gov.au",
    ".gc.ca",
    ".europa.eu",
    ".mil",
)
PRIMARY_HOSTS = frozenset(
    {
        "sec.gov", "cftc.gov", "federalreserve.gov", "treasury.gov", "whitehouse.gov",
        "justice.gov", "congress.gov", "fdic.gov", "occ.gov", "finra.org", "bls.gov", "bea.gov",
        "fca.org.uk", "bankofengland.co.uk", "ecb.europa.eu", "esma.europa.eu", "eba.europa.eu",
        "bis.org", "imf.org", "worldbank.org", "fsb.org", "iosco.org", "mas.gov.sg", "sfc.hk",
        "hkma.gov.hk", "rbi.org.in", "boj.or.jp", "fsa.go.jp", "bundesbank.de", "bafin.de",
        "prnewswire.com", "businesswire.com", "globenewswire.com", "accesswire.com",
        "investor.gov", "supremecourt.gov", "uscourts.gov", "courtlistener.com",
    }
)
# Social/video/aggregator hosts are never fetched as sources.
BLOCKED_HOSTS = frozenset(
    {
        "news.google.com", "bing.com", "youtube.com", "youtu.be", "x.com", "twitter.com",
        "facebook.com", "instagram.com", "reddit.com", "tiktok.com", "linkedin.com",
        "t.me", "telegram.me", "discord.gg", "discord.com", "apps.apple.com", "play.google.com",
        "tradingview.com", "coinmarketcap.com", "coingecko.com", "flipboard.com", "newsnow.co.uk",
        "msn.com",
    }
)
_HOST_LABEL_RE = re.compile(r"[^a-z0-9]")
# Asset and sector names are not issuers: bitcoin.com is a publisher, not Bitcoin.
_NON_ISSUER_NAMES = frozenset(
    {
        "bitcoin", "ethereum", "ether", "crypto", "cryptocurrency", "solana", "ripple", "dogecoin",
        "litecoin", "cardano", "polkadot", "tether", "stablecoin", "defi", "blockchain", "markets",
        "finance", "money", "news", "trading", "token", "coin", "dollar", "gold", "silver", "oil",
    }
)


def _host_matches(host: str, candidates: frozenset[str]) -> bool:
    return any(host == c or host.endswith("." + c) for c in candidates)


def is_blocked_host(url: str) -> bool:
    return _host_matches(host_of(url), BLOCKED_HOSTS)


def is_primary_url(url: str, entities: list[str] | None = None) -> bool:
    host = host_of(url)
    if not host:
        return False
    if _host_matches(host, PRIMARY_HOSTS) or host.endswith(PRIMARY_HOST_SUFFIXES):
        return True
    # Issuer's own site: host label equals an entity name (coinbase.com for "Coinbase").
    label = host.split(".")[-2] if host.count(".") >= 1 else host
    for entity in entities or []:
        name = _HOST_LABEL_RE.sub("", entity.lower())
        if len(name) >= 4 and name == label and name not in _NON_ISSUER_NAMES:
            return True
    return False
