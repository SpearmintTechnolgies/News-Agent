"""Shared pytest fixtures for NewsAgent.

Keeps the default test suite offline: live RSS source expansion is skipped and
V6 deep research returns an empty dossier unless a test passes its own research_fn.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _default_offline_source_expansion(monkeypatch):
    """Prevent accidental live feed fetches during unit/integration tests."""
    monkeypatch.setenv("NEWSAGENT_V5_SKIP_SOURCE_EXPANSION", "true")


@pytest.fixture(autouse=True)
def _offline_v6_research(monkeypatch):
    """No live search or page fetches from the V6 story pipeline in tests."""
    from newsagent_v2.research.dossier import ResearchDossier
    import newsagent_v2.story6 as story6

    def offline(story):
        return ResearchDossier(event_id=str(story.get("event_id") or ""), title=str(story.get("representative_title") or ""))

    monkeypatch.setattr(story6, "deep_research", offline)


@pytest.fixture(autouse=True)
def _offline_site_index(monkeypatch):
    """No live sitemap fetches when V6 drafts are linked in tests (tests pass their own index)."""
    import newsagent_v2.seo6.apply as seo_apply
    from newsagent_v2.seo6.sitemap import SiteIndex

    monkeypatch.setattr(seo_apply, "load_site_index", lambda base_url, **_: SiteIndex(base_url=base_url))
