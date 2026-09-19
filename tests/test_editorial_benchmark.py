from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.benchmark.contract import (
    BENCHMARK_CANDIDATE_LIMIT,
    EDITORIAL_OUTPUT_SCHEMA_VERSION,
    SELECTED_COUNT,
)
from newsagent_v2.benchmark.input import (
    build_editorial_input,
    build_editorial_input_from_files,
    write_json_utf8,
)
from newsagent_v2.benchmark.validate import validate_editorial_output

REPO_ROOT = Path(__file__).resolve().parents[1]
RANKING_AUDIT = REPO_ROOT / "output" / "ranking_audit.json"
EVENT_CLUSTERS = REPO_ROOT / "output" / "event_clusters.json"


def _member(
    *,
    source: str,
    title: str,
    url: str,
    extra: dict | None = None,
) -> dict:
    row = {
        "source": source,
        "source_type": "newsroom",
        "source_role": "discovery",
        "source_authority": 0.8,
        "title": title,
        "url": url,
        "published": "Sun, 13 Sep 2026 12:00:00 +0000",
        "summary": "summary for " + title,
        "score": 1.23,
        "fingerprint": "abc",
    }
    if extra:
        row.update(extra)
    return row


def _ranking_row(event_id: str, title: str, rank_hint: float) -> dict:
    return {
        "event_id": event_id,
        "title": title,
        "representative_source": "CoinDesk",
        "representative_score": rank_hint,
        "corroboration": 0.1,
        "event_score": rank_hint + 0.1,
        "source_count": 1,
        "sources": ["CoinDesk"],
        "dimensions": {
            "freshness": 1.0,
            "security": 0.0,
            "regulatory": 0.0,
            "market": 0.0,
            "adoption": 0.0,
            "magnitude": 0.0,
            "concrete": 0.0,
            "source": 0.35,
            "speculation": 0.0,
        },
    }


def make_fixture(n: int = 15, extra_members_on: str | None = None) -> tuple[list, list]:
    ranking = []
    clusters = []
    for i in range(1, n + 1):
        event_id = f"event-{i:03d}"
        title = f"Headline {i}"
        ranking.append(_ranking_row(event_id, title, 10.0 - i * 0.1))
        members = [
            _member(
                source="CoinDesk",
                title=title,
                url=f"https://example.com/{event_id}/a",
            )
        ]
        if extra_members_on == event_id:
            members.append(
                _member(
                    source="Cointelegraph",
                    title=title + " alt",
                    url=f"https://example.com/{event_id}/b",
                )
            )
            members.append(
                _member(
                    source="The Block",
                    title=title + " third",
                    url=f"https://example.com/{event_id}/c",
                )
            )
        clusters.append(
            {
                "event_id": event_id,
                "representative": members[0],
                "members": members,
                "sources": [m["source"] for m in members],
                "source_count": len({m["source"] for m in members}),
                "similarity_reason": "test",
                "event_score": ranking[-1]["event_score"],
            }
        )
    return ranking, clusters


def _judgment(event_id: str, *, selected: bool, url: str) -> dict:
    return {
        "event_id": event_id,
        "selected": selected,
        "same_event_as": [],
        "is_current_event": True,
        "is_background_context": False,
        "semantic_category": "other",
        "speculation": {
            "is_speculative": False,
            "flags": ["none"],
        },
        "newsworthiness_reasoning": f"Reasoning for {event_id}",
        "evidence_refs": [{"url": url, "source": "CoinDesk"}],
        "confidence": 0.7,
        "rejection_reason": None if selected else "not in editorial top 5",
    }


def valid_output(benchmark: dict) -> dict:
    candidates = benchmark["candidates"]
    selected = [c["event_id"] for c in candidates[:SELECTED_COUNT]]
    judgments = []
    for candidate in candidates:
        event_id = candidate["event_id"]
        url = candidate["evidence"][0]["url"]
        judgments.append(
            _judgment(
                event_id,
                selected=event_id in selected,
                url=url,
            )
        )
    return {
        "schema_version": EDITORIAL_OUTPUT_SCHEMA_VERSION,
        "selected_event_ids": selected,
        "judgments": judgments,
    }


