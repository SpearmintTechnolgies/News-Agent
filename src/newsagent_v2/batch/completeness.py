"""Account for every requested Top-5 event ID. No silent omissions."""

from __future__ import annotations

from typing import Any

from newsagent_v2.batch.contract import BatchError


def account_requested_ids(
    requested: list[str],
    returned: dict[str, Any] | None,
    failed: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    requested IDs must equal returned IDs union explicitly_failed_ids.

    Rejects duplicates, unknown IDs, and silently missing IDs.
    """
    if not isinstance(requested, list) or not all(isinstance(item, str) and item for item in requested):
        raise BatchError("invalid_requested_ids", "requested event IDs must be non-empty strings")
    returned = returned or {}
    failed = failed or {}
    if not isinstance(returned, dict) or not isinstance(failed, dict):
        raise BatchError("invalid_account_maps", "returned and failed must be objects")

    duplicates = sorted({item for item in requested if requested.count(item) > 1})
    returned_ids = {str(key) for key in returned}
    failed_ids = {str(key) for key in failed}
    overlap = sorted(returned_ids & failed_ids)
    unknown = sorted((returned_ids | failed_ids) - set(requested))
    missing = sorted(set(requested) - returned_ids - failed_ids)
    ok = not duplicates and not overlap and not unknown and not missing
    return {
        "ok": ok,
        "requested": list(requested),
        "returned_ids": sorted(returned_ids),
        "explicitly_failed_ids": sorted(failed_ids),
        "duplicates": duplicates,
        "overlap": overlap,
        "unknown_ids": unknown,
        "missing_ids": missing,
    }


def require_complete_ids(
    requested: list[str],
    returned: dict[str, Any] | None,
    failed: dict[str, str] | None = None,
) -> dict[str, Any]:
    report = account_requested_ids(requested, returned, failed)
    if report["ok"]:
        return report
    if report["duplicates"]:
        raise BatchError("duplicate_event_ids", "requested event IDs contain duplicates")
    if report["unknown_ids"]:
        raise BatchError("unknown_event_ids", "response contains event IDs that were not requested")
    if report["overlap"]:
        raise BatchError("event_id_overlap", "event IDs cannot be both returned and failed")
    raise BatchError("silently_missing_event_ids", "requested event IDs were omitted without a structured failure")
