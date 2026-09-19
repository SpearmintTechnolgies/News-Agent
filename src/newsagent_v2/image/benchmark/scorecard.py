"""Human image-quality scorecard. Subjective fields stay null until review."""

from __future__ import annotations

from typing import Any

from newsagent_v2.image.contract import IMAGE_SCORECARD_SCHEMA_VERSION
from newsagent_v2.image.benchmark.contract import (
    OBJECTIVE_METRIC_FIELDS,
    SUBJECTIVE_SCORE_FIELDS,
)


def empty_subjective_scores() -> dict[str, None]:
    return {name: None for name in SUBJECTIVE_SCORE_FIELDS}


def build_scorecard(
    *,
    run_id: str,
    event_id: str | None,
    provider_name: str,
    model_name: str | None,
    objective: dict[str, Any],
    reviewer_notes: str | None = None,
    disqualifying_issue: str | None = None,
    preferred_output: bool | None = None,
) -> dict[str, Any]:
    metrics = {name: objective.get(name) for name in OBJECTIVE_METRIC_FIELDS}
    return {
        "schema_version": IMAGE_SCORECARD_SCHEMA_VERSION,
        "run_id": run_id,
        "event_id": event_id,
        "provider_name": provider_name,
        "model_name": model_name,
        "subjective": empty_subjective_scores(),
        "objective": metrics,
        "reviewer_notes": reviewer_notes,
        "disqualifying_issue": disqualifying_issue,
        "preferred_output": preferred_output,
        "auto_scored": False,
        "human_review_complete": False,
    }