class EditorialInputTests(unittest.TestCase):
    def test_valid_benchmark_construction(self) -> None:
        ranking, clusters = make_fixture(20)
        payload = build_editorial_input(ranking, clusters)
        self.assertEqual(payload["candidate_count"], 15)
        first = payload["candidates"][0]
        self.assertEqual(first["event_id"], "event-001")
        self.assertEqual(first["deterministic_rank"], 1)
        self.assertEqual(first["representative_title"], ranking[0]["title"])
        self.assertEqual(first["event_score"], ranking[0]["event_score"])
        self.assertEqual(
            first["representative_score"],
            ranking[0]["representative_score"],
        )
        self.assertEqual(first["corroboration"], ranking[0]["corroboration"])
        self.assertEqual(first["source_count"], ranking[0]["source_count"])
        self.assertEqual(first["sources"], ranking[0]["sources"])
        self.assertEqual(first["dimensions"], ranking[0]["dimensions"])

    def test_exactly_15_candidates_when_at_least_15_available(self) -> None:
        ranking, clusters = make_fixture(20)
        payload = build_editorial_input(ranking, clusters)
        self.assertEqual(len(payload["candidates"]), BENCHMARK_CANDIDATE_LIMIT)
        self.assertEqual(
            [c["event_id"] for c in payload["candidates"]],
            [f"event-{i:03d}" for i in range(1, 16)],
        )

    def test_preserves_all_cluster_evidence(self) -> None:
        ranking, clusters = make_fixture(15, extra_members_on="event-002")
        payload = build_editorial_input(ranking, clusters)
        cluster_members = clusters[1]["members"]
        evidence = payload["candidates"][1]["evidence"]
        self.assertEqual(len(evidence), 3)
        self.assertEqual(len(evidence), len(cluster_members))
        for copied, original in zip(evidence, cluster_members):
            self.assertEqual(copied["source"], original["source"])
            self.assertEqual(copied["source_type"], original["source_type"])
            self.assertEqual(copied["source_role"], original["source_role"])
            self.assertEqual(
                copied["source_authority"],
                original["source_authority"],
            )
            self.assertEqual(copied["title"], original["title"])
            self.assertEqual(copied["url"], original["url"])
            self.assertEqual(copied["published"], original["published"])
            self.assertEqual(copied["summary"], original["summary"])
            self.assertNotIn("fingerprint", copied)
            self.assertNotIn("score", copied)

    def test_does_not_mutate_source_rows(self) -> None:
        ranking, clusters = make_fixture(15)
        original_score = ranking[0]["event_score"]
        original_dims = copy.deepcopy(ranking[0]["dimensions"])
        payload = build_editorial_input(ranking, clusters)
        payload["candidates"][0]["event_score"] = 99
        payload["candidates"][0]["dimensions"]["freshness"] = 99
        self.assertEqual(ranking[0]["event_score"], original_score)
        self.assertEqual(ranking[0]["dimensions"], original_dims)

    def test_utf8_without_bom(self) -> None:
        ranking, clusters = make_fixture(15)
        payload = build_editorial_input(ranking, clusters)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "editorial_input.json"
            write_json_utf8(path, payload)
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["candidate_count"], 15)


class EditorialValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        ranking, clusters = make_fixture(15)
        self.benchmark = build_editorial_input(ranking, clusters)
        self.valid = valid_output(self.benchmark)

    def test_valid_editorial_output(self) -> None:
        errors = validate_editorial_output(self.valid, self.benchmark)
        self.assertEqual(errors, [])

    def test_malformed_non_dict_structure(self) -> None:
        errors = validate_editorial_output(["not", "a", "dict"], self.benchmark)
        self.assertTrue(any("malformed structure" in e for e in errors))

    def test_missing_required_fields(self) -> None:
        errors = validate_editorial_output({"schema_version": "x"}, self.benchmark)
        self.assertTrue(any("missing required field" in e for e in errors))

    def test_invalid_event_id(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["selected_event_ids"][0] = "event-999"
        payload["judgments"][0]["event_id"] = "event-999"
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("nonexistent candidate event ID" in e for e in errors))

    def test_duplicate_selected_id(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["selected_event_ids"][1] = payload["selected_event_ids"][0]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("duplicate selected event ID" in e for e in errors))

    def test_fewer_than_five_selected_events(self) -> None:
        payload = copy.deepcopy(self.valid)
        dropped = payload["selected_event_ids"].pop()
        for judgment in payload["judgments"]:
            if judgment["event_id"] == dropped:
                judgment["selected"] = False
                judgment["rejection_reason"] = "dropped for count test"
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("wrong Top-5 count" in e for e in errors))

    def test_more_than_five_selected_events(self) -> None:
        payload = copy.deepcopy(self.valid)
        extra = payload["judgments"][5]["event_id"]
        payload["selected_event_ids"].append(extra)
        payload["judgments"][5]["selected"] = True
        payload["judgments"][5]["rejection_reason"] = None
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("wrong Top-5 count" in e for e in errors))

    def test_invalid_evidence_reference(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["evidence_refs"] = [
            {"url": "https://invented.example/not-in-cluster"}
        ]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("invalid evidence reference" in e for e in errors))

    def test_self_duplicate_relation(self) -> None:
        payload = copy.deepcopy(self.valid)
        event_id = payload["judgments"][0]["event_id"]
        payload["judgments"][0]["same_event_as"] = [event_id]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("self-duplicate relation" in e for e in errors))

    def test_malformed_confidence(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["confidence"] = 1.5
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("malformed confidence" in e for e in errors))

        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["confidence"] = "high"
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("malformed confidence" in e for e in errors))

        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["confidence"] = math.nan
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("malformed confidence" in e for e in errors))

    def test_missing_rejection_reason(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][5]["rejection_reason"] = None
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("missing rejection reason" in e for e in errors))

    def test_malformed_semantic_category(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["semantic_category"] = "not-a-category"
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("malformed semantic category" in e for e in errors))

    def test_malformed_speculation_flags(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["speculation"]["flags"] = ["made_up_flag"]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("malformed speculation flags" in e for e in errors))

    def test_missing_judgments(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"] = payload["judgments"][:3]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(any("missing judgments" in e for e in errors))

    def test_invalid_duplicate_event_reference(self) -> None:
        payload = copy.deepcopy(self.valid)
        payload["judgments"][0]["same_event_as"] = ["event-999"]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(
            any("invalid duplicate-event reference" in e for e in errors)
        )

    def test_duplicate_same_event_as_entries(self) -> None:
        payload = copy.deepcopy(self.valid)
        other_id = payload["judgments"][5]["event_id"]
        payload["judgments"][0]["same_event_as"] = [other_id, other_id]
        payload["judgments"][5]["same_event_as"] = [
            payload["judgments"][0]["event_id"]
        ]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(
            any("duplicate same_event_as entries" in e for e in errors)
        )

    def test_asymmetric_same_event_relation_rejected(self) -> None:
        payload = copy.deepcopy(self.valid)
        left = payload["judgments"][5]
        right = payload["judgments"][6]
        left["same_event_as"] = [right["event_id"]]
        right["same_event_as"] = []
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(
            any("asymmetric same-event relation" in e for e in errors)
        )

    def test_symmetric_same_event_relation_accepted(self) -> None:
        payload = copy.deepcopy(self.valid)
        left = payload["judgments"][5]
        right = payload["judgments"][6]
        left["same_event_as"] = [right["event_id"]]
        right["same_event_as"] = [left["event_id"]]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertEqual(errors, [])

    def test_symmetric_same_event_between_selected_still_rejected(self) -> None:
        payload = copy.deepcopy(self.valid)
        left = payload["judgments"][0]
        right = payload["judgments"][1]
        left["same_event_as"] = [right["event_id"]]
        right["same_event_as"] = [left["event_id"]]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertTrue(
            any("selected events marked same-event" in e for e in errors)
        )
        self.assertFalse(
            any("asymmetric same-event relation" in e for e in errors)
        )

    def test_malformed_evidence_source_types(self) -> None:
        url = self.valid["judgments"][0]["evidence_refs"][0]["url"]
        for bad_source in (123, None, "", []):
            payload = copy.deepcopy(self.valid)
            payload["judgments"][0]["evidence_refs"] = [
                {"url": url, "source": bad_source}
            ]
            errors = validate_editorial_output(payload, self.benchmark)
            self.assertTrue(
                any(
                    "source must be a non-empty string" in e
                    for e in errors
                ),
                msg=f"source={bad_source!r} errors={errors}",
            )

    def test_omitted_evidence_source_is_valid(self) -> None:
        payload = copy.deepcopy(self.valid)
        url = payload["judgments"][0]["evidence_refs"][0]["url"]
        payload["judgments"][0]["evidence_refs"] = [{"url": url}]
        errors = validate_editorial_output(payload, self.benchmark)
        self.assertEqual(errors, [])


@unittest.skipUnless(
    RANKING_AUDIT.exists() and EVENT_CLUSTERS.exists(),
    "selector output files are not present",
)
class FrozenSelectorOutputTests(unittest.TestCase):
    def test_real_outputs_yield_15_candidates_with_all_evidence(self) -> None:
        ranking = json.loads(RANKING_AUDIT.read_text(encoding="utf-8"))
        clusters = json.loads(EVENT_CLUSTERS.read_text(encoding="utf-8"))
        payload = build_editorial_input_from_files(RANKING_AUDIT, EVENT_CLUSTERS)
        self.assertGreaterEqual(len(ranking), 15)
        self.assertEqual(payload["candidate_count"], 15)
        clusters_by_id = {c["event_id"]: c for c in clusters}
        for candidate in payload["candidates"]:
            cluster = clusters_by_id[candidate["event_id"]]
            self.assertEqual(
                len(candidate["evidence"]),
                len(cluster["members"]),
            )


if __name__ == "__main__":
    unittest.main()


