"""
Deterministic validator for editorial-output-v1.

Rejects structurally invalid model output. Does not call AI and does
not score editorial quality.
"""

from __future__ import annotations

import math
from typing import Any

from .contract import (
    EDITORIAL_OUTPUT_SCHEMA_VERSION,
    REQUIRED_JUDGMENT_FIELDS,
    REQUIRED_OUTPUT_FIELDS,
    REQUIRED_SPECULATION_FIELDS,
    SELECTED_COUNT,
    SEMANTIC_CATEGORIES,
    SPECULATION_FLAGS,
)


def _err(errors: list[str], message: str) -> None:
    errors.append(message)


def _candidate_index(benchmark_input: dict[str, Any]) -> dict[str, dict]:
    candidates = benchmark_input.get("candidates")
    if not isinstance(candidates, list):
        return {}
    index: dict[str, dict] = {}
    for candidate in candidates:
        if isinstance(candidate, dict) and isinstance(
            candidate.get("event_id"), str
        ):
            index[candidate["event_id"]] = candidate
    return index


def _evidence_keys(candidate: dict[str, Any]) -> set[tuple[str, str | None]]:
    keys: set[tuple[str, str | None]] = set()
    for item in candidate.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url:
            continue
        source = item.get("source")
        keys.add((url, source if isinstance(source, str) else None))
        keys.add((url, None))
    return keys


