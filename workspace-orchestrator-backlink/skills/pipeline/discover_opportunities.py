#!/usr/bin/env python3
"""discover_opportunities.py — search, fetch, parse, dedup, create workflows."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402
from tools.parser.parser import parse  # noqa: E402
from tools.search.search import SearchResult, search  # noqa: E402

CAMPAIGN_CONFIG = _ROOT / "config" / "campaign.json"

DEFAULT_QUERIES = (
    "cryptography guest post",
    "crypto write for us",
    "blockchain guest post submission",
    "cryptography blog contribute",
)


def load_campaign_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CAMPAIGN_CONFIG
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def default_queries(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or load_campaign_config()
    raw = cfg.get("search_queries")
    if isinstance(raw, list) and raw:
        return [str(q).strip() for q in raw if str(q).strip()]
    return list(DEFAULT_QUERIES)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def fetch_and_parse_url(
    url: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch page (or use parsed_pages cache), parse signals, persist cache."""
    cached = backlink_db.get_parsed_page(url, db_path=db_path)
    if cached and not force_refresh:
        return {
            "url": url,
            "cached": True,
            "title": cached.get("title"),
            "signals": cached.get("signals") or {},
            "content_hash": cached.get("content_hash"),
        }

    from tools.page_fetch.page_fetch import page_fetch  # noqa: E402

    fetched = page_fetch(url)
    parsed = parse(fetched.html, base_url=fetched.final_url or url)
    digest = content_hash(parsed.text)
    backlink_db.save_parsed_page(
        fetched.final_url or url,
        title=parsed.title or None,
        content_hash=digest,
        signals=parsed.signals,
        db_path=db_path,
    )
    return {
        "url": fetched.final_url or url,
        "cached": False,
        "title": parsed.title,
        "signals": parsed.signals,
        "content_hash": digest,
        "status": fetched.status,
    }


