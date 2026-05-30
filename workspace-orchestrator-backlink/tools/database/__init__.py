"""Thin wrappers over backlink_db for tool-layer use."""

from tools.database.db_tool import (
    get_search_cache,
    get_workflow,
    init_db,
    is_blacklisted,
    purge_expired_search_cache,
    save_search_cache,
)

__all__ = [
    "init_db",
    "get_workflow",
    "is_blacklisted",
    "get_search_cache",
    "save_search_cache",
    "purge_expired_search_cache",
]
