from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from newsagent_v2.benchmark.input import build_editorial_input
from newsagent_v2.benchmark.normalize_relations import (
    normalize_same_event_relations,
)
from newsagent_v2.benchmark.prompt import SYSTEM_PROMPT
from newsagent_v2.benchmark.validate import validate_editorial_output
from tests.test_editorial_benchmark import make_fixture, valid_output

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_INPUT = REPO_ROOT / "output" / "benchmarks" / "editorial_input.json"
REPLAY_PARSED = (
    REPO_ROOT
    / "output"
    / "benchmarks"
    / "runs"
    / "20260914T052039Z-561c54d3"
    / "parsed_editorial_output.json"
)
REPLAY_RAW_PROVIDER = (
    REPO_ROOT
    / "output"
    / "benchmarks"
    / "runs"
    / "20260914T052039Z-561c54d3"
    / "raw_provider_response.json"
)


def _judgment(output: dict, event_id: str) -> dict:
    for item in output["judgments"]:
        if item["event_id"] == event_id:
            return item
    raise AssertionError(f"missing judgment {event_id}")


def _folded(text: str) -> str:
    return " ".join(text.split()).lower()


class SameEventNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        ranking, clusters = make_fixture(15)
        self.benchmark = build_editorial_input(ranking, clusters)
        self.valid = valid_output(self.benchmark)
        self.left_id = self.valid["judgments"][5]["event_id"]
        self.right_id = self.valid["judgments"][6]["event_id"]

    def test_one_way_becomes_symmetric(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        snapshot = copy.deepcopy(payload)
        normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(
            _judgment(payload, self.left_id)["same_event_as"],
            snapshot["judgments"][5]["same_event_as"],
        )
        self.assertIn(
            self.right_id,
            _judgment(normalized, self.left_id)["same_event_as"],
        )
        self.assertIn(
            self.left_id,
            _judgment(normalized, self.right_id)["same_event_as"],
        )
        self.assertTrue(stats["normalization_applied"])
        self.assertEqual(stats["mirrored_relation_count"], 1)
        self.assertIn(
            sorted([self.left_id, self.right_id]),
            [sorted(pair) for pair in stats["mirrored_relation_pairs"]],
        )
        errors = validate_editorial_output(normalized, self.benchmark)
        self.assertEqual(errors, [])

    def test_already_symmetric_unchanged(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        _judgment(payload, self.right_id)["same_event_as"] = [self.left_id]
        normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(
            _judgment(normalized, self.left_id)["same_event_as"],
            [self.right_id],
        )
        self.assertEqual(
            _judgment(normalized, self.right_id)["same_event_as"],
            [self.left_id],
        )
        self.assertEqual(stats["mirrored_relation_count"], 0)
        self.assertFalse(stats["normalization_applied"])

    def test_duplicate_relations_normalize_deterministically(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [
            self.right_id,
            self.right_id,
        ]
        _judgment(payload, self.right_id)["same_event_as"] = [self.left_id]
        normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(
            _judgment(normalized, self.left_id)["same_event_as"],
            [self.right_id],
        )
        self.assertEqual(stats["deduplicated_relation_count"], 1)
        self.assertTrue(stats["normalization_applied"])

    def test_self_relation_not_silently_repaired(self) -> None:
        payload = copy.deepcopy(self.valid)
        event_id = payload["judgments"][0]["event_id"]
        _judgment(payload, event_id)["same_event_as"] = [event_id]
        normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(
            _judgment(normalized, event_id)["same_event_as"],
            [event_id],
        )
        self.assertEqual(stats["mirrored_relation_count"], 0)
        errors = validate_editorial_output(normalized, self.benchmark)
        self.assertTrue(any("self-duplicate relation" in e for e in errors))

    def test_unknown_id_not_silently_repaired(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = ["event-999"]
        normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(
            _judgment(normalized, self.left_id)["same_event_as"],
            ["event-999"],
        )
        self.assertEqual(stats["mirrored_relation_count"], 0)
        self.assertFalse(any(j["event_id"] == "event-999" for j in normalized["judgments"]))
        errors = validate_editorial_output(normalized, self.benchmark)
        self.assertTrue(
            any("invalid duplicate-event reference" in e for e in errors)
        )

    def test_unrelated_candidates_untouched(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        unrelated_id = payload["judgments"][7]["event_id"]
        before = copy.deepcopy(_judgment(payload, unrelated_id))
        normalized, _stats = normalize_same_event_relations(payload)
        self.assertEqual(_judgment(normalized, unrelated_id), before)

    def test_non_relation_fields_unchanged(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        snapshot = copy.deepcopy(payload)
        normalized, _stats = normalize_same_event_relations(payload)
        skip = {"same_event_as"}
        for raw, norm in zip(snapshot["judgments"], normalized["judgments"]):
            for key in raw:
                if key in skip:
                    continue
                self.assertEqual(norm[key], raw[key], msg=key)
        self.assertEqual(
            normalized["selected_event_ids"],
            snapshot["selected_event_ids"],
        )
        self.assertEqual(normalized["schema_version"], snapshot["schema_version"])

    def test_raw_validator_still_rejects_asymmetric(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("asymmetric same-event relation" in e for e in errors))

    def test_two_selected_same_event_still_fail(self) -> None:
        payload = copy.deepcopy(self.valid)
        left = payload["judgments"][0]["event_id"]
        right = payload["judgments"][1]["event_id"]
        _judgment(payload, left)["same_event_as"] = [right]
        normalized, _stats = normalize_same_event_relations(payload)
        self.assertIn(left, _judgment(normalized, right)["same_event_as"])
        errors = validate_editorial_output(normalized, self.benchmark)
        self.assertTrue(any("selected events marked same-event" in e for e in errors))

    def test_normalization_telemetry_reports_mirrored_relations(self) -> None:
        payload = copy.deepcopy(self.valid)
        _judgment(payload, self.left_id)["same_event_as"] = [self.right_id]
        _normalized, stats = normalize_same_event_relations(payload)
        self.assertEqual(stats["mirrored_relation_count"], 1)
        self.assertEqual(
            stats["normalized_relation_pairs"],
            [sorted([self.left_id, self.right_id])],
        )


class PromptSameEventTests(unittest.TestCase):
    def test_prompt_distinguishes_same_event_from_follow_up(self) -> None:
        text = _folded(SYSTEM_PROMPT)
        self.assertIn("same underlying news event", text)
        self.assertIn("follow-up developments", text)
        self.assertIn("same exploit", text)
        self.assertIn("separate news events", text)
        self.assertIn("if a lists b in same_event_as, b must list a", text)


@unittest.skipUnless(REPLAY_PARSED.exists() and FROZEN_INPUT.exists(), "replay artifacts missing")
class OfflineReplayTests(unittest.TestCase):
    def test_replay_raw_vs_normalized_without_network(self) -> None:
        raw = json.loads(REPLAY_PARSED.read_text(encoding="utf-8"))
        frozen = json.loads(FROZEN_INPUT.read_text(encoding="utf-8"))
        original = copy.deepcopy(raw)
        raw_errors = validate_editorial_output(raw, frozen)
        normalized, stats = normalize_same_event_relations(raw)
        self.assertEqual(raw, original)
        if REPLAY_RAW_PROVIDER.exists():
            provider = json.loads(REPLAY_RAW_PROVIDER.read_text(encoding="utf-8"))
            again = json.loads(REPLAY_RAW_PROVIDER.read_text(encoding="utf-8"))
            self.assertEqual(provider, again)
        self.assertTrue(any("asymmetric same-event relation" in e for e in raw_errors))
        norm_errors = validate_editorial_output(normalized, frozen)
        self.assertEqual(norm_errors, [])
        self.assertGreaterEqual(stats["mirrored_relation_count"], 2)
        pairs = {tuple(pair) for pair in stats["normalized_relation_pairs"]}
        self.assertIn(("event-003", "event-044"), pairs)
        self.assertIn(("event-032", "event-040"), pairs)


if __name__ == "__main__":
    unittest.main()


