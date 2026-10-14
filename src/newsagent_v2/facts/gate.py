"""Pre-writer evidence gate: can this event honestly support a long-form article?"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from newsagent_v2.facts.bank import FactBank
from newsagent_v2.research.dossier import ResearchDossier, host_of

# Built-in OG desks (kept even if config/sources.json is missing).
_CORE_OG_HOSTS = (
    "coindesk.com",
    "cointelegraph.com",
    "theblock.co",
    "bitcoinmagazine.com",
    "decrypt.co",
    "cryptoslate.com",
    "news.bitcoin.com",
    "newsbtc.com",
    "cryptopotato.com",
    "beincrypto.com",
    "blockworks.co",
    "thedefiant.io",
    "dlnews.com",
    "cryptobriefing.com",
    "u.today",
    "ambcrypto.com",
    "coinjournal.net",
    "bitcoinist.com",
    "coinbase.com",
    "kraken.com",
    "blog.kraken.com",
    "ethereum.org",
    "blog.ethereum.org",
    "ripple.com",
    "circle.com",
    # Mainstream / wires that often carry the same stories
    "reuters.com",
    "bloomberg.com",
    "cnbc.com",
    "bbc.com",
    "bbc.co.uk",
    "npr.org",
    "marketwatch.com",
    "yahoo.com",
    "finance.yahoo.com",
    "investing.com",
    "wsj.com",
    "ft.com",
    "nytimes.com",
    "theguardian.com",
    "forbes.com",
    "fortune.com",
    "axios.com",
    "apnews.com",
    "afp.com",
)

# RSS CDN hosts in sources.json → the host that appears on article URLs.
_FEED_HOST_ALIASES = {
    "feeds.bbci.co.uk": ("bbc.co.uk", "bbc.com"),
    "feeds.marketwatch.com": ("marketwatch.com",),
    "feeds.npr.org": ("npr.org",),
    "search.cnbc.com": ("cnbc.com",),
    "home.treasury.gov": ("treasury.gov",),
    "blog.kraken.com": ("kraken.com", "blog.kraken.com"),
    "blog.ethereum.org": ("ethereum.org", "blog.ethereum.org"),
}


def _sources_config_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "sources.json"


def _hosts_from_sources_json(path: Path | None = None) -> tuple[str, ...]:
    config = path or _sources_config_path()
    if not config.is_file():
        return ()
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ()
    hosts: set[str] = set()
    for row in payload.get("feeds") or []:
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        host = host_of(str(row.get("url") or ""))
        if not host:
            continue
        aliases = _FEED_HOST_ALIASES.get(host)
        if aliases:
            hosts.update(aliases)
        else:
            hosts.add(host)
    return tuple(sorted(hosts))


def _hosts_from_env(environ: dict[str, str] | None = None) -> tuple[str, ...]:
    env = environ if environ is not None else os.environ
    raw = str(env.get("NEWSAGENT_TRUSTED_HOSTS") or "").strip()
    if not raw:
        return ()
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        host = part.strip().lower().removeprefix("www.")
        if host:
            out.append(host)
    return tuple(out)


def load_trusted_hosts(
    *,
    environ: dict[str, str] | None = None,
    sources_path: Path | None = None,
) -> tuple[str, ...]:
    """All OG outlets: core desks + every enabled feed in sources.json + env extras."""
    merged = {
        *_CORE_OG_HOSTS,
        *_hosts_from_sources_json(sources_path),
        *_hosts_from_env(environ),
    }
    return tuple(sorted(merged))


# Module default used by qualify(); refreshed if config changes rarely enough for process life.
TRUSTED_HOSTS = load_trusted_hosts()


def trusted_outlet(urls: list[str], hosts: tuple[str, ...] | None = None) -> str:
    """Return the trusted host when any URL is from one of those OG outlets."""
    allow = hosts if hosts is not None else TRUSTED_HOSTS
    for url in urls:
        host = host_of(url or "")
        if not host:
            continue
        for trusted in allow:
            if host == trusted or host.endswith("." + trusted):
                return trusted
    return ""


@dataclass
class GatePolicy:
    min_full_sources: int = 2
    min_publishers: int = 2
    min_research_words: int = 800
    min_core_facts: int = 6
    min_numeric_core_facts: int = 1
    min_corroborated_core_facts: int = 0
    min_core_fact_words: int = 100


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.passed:
            return "Evidence sufficient for a long-form article."
        return "Skipped: " + "; ".join(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": list(self.reasons), "metrics": dict(self.metrics)}


def assess_evidence(
    dossier: ResearchDossier,
    bank: FactBank,
    policy: GatePolicy | None = None,
) -> GateResult:
    rules = policy or GatePolicy()
    stats = bank.stats()
    metrics = {
        "full_sources": len(dossier.full_sources),
        "publishers": len(dossier.publishers),
        "primary_sources": dossier.primary_count,
        "research_words": dossier.total_words,
        **{k: v for k, v in stats.items() if k != "dropped"},
    }
    checks = [
        (metrics["full_sources"] >= rules.min_full_sources,
         f"only {metrics['full_sources']} full sources (need {rules.min_full_sources})"),
        (metrics["publishers"] >= rules.min_publishers,
         f"only {metrics['publishers']} independent publishers (need {rules.min_publishers})"),
        (metrics["research_words"] >= rules.min_research_words,
         f"only {metrics['research_words']} words of research (need {rules.min_research_words})"),
        (metrics["core_facts"] >= rules.min_core_facts,
         f"only {metrics['core_facts']} distinct on-topic facts (need {rules.min_core_facts})"),
        (metrics["fact_words"] >= rules.min_core_fact_words,
         f"only {metrics['fact_words']} words of on-topic facts (need {rules.min_core_fact_words})"),
        (metrics["numeric_core_facts"] >= rules.min_numeric_core_facts,
         f"only {metrics['numeric_core_facts']} facts with figures (need {rules.min_numeric_core_facts})"),
        (metrics["corroborated_core_facts"] >= rules.min_corroborated_core_facts,
         f"only {metrics['corroborated_core_facts']} facts confirmed by 2+ outlets "
         f"(need {rules.min_corroborated_core_facts})"),
    ]
    reasons = [message for ok, message in checks if not ok]
    return GateResult(passed=not reasons, reasons=reasons, metrics=metrics)


def qualify(
    dossier: ResearchDossier,
    bank: FactBank,
    urls: list[str],
    policy: GatePolicy | None = None,
    *,
    trusted_hosts: tuple[str, ...] | None = None,
) -> GateResult:
    """Pass when an OG / trusted outlet is already covering the story; else use the evidence bar."""
    gate = assess_evidence(dossier, bank, policy)
    outlet = trusted_outlet(urls, hosts=trusted_hosts)
    if not outlet:
        return gate
    gate.passed = True
    gate.reasons = []
    gate.metrics = {**gate.metrics, "trusted_outlet": outlet}
    return gate