def _result_to_candidate(
    result: SearchResult,
    *,
    parsed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {
        "title": result.title,
        "url": result.url,
        "snippet": result.snippet,
        "source": result.source,
    }
    if parsed:
        candidate["parsed"] = {
            "cached": parsed.get("cached"),
            "title": parsed.get("title"),
            "signals": parsed.get("signals") or {},
        }
    return candidate


def discover_from_search(
    campaign_id: int,
    *,
    queries: list[str] | None = None,
    limit_per_query: int = 5,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    fetch_pages: bool = True,
    use_search_cache: bool = True,
) -> dict[str, Any]:
    """
    Search for backlink candidates, dedupe by url_hash, create opportunity + workflow for new URLs.
    """
    query_list = queries or default_queries()
    created: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for query in query_list:
        try:
            results = search(
                query,
                limit=limit_per_query,
                use_cache=use_search_cache,
                db_path=db_path,
            )
        except Exception as exc:  # noqa: BLE001 — collect per-query failures
            errors.append({"query": query, "error": str(exc)})
            continue

        for result in results:
            normalized = backlink_db.normalize_url(result.url)
            if normalized in seen_urls:
                continue
            seen_urls.add(normalized)

            hash_value = backlink_db.url_hash(result.url)
            existing = backlink_db.find_opportunity_by_url_hash(
                campaign_id,
                hash_value,
                db_path=db_path,
            )
            if existing:
                skipped.append(
                    {
                        "url": result.url,
                        "opportunity_id": existing.id,
                        "reason": "duplicate_url_hash",
                    }
                )
                continue

            parsed: dict[str, Any] | None = None
            if fetch_pages:
                try:
                    parsed = fetch_and_parse_url(result.url, db_path=db_path)
                except Exception as exc:  # noqa: BLE001
                    errors.append({"url": result.url, "error": str(exc)})
                    parsed = None

            candidates.append(_result_to_candidate(result, parsed=parsed))

            title = (parsed or {}).get("title") or result.title
            workflow_id = workflow_manager.new_workflow_id()
            try:
                opp, wf = backlink_db.create_opportunity_and_workflow(
                    campaign_id,
                    result.url,
                    workflow_id,
                    title=title,
                    snippet=result.snippet,
                    db_path=db_path,
                )
            except ValueError:
                existing = backlink_db.find_opportunity_by_url_hash(
                    campaign_id,
                    hash_value,
                    db_path=db_path,
                )
                skipped.append(
                    {
                        "url": result.url,
                        "opportunity_id": existing.id if existing else None,
                        "reason": "race_duplicate",
                    }
                )
                continue

            if parsed:
                backlink_db.update_opportunity_fields(
                    opp.id,
                    title=title,
                    snippet=result.snippet,
                    context_json={"discovery_signals": parsed.get("signals") or {}},
                    db_path=db_path,
                )
                backlink_db.insert_log(
                    wf.workflow_id,
                    "discovery",
                    f"Discovered {result.url}",
                    detail={"signals": parsed.get("signals"), "cached": parsed.get("cached")},
                    db_path=db_path,
                )

            created.append(
                {
                    "opportunity_id": opp.id,
                    "workflow_id": wf.workflow_id,
                    "url": opp.url,
                    "title": title,
                }
            )

    return {
        "campaign_id": campaign_id,
        "queries": query_list,
        "candidates_found": len(candidates),
        "created": created,
        "skipped_duplicates": skipped,
        "errors": errors,
    }


def enrich_workflow_opportunity(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
    fetch_pages: bool = True,
) -> workflow_manager.AgentResult:
    """Discovery step for an existing workflow — fetch/parse its opportunity URL."""
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if not row.opportunity_id:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="discovery",
            data={},
            error="Workflow has no linked opportunity",
        )

    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    if not opp or not opp.url:
        return workflow_manager.AgentResult(
            success=False,
            workflow_id=workflow_id,
            step="discovery",
            data={"opportunity_id": row.opportunity_id},
            error="Opportunity URL missing",
        )

    parsed: dict[str, Any] | None = None
    error: str | None = None
    if fetch_pages:
        try:
            parsed = fetch_and_parse_url(opp.url, db_path=db_path)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
    else:
        cached = backlink_db.get_parsed_page(opp.url, db_path=db_path)
        if cached:
            parsed = {
                "url": opp.url,
                "cached": True,
                "title": cached.get("title"),
                "signals": cached.get("signals") or {},
            }

    title = (parsed or {}).get("title") or opp.title
    signals = (parsed or {}).get("signals") or {}
    if parsed:
        backlink_db.update_opportunity_fields(
            opp.id,
            title=title,
            context_json={"discovery_signals": signals},
            db_path=db_path,
        )

    backlink_db.insert_log(
        workflow_id,
        "discovery",
        f"Enriched opportunity {opp.url}",
        detail={"signals": signals, "cached": (parsed or {}).get("cached"), "fetch_error": error},
        db_path=db_path,
    )

    return workflow_manager.AgentResult(
        success=True,
        workflow_id=workflow_id,
        step="discovery",
        data={
            "opportunity_id": opp.id,
            "url": opp.url,
            "title": title,
            "signals": signals,
            "cached": (parsed or {}).get("cached"),
            "fetch_error": error,
            "candidates": [
                _result_to_candidate(
                    SearchResult(title=title or "", url=opp.url, snippet=opp.snippet or ""),
                    parsed=parsed,
                )
            ],
        },
    )


def main() -> int:
    import argparse

    from worker_cli import run_discovery  # noqa: E402

    parser = argparse.ArgumentParser(description="Run discovery for one workflow")
    parser.add_argument("--workflow-id", required=True)
    parser.add_argument("--db", default=backlink_db.DEFAULT_DB_PATH)
    args = parser.parse_args()
    return run_discovery(args.workflow_id, args.db)


if __name__ == "__main__":
    raise SystemExit(main())
