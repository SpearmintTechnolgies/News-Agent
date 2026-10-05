"""V6 SEO: site index from the sitemap, internal linking, Rank Math-style scoring."""

from newsagent_v2.seo6.apply import prepare_for_site
from newsagent_v2.seo6.linker import LinkPlan, plan_and_apply, rank_related
from newsagent_v2.seo6.score import (
    SEOScore,
    best_focus_keyword,
    finalize_seo,
    fit_meta,
    phrase_in,
    score_article,
    slug_with_keyword,
)
from newsagent_v2.seo6.sitemap import SiteIndex, SitePost, fetch_site_index, load_site_index

__all__ = [
    "LinkPlan",
    "plan_and_apply",
    "prepare_for_site",
    "rank_related",
    "SEOScore",
    "best_focus_keyword",
    "finalize_seo",
    "fit_meta",
    "phrase_in",
    "score_article",
    "slug_with_keyword",
    "SiteIndex",
    "SitePost",
    "fetch_site_index",
    "load_site_index",
]
