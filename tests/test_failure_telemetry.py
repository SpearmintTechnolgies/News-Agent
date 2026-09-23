"""Test that full failure diagnostics are persisted."""

import sys
from pathlib import Path
from datetime import datetime, timezone

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from newsagent_v2.v5_generation.run_story_adapter import GenerationJob


def test_failure_details_persisted():
    """Failure diagnostics are stored in job.metadata."""

    # Create job with failure
    job = GenerationJob(
        job_id="evt-test-123",
        event_id="evt-test",
        event={"headline": "Test"},
        state="FAILED",
        error="MECHANICS_FAILED",
    )

    # Simulate failure details being added (as would happen in _run_generation failure path)
    job.metadata["failure_details"] = {
        "failure_class": "QA_OR_GROUNDING_FAILED",
        "error": "MECHANICS_FAILED",
        "notes": "Article failed QA validation",
        "critical_codes": ["invalid_evidence_url", "claim_unknown_evidence"],
        "event_id": "evt-test",
        "job_id": "evt-test-123",
        "stage": "generation",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "sources_retrieved": 2,
        "independent_sources": 1,
        "primary_sources": 0,
        "unique_propositions": 3,
        "evidence_capacity": "LIMITED",
        "article_type": "spot",
        "native_words": 0,
        "final_words": 0,
        "writer_calls": 1,
        "repair_calls": 0,
        "expansion_calls": 0,
        "supported": 0,
        "ambiguous": 0,
        "unsupported": 0,
        "writer_model": "groq",
        "writer_provider": "groq",
        "kimi_calls": 0,
        "vertex_calls": 0,
    }

    # Serialize to dict (as would happen in save_generation_job)
    from dataclasses import asdict
    data = asdict(job)

    # Assertions
    assert "metadata" in data, "metadata should be in serialized data"
    assert "failure_details" in data["metadata"], "failure_details should be in metadata"

    details = data["metadata"]["failure_details"]
    assert details["failure_class"] == "QA_OR_GROUNDING_FAILED"
    assert details["critical_codes"] == ["invalid_evidence_url", "claim_unknown_evidence"]
    assert details["sources_retrieved"] == 2
    assert details["unique_propositions"] == 3
    assert details["evidence_capacity"] == "LIMITED"
    assert details["kimi_calls"] == 0
    assert details["vertex_calls"] == 0

    print("[PASS] Failure diagnostics persisted correctly:")
    print(f"  failure_class: {details['failure_class']}")
    print(f"  critical_codes: {details['critical_codes']}")
    print(f"  sources_retrieved: {details['sources_retrieved']}")
    print(f"  unique_propositions: {details['unique_propositions']}")
    print(f"  evidence_capacity: {details['evidence_capacity']}")
    print(f"  kimi_calls: {details['kimi_calls']}")
    print(f"  vertex_calls: {details['vertex_calls']}")


def test_to_dict_includes_metadata():
    """Job.to_dict() includes metadata with failure_details."""

    job = GenerationJob(
        job_id="evt-test-456",
        event_id="evt-test",
        event={},
        state="FAILED",
        error="INSUFFICIENT_EVIDENCE",
    )

    job.metadata["failure_details"] = {
        "failure_class": "INSUFFICIENT_EVIDENCE",
        "unique_propositions": 0,
    }

    data = job.to_dict()

    assert "metadata" in data
    assert "failure_details" in data["metadata"]
    assert data["metadata"]["failure_details"]["failure_class"] == "INSUFFICIENT_EVIDENCE"
    assert data["metadata"]["failure_details"]["unique_propositions"] == 0

    print("[PASS] Job.to_dict() includes metadata")


def test_job_without_failure():
    """Jobs without failure don't have failure_details."""

    job = GenerationJob(
        job_id="evt-test-789",
        event_id="evt-test",
        event={},
        state="REVIEW",
        article_version="v1",
    )

    data = job.to_dict()

    assert data["state"] == "REVIEW"
    assert data["article_version"] == "v1"
    # metadata exists but is empty
    assert "metadata" in data
    assert data["metadata"] == {}

    print("[PASS] Successful job serialization works")


if __name__ == "__main__":
    print("=" * 70)
    print("FAILURE TELEMETRY TESTS")
    print("=" * 70 + "\n")

    test_failure_details_persisted()
    print()
    test_to_dict_includes_metadata()
    print()
    test_job_without_failure()

    print("\n" + "=" * 70)
    print("ALL TESTS PASSED")
    print("=" * 70)
