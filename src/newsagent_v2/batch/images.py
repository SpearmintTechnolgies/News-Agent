"""Independent per-story image jobs. Parallel-ready; default sequential."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable


IMAGE_MAX_CONCURRENCY = 3


def run_image_jobs(
    jobs: list[dict[str, Any]],
    image_fn: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    parallel: bool = False,
    max_workers: int = IMAGE_MAX_CONCURRENCY,
) -> list[dict[str, Any]]:
    """
    Run one image job per story. Failures are recorded on that job only.

    parallel=True is safe for independent mocked jobs. Live Cloudflare
    batches should keep parallel=False unless rate limits are known.
    """
    if not jobs:
        return []

    def _run(job: dict[str, Any]) -> dict[str, Any]:
        try:
            result = image_fn(job)
            if not isinstance(result, dict):
                return {
                    "event_id": job.get("event_id"),
                    "success": False,
                    "reason": "image_invalid_result",
                    "image_request_count": 0,
                }
            payload = dict(result)
            payload.setdefault("event_id", job.get("event_id"))
            payload.setdefault("image_request_count", 1 if payload.get("success") else 0)
            return payload
        except Exception as exc:
            return {
                "event_id": job.get("event_id"),
                "success": False,
                "reason": f"image_error: {exc.__class__.__name__}",
                "image_request_count": 0,
            }

    if not parallel or len(jobs) == 1:
        return [_run(job) for job in jobs]

    workers = max(1, min(int(max_workers), len(jobs)))
    ordered: list[dict[str, Any] | None] = [None] * len(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run, job): index for index, job in enumerate(jobs)}
        for future in as_completed(futures):
            ordered[futures[future]] = future.result()
    return [item if item is not None else {"success": False, "reason": "image_missing_result"} for item in ordered]
