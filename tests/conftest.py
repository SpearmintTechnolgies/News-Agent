"""Shared pytest fixtures for NewsAgent.

Keeps the default test suite offline: live RSS source expansion is skipped and
V6 deep research returns an empty dossier unless a test passes its own research_fn.
"""

from __future__ import annotations

import os
import socket

import dotenv
import pytest

# Release checks must never read a developer token or call a live provider.
dotenv.load_dotenv = lambda *args, **kwargs: False
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    def offline(*args, **kwargs):
        raise RuntimeError("Network disabled in release checks; inject a fake transport")
    monkeypatch.setattr(socket.socket, "connect", offline)
    monkeypatch.setattr(socket.socket, "connect_ex", offline)
    monkeypatch.setattr(socket, "create_connection", offline)
    monkeypatch.setattr(socket, "getaddrinfo", offline)
    from newsagent_v2.wordpress import authors
    from newsagent_v2.control import site_flow
    monkeypatch.setattr(authors, "active_site_id", "")
    monkeypatch.setattr(site_flow, "_pending", {})
    monkeypatch.setattr(site_flow, "_catalog", {})


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
