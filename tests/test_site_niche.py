"""Per-site discovery niches (e.g. Altcoin Informer → altcoins only)."""

from types import SimpleNamespace

from newsagent_v2.control.site_niche import (
    ALTCOINS,
    CRYPTO,
    NICHE_ENV,
    active_niche,
    event_matches_niche,
    niche_for_host,
    niche_for_site,
    niche_search_queries,
)
from newsagent_v2.control.sites import Site
from newsagent_v2.control.story_picker import HIGH_TREND, category_queries, story_matches


def _event(title: str):
    return SimpleNamespace(canonical_title=title, topic="", reports=[])


def test_altcoininformer_host_is_altcoin_niche():
    assert niche_for_host("altcoininformer.com").key == ALTCOINS
    assert niche_for_host("www.altcoininformer.com").key == ALTCOINS
    site = Site("abcd1234", "Altcoin Informer", "https://altcoininformer.com", "u", "p")
    assert niche_for_site(site).key == ALTCOINS


def test_coinnetwork_stays_broad_crypto():
    site = Site("coinnet1", "coinnetwork.info", "https://coinnetwork.info", "u", "p")
    assert niche_for_site(site).key == CRYPTO
    assert niche_search_queries(niche_for_site(site)) == ()


def test_altcoin_niche_keeps_solana_drops_bitcoin_etf(monkeypatch):
    monkeypatch.setenv(NICHE_ENV, ALTCOINS)
    assert event_matches_niche(_event("Solana TVL hits a new high"))
    assert not event_matches_niche(_event("Bitcoin ETF inflows smash records"))
    assert story_matches(_event("XRP rallies after court filing"), HIGH_TREND)
    assert not story_matches(_event("SEC clears spot Bitcoin ETF"), "trend")


def test_altcoin_niche_drives_high_trend_search_queries(monkeypatch):
    monkeypatch.setenv(NICHE_ENV, ALTCOINS)
    queries = category_queries(HIGH_TREND)
    assert queries
    assert any("altcoin" in q or "solana" in q or "xrp" in q for q in queries)
    assert category_queries(HIGH_TREND, round=1) != category_queries(HIGH_TREND, round=0)


def test_active_niche_reads_env(monkeypatch):
    monkeypatch.setenv(NICHE_ENV, ALTCOINS)
    assert active_niche().key == ALTCOINS
    monkeypatch.setenv(NICHE_ENV, CRYPTO)
    assert active_niche().key == CRYPTO


def test_catalog_search_and_relevance_use_site_categories():
    from newsagent_v2.control.site_niche import (
        catalog_search_queries,
        category_relevance_score,
        event_matches_site_categories,
    )

    categories = [
        {"id": 1, "name": "Solana News"},
        {"id": 2, "name": "Animals Memecoins"},
    ]
    queries = catalog_search_queries(categories, round=0)
    assert queries
    assert any("solana" in q.lower() or "doge" in q.lower() or "pepe" in q.lower() for q in queries)
    sol = _event("Solana validators hit a record")
    btc = _event("Bitcoin ETF inflows smash records")
    assert category_relevance_score(sol, categories) >= 1
    assert event_matches_site_categories(sol, categories)
    assert not event_matches_site_categories(btc, categories)


def test_activate_sets_altcoin_niche_env(tmp_path):
    from newsagent_v2.control.sites import Site, activate, save_site

    path = tmp_path / "sites.json"
    site = Site("alt1", "Altcoin Informer", "https://altcoininformer.com", "u", "p")
    save_site(site, path=path)
    env: dict[str, str] = {}
    activate(site.id, path=path, environ=env)
    assert env[NICHE_ENV] == ALTCOINS