def _valid_confidence(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(float(value)):
        return False
    return 0.0 <= float(value) <= 1.0


def validate_editorial_output(
    output: Any,
    benchmark_input: dict[str, Any],
) -> list[str]:
    errors: list[str] = []

    if not isinstance(output, dict):
        _err(errors, "malformed structure: output must be a JSON object")
        return errors

    for field in REQUIRED_OUTPUT_FIELDS:
        if field not in output:
            _err(errors, f"missing required field: {field}")

    if errors:
        return errors

    if output.get("schema_version") != EDITORIAL_OUTPUT_SCHEMA_VERSION:
        _err(
            errors,
            "malformed schema_version: "
            f"expected {EDITORIAL_OUTPUT_SCHEMA_VERSION!r}",
        )

    candidates = _candidate_index(benchmark_input)
    candidate_ids = set(candidates)

    selected_ids = output.get("selected_event_ids")
    if not isinstance(selected_ids, list):
        _err(errors, "malformed selected_event_ids: must be a list")
        selected_ids = []
    else:
        if len(selected_ids) != SELECTED_COUNT:
            _err(
                errors,
                "wrong Top-5 count: "
                f"expected {SELECTED_COUNT}, got {len(selected_ids)}",
            )
        if len(selected_ids) != len(set(selected_ids)):
            _err(errors, "duplicate selected event ID")
        for event_id in selected_ids:
            if not isinstance(event_id, str):
                _err(errors, "malformed selected event ID: must be a string")
            elif event_id not in candidate_ids:
                _err(
                    errors,
                    f"nonexistent candidate event ID: {event_id!r}",
                )

    selected_id_set = {
        event_id for event_id in selected_ids if isinstance(event_id, str)
    }

    judgments = output.get("judgments")
    if not isinstance(judgments, list):
        _err(errors, "malformed judgments: must be a list")
        return errors

    seen_judgment_ids: list[str] = []

    for i, judgment in enumerate(judgments):
        prefix = f"judgment[{i}]"
        if not isinstance(judgment, dict):
            _err(errors, f"{prefix}: malformed structure: must be an object")
            continue

        missing = [
            field
            for field in REQUIRED_JUDGMENT_FIELDS
            if field not in judgment
        ]
        if missing:
            _err(
                errors,
                f"{prefix}: missing required fields: {', '.join(missing)}",
            )
            continue

        event_id = judgment.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            _err(errors, f"{prefix}: malformed event_id")
            continue
        if event_id not in candidate_ids:
            _err(
                errors,
                f"{prefix}: nonexistent candidate event ID: {event_id!r}",
            )
            continue
        if event_id in seen_judgment_ids:
            _err(errors, f"{prefix}: duplicate judgment for {event_id!r}")
        seen_judgment_ids.append(event_id)

        selected = judgment.get("selected")
        if not isinstance(selected, bool):
            _err(errors, f"{prefix}: malformed selected: must be boolean")
            selected = False

        if selected and event_id not in selected_id_set:
            _err(
                errors,
                f"{prefix}: contradiction: selected true but "
                f"{event_id!r} not in selected_event_ids",
            )
        if not selected and event_id in selected_id_set:
            _err(
                errors,
                f"{prefix}: contradiction: selected false but "
                f"{event_id!r} is in selected_event_ids",
            )

        same_event_as = judgment.get("same_event_as")
        if not isinstance(same_event_as, list):
            _err(errors, f"{prefix}: malformed same_event_as: must be a list")
            same_event_as = []
        else:
            string_ids = [
                other_id
                for other_id in same_event_as
                if isinstance(other_id, str)
            ]
            if len(string_ids) != len(set(string_ids)):
                _err(
                    errors,
                    f"{prefix}: duplicate same_event_as entries",
                )
            for other_id in same_event_as:
                if not isinstance(other_id, str):
                    _err(
                        errors,
                        f"{prefix}: invalid duplicate-event reference: "
                        "IDs must be strings",
                    )
                    continue
                if other_id == event_id:
                    _err(
                        errors,
                        f"{prefix}: self-duplicate relation",
                    )
                elif other_id not in candidate_ids:
                    _err(
                        errors,
                        f"{prefix}: invalid duplicate-event reference: "
                        f"{other_id!r}",
                    )

        is_current = judgment.get("is_current_event")
        is_background = judgment.get("is_background_context")
        if not isinstance(is_current, bool):
            _err(errors, f"{prefix}: malformed is_current_event")
        if not isinstance(is_background, bool):
            _err(errors, f"{prefix}: malformed is_background_context")
        if is_current is True and is_background is True:
            _err(
                errors,
                f"{prefix}: contradiction: is_current_event and "
                "is_background_context cannot both be true",
            )

        category = judgment.get("semantic_category")
        if not isinstance(category, str) or category not in SEMANTIC_CATEGORIES:
            _err(errors, f"{prefix}: malformed semantic category")

        speculation = judgment.get("speculation")
        if not isinstance(speculation, dict):
            _err(errors, f"{prefix}: malformed speculation flags")
        else:
            missing_spec = [
                field
                for field in REQUIRED_SPECULATION_FIELDS
                if field not in speculation
            ]
            if missing_spec:
                _err(
                    errors,
                    f"{prefix}: malformed speculation flags: missing "
                    + ", ".join(missing_spec),
                )
            else:
                is_speculative = speculation.get("is_speculative")
                flags = speculation.get("flags")
                if not isinstance(is_speculative, bool):
                    _err(
                        errors,
                        f"{prefix}: malformed speculation flags: "
                        "is_speculative must be boolean",
                    )
                if not isinstance(flags, list) or any(
                    not isinstance(flag, str) for flag in flags
                ):
                    _err(
                        errors,
                        f"{prefix}: malformed speculation flags: "
                        "flags must be a list of strings",
                    )
                    flags = []
                else:
                    unknown = [
                        flag for flag in flags if flag not in SPECULATION_FLAGS
                    ]
                    if unknown:
                        _err(
                            errors,
                            f"{prefix}: malformed speculation flags: "
                            f"unknown {unknown!r}",
                        )
                    if "none" in flags and len(flags) > 1:
                        _err(
                            errors,
                            f"{prefix}: contradiction: speculation flag "
                            "'none' cannot mix with other flags",
                        )
                    non_none = [flag for flag in flags if flag != "none"]
                    if is_speculative is True and not non_none:
                        _err(
                            errors,
                            f"{prefix}: contradiction: is_speculative true "
                            "requires a non-none speculation flag",
                        )
                    if is_speculative is False and non_none:
                        _err(
                            errors,
                            f"{prefix}: contradiction: is_speculative false "
                            "cannot include non-none flags",
                        )

        reasoning = judgment.get("newsworthiness_reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            _err(errors, f"{prefix}: missing newsworthiness reasoning")

        evidence_refs = judgment.get("evidence_refs")
        allowed = _evidence_keys(candidates.get(event_id, {}))
        if not isinstance(evidence_refs, list):
            _err(errors, f"{prefix}: malformed evidence_refs: must be a list")
        else:
            if selected and not evidence_refs:
                _err(
                    errors,
                    f"{prefix}: selected event missing evidence references",
                )
            for j, ref in enumerate(evidence_refs):
                if not isinstance(ref, dict) or "url" not in ref:
                    _err(
                        errors,
                        f"{prefix}: invalid evidence reference [{j}]",
                    )
                    continue
                url = ref.get("url")
                if not isinstance(url, str) or not url:
                    _err(
                        errors,
                        f"{prefix}: invalid evidence reference [{j}]",
                    )
                    continue
                if "source" in ref:
                    source = ref.get("source")
                    if not isinstance(source, str) or not source.strip():
                        _err(
                            errors,
                            f"{prefix}: malformed evidence reference "
                            f"[{j}]: source must be a non-empty string",
                        )
                        continue
                    if (url, source) not in allowed:
                        _err(
                            errors,
                            f"{prefix}: invalid evidence reference [{j}]: "
                            f"source {source!r} does not match {url!r}",
                        )
                elif (url, None) not in allowed:
                    _err(
                        errors,
                        f"{prefix}: invalid evidence reference [{j}]: "
                        f"{url!r} is not evidence on {event_id}",
                    )

        if not _valid_confidence(judgment.get("confidence")):
            _err(errors, f"{prefix}: malformed confidence")

        rejection = judgment.get("rejection_reason")
        if selected:
            if rejection is not None:
                _err(
                    errors,
                    f"{prefix}: contradiction: selected event must not "
                    "have a rejection_reason",
                )
        else:
            if not isinstance(rejection, str) or not rejection.strip():
                _err(errors, f"{prefix}: missing rejection reason")

    judged_ids = set(seen_judgment_ids)
    missing_ids = sorted(candidate_ids - judged_ids)
    if missing_ids:
        _err(
            errors,
            "missing judgments for: " + ", ".join(missing_ids),
        )

    extra_ids = sorted(judged_ids - candidate_ids)
    if extra_ids:
        _err(
            errors,
            "judgments for nonexistent candidates: " + ", ".join(extra_ids),
        )

    same_map: dict[str, set[str]] = {}
    for judgment in judgments:
        if not isinstance(judgment, dict):
            continue
        event_id = judgment.get("event_id")
        if not isinstance(event_id, str) or event_id not in candidate_ids:
            continue
        others = judgment.get("same_event_as")
        if not isinstance(others, list):
            same_map[event_id] = set()
            continue
        same_map[event_id] = {
            other
            for other in others
            if isinstance(other, str)
            and other in candidate_ids
            and other != event_id
        }

    reported_asymmetric: set[tuple[str, str]] = set()
    for event_id, others in same_map.items():
        for other in others:
            if event_id in same_map.get(other, set()):
                continue
            pair = tuple(sorted((event_id, other)))
            if pair in reported_asymmetric:
                continue
            reported_asymmetric.add(pair)
            _err(
                errors,
                "asymmetric same-event relation: "
                f"{event_id} ~ {other}",
            )

    selected_same: dict[str, set[str]] = {}
    for event_id, others in same_map.items():
        if event_id not in selected_id_set:
            continue
        selected_same[event_id] = others

    for event_id, others in selected_same.items():
        overlap = others.intersection(selected_id_set)
        if overlap:
            _err(
                errors,
                "contradiction: selected events marked same-event: "
                f"{event_id} ~ {sorted(overlap)}",
            )

    return errors
