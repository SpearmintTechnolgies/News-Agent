"""Tests for V5 Generation Wiring components.

Tests cover:
- Persistent Store (discovery runs, generation jobs, session state)
- Provider Preflight (credential readiness)
- Cost Ledger (cost tracking)
- Generation Worker (async generation with progress)
- V5 Callbacks (RUN STORY wiring)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator
from unittest.mock import Mock, MagicMock, patch

import pytest

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.v5_generation.persistent_store import (
    PersistentV5Store,
    DiscoveryRun,
    GenerationJob,
    DATA_ROOT,
)
from newsagent_v2.v5_generation.provider_preflight import (
    ProviderPreflight,
    ProviderStatus,
)
from newsagent_v2.v5_generation.cost_ledger import (
    CostLedger,
    CostEntry,
    CostStatus,
)
from newsagent_v2.v5_generation.generation_worker import (
    GenerationWorker,
)


# ============== Fixtures ==============

@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Create temporary state directory."""
    return tmp_path / "v5_state"


@pytest.fixture
def persistent_store(tmp_state_dir: Path) -> Generator[PersistentV5Store, None, None]:
    """Create temporary persistent store."""
    store = PersistentV5Store(tmp_state_dir)
    yield store


@pytest.fixture
def mock_event() -> NewsEvent:
    """Create a mock NewsEvent."""
    return NewsEvent(
        event_id="evt-test-001",
        canonical_title="Test Event Headline",
    )


# ============== PersistentStore Tests ==============

class TestPersistentStore:
    """Test PersistentStore functionality."""

    def test_save_and_retrieve_discovery_run(self, persistent_store: PersistentStore) -> None:
        """Test saving and retrieving discovery runs."""
        run = DiscoveryRun(
            run_id="run-001",
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_ids=["evt-1", "evt-2", "evt-3"],
            metadata={"source_count": 5},
        )

        persistent_store.save_discovery_run(run)

        retrieved = persistent_store.get_discovery_run("run-001")
        assert retrieved is not None
        assert retrieved.run_id == "run-001"
        assert retrieved.event_ids == ["evt-1", "evt-2", "evt-3"]
        assert retrieved.metadata["source_count"] == 5

    def test_list_discovery_runs_sorted(self, persistent_store: PersistentStore) -> None:
        """Test discovery runs are sorted by timestamp."""
        runs = [
            DiscoveryRun(
                run_id=f"run-{i:03d}",
                timestamp=f"2024-01-{i+1:02d}T12:00:00+00:00",
                event_ids=[f"evt-{i}"],
            )
            for i in range(3)
        ]
        # Save in reverse order
        for run in reversed(runs):
            persistent_store.save_discovery_run(run)

        retrieved = persistent_store.list_discovery_runs()
        assert len(retrieved) == 3
        # Should be newest first
        assert retrieved[0].run_id == "run-002"
        assert retrieved[2].run_id == "run-000"

    def test_save_and_retrieve_generation_job(self, persistent_store: PersistentStore) -> None:
        """Test saving and retrieving generation jobs."""
        job = GenerationJob(
            job_id="job-001",
            event_id="evt-001",
            discovery_run_id="run-001",
            state=GenerationState.REQUESTING,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            article_version="v1",
        )

        persistent_store.save_generation_job(job)

        retrieved = persistent_store.get_generation_job("job-001")
        assert retrieved is not None
        assert retrieved.job_id == "job-001"
        assert retrieved.event_id == "evt-001"
        assert retrieved.state == GenerationState.REQUESTING

    def test_get_job_by_event_id_returns_most_recent(self, persistent_store: PersistentStore) -> None:
        """Test getting latest job for an event."""
        jobs = [
            GenerationJob(
                job_id=f"job-{i:03d}",
                event_id="evt-same",
                discovery_run_id=None,
                state=GenerationState.SUCCEEDED,
                created_at=f"2024-01-{i+1:02d}T12:00:00+00:00",
                updated_at=f"2024-01-{i+1:02d}T12:00:00+00:00",
            )
            for i in range(3)
        ]

        for job in jobs:
            persistent_store.save_generation_job(job)

        retrieved = persistent_store.get_job_by_event_id("evt-same")
        assert retrieved is not None
        assert retrieved.job_id == "job-002"  # Most recent

    def test_get_active_jobs_filters_terminal_states(self, persistent_store: PersistentStore) -> None:
        """Test active jobs excludes terminal states."""
        jobs = [
            GenerationJob(
                job_id=f"job-{state}",
                event_id="evt-001",
                discovery_run_id=None,
                state=state,
                created_at=datetime.now(timezone.utc).isoformat(),
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            for state in [
                GenerationState.REQUESTING,
                GenerationState.WRITING,
                GenerationState.SUCCEEDED,
                GenerationState.FAILED,
            ]
        ]

        for job in jobs:
            persistent_store.save_generation_job(job)

        active = persistent_store.get_active_jobs()
        active_states = {j.state for j in active}

        assert GenerationState.SUCCEEDED not in active_states
        assert GenerationState.FAILED not in active_states
        assert GenerationState.REQUESTING in active_states
        assert GenerationState.WRITING in active_states

    def test_has_active_job_for_event(self, persistent_store: PersistentStore) -> None:
        """Test checking for active job for an event."""
        # Create an active job
        active_job = GenerationJob(
            job_id="job-active",
            event_id="evt-active",
            discovery_run_id=None,
            state=GenerationState.WRITING,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        persistent_store.save_generation_job(active_job)

        # Create a completed job
        completed_job = GenerationJob(
            job_id="job-done",
            event_id="evt-done",
            discovery_run_id=None,
            state=GenerationState.SUCCEEDED,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        persistent_store.save_generation_job(completed_job)

        assert persistent_store.has_active_job_for_event("evt-active") is True
        assert persistent_store.has_active_job_for_event("evt-done") is False
        assert persistent_store.has_active_job_for_event("evt-unknown") is False

    def test_session_state_save_and_retrieve(self, persistent_store: PersistentStore) -> None:
        """Test saving and retrieving session state."""
        session = SessionState(
            run_id="run-001",
            followed_ids={"evt-1", "evt-2"},
            ignored_ids={"evt-3"},
            selected_ids={"evt-1"},
        )

        persistent_store.save_session_state(session)

        retrieved = persistent_store.get_session_state("run-001")
        assert retrieved is not None
        assert retrieved.run_id == "run-001"
        assert retrieved.followed_ids == {"evt-1", "evt-2"}
        assert retrieved.ignored_ids == {"evt-3"}
        assert retrieved.selected_ids == {"evt-1"}

    def test_get_or_create_session_creates_new_if_missing(self, persistent_store: PersistentStore) -> None:
        """Test get_or_create creates new session when missing."""
        session = persistent_store.get_or_create_session("run-new")
        assert session.run_id == "run-new"
        assert session.followed_ids == set()
        assert session.ignored_ids == set()
        assert session.selected_ids == set()

    def test_atomic_write_survives_crash(self, tmp_state_dir: Path) -> None:
        """Test that atomic writes don't leave corrupt files."""
        store = PersistentV5Store(tmp_state_dir)

        job = GenerationJob(
            job_id="job-crash-test",
            event_id="evt-001",
            discovery_run_id=None,
            state=GenerationState.REQUESTING,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )

        store.save_generation_job(job)

        # Verify no temp files left behind
        temp_files = list(tmp_state_dir.glob("*.tmp"))
        assert len(temp_files) == 0

        # Verify file is valid JSON
        job_file = tmp_state_dir / "job_job-crash-test.json"
        assert job_file.exists()
        with open(job_file) as f:
            data = json.load(f)
        assert data["job_id"] == "job-crash-test"


# ============== ProviderPreflight Tests ==============

class TestProviderPreflight:
    """Test ProviderPreflight functionality."""

    def test_check_groq_key_missing(self) -> None:
        """Test GROQ check when key is missing."""
        preflight = ProviderPreflight(environ={})
        status = preflight.check_groq()

        assert status.name == "GROQ_API_KEY"
        assert status.present is False

    def test_check_groq_key_present(self) -> None:
        """Test GROQ check when key is present."""
        preflight = ProviderPreflight(environ={
            "GROQ_API_KEY": "gsk_test_api_key_12345",
        })
        status = preflight.check_groq()

        assert status.name == "GROQ_API_KEY"
        assert status.present is True
        assert "gsk_" in status.masked_value
        assert "..." in status.masked_value
        assert "key_12345" not in status.masked_value  # Should be masked

    def test_check_google_credentials_missing(self) -> None:
        """Test Google credentials check when missing."""
        preflight = ProviderPreflight(environ={})
        status = preflight.check_google_credentials()

        assert status.name == "GOOGLE_APPLICATION_CREDENTIALS"
        assert status.present is False

    def test_check_google_credentials_file_not_found(self) -> None:
        """Test Google credentials check when file doesn't exist."""
        preflight = ProviderPreflight(environ={
            "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/path/creds.json",
        })
        status = preflight.check_google_credentials()

        assert status.present is False
        assert "File not found" in (status.error or "")

    def test_check_google_credentials_file_exists(self, tmp_path: Path) -> None:
        """Test Google credentials check when file exists."""
        cred_file = tmp_path / "creds.json"
        cred_file.write_text("{}")

        preflight = ProviderPreflight(environ={
            "GOOGLE_APPLICATION_CREDENTIALS": str(cred_file),
        })
        status = preflight.check_google_credentials()

        assert status.present is True
        assert status.masked_value == "creds.json"

    def test_run_all_checks_completeness(self) -> None:
        """Test preflight runs all checks."""
        preflight = ProviderPreflight(environ={
            "GROQ_API_KEY": "gsk_test",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
        })
        report = preflight.run_all_checks()

        credential_names = {c.name for c in report.credentials}
        assert credential_names == {
            "GROQ_API_KEY",
            "NEWSAGENT_V2_VERTEX_PROJECT",
            "NEWSAGENT_V2_VERTEX_LOCATION",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }

    def test_readiness_summary_all_ready(self) -> None:
        """Test readiness when all providers are ready."""
        preflight = ProviderPreflight(environ={
            "GROQ_API_KEY": "gsk_test_key",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/some/path/creds.json",
        })

        summary = preflight.get_readiness_summary()

        assert summary["overall"] is True
        assert summary["can_write"] is True

    def test_readiness_summary_no_writing(self) -> None:
        """Test readiness when no writing providers available."""
        preflight = ProviderPreflight(environ={})

        summary = preflight.get_readiness_summary()

        assert summary["overall"] is False
        assert summary["can_write"] is False


class TestCredentialHelpers:
    """Test credential helper functions."""

    def test_mask_credential_short(self) -> None:
        """Test masking short credentials."""
        assert mask_credential("abc") == "****"
        assert mask_credential("") == ""
        assert mask_credential(None) == ""

    def test_mask_credential_long(self) -> None:
        """Test masking long credentials."""
        key = "TEST_GROQ_SECRET_KEY"
        masked = mask_credential(key, prefix_len=4, suffix_len=4)
        assert masked.startswith("gsk_")
        assert masked.endswith("2345")
        assert "..." in masked

    def test_check_file_exists_with_existing_file(self, tmp_path: Path) -> None:
        """Test file check with existing file."""
        file = tmp_path / "exists.txt"
        file.write_text("content")

        exists, error = check_file_exists(str(file))
        assert exists is True
        assert error == ""

    def test_check_file_exists_with_missing_file(self) -> None:
        """Test file check with missing file."""
        exists, error = check_file_exists("/nonexistent/path/file.json")
        assert exists is False
        assert "File not found" in error

    def test_check_file_exists_with_none(self) -> None:
        """Test file check with None path."""
        exists, error = check_file_exists(None)
        assert exists is False
        assert "not set" in error


# ============== CostLedger Tests ==============

class TestCostLedger:
    """Test CostLedger functionality."""

    def test_start_entry_creates_pending_entry(self) -> None:
        """Test starting a cost entry."""
        ledger = CostLedger()

        entry = ledger.start_entry(
            provider="groq",
            model="llama-3.3-70b",
            operation="write",
        )

        assert entry.provider == "groq"
        assert entry.operation == "write"
        assert entry.cost_status == CostStatus.PENDING
        assert entry.entry_id.startswith("cost_")

    def test_finalize_entry_updates_costs(self) -> None:
        """Test finalizing an entry with costs."""
        ledger = CostLedger()

        entry = ledger.start_entry(
            provider="groq",
            model="llama-3.3-70b",
            operation="write",
        )

        finalized = ledger.finalize_entry(
            entry_id=entry.entry_id,
            prompt_tokens=1000,
            completion_tokens=500,
            latency_ms=1500,
            cost_inr=0.009,
        )

        assert finalized is not None
        assert finalized.prompt_tokens == 1000
        assert finalized.completion_tokens == 500
        assert finalized.total_tokens == 1500
        assert finalized.cost_inr == 0.009
        assert finalized.cost_status == CostStatus.KNOWN

    def test_finalize_unknown_cost(self) -> None:
        """Test finalizing with unknown cost."""
        ledger = CostLedger()

        entry = ledger.start_entry(
            provider="groq",
            model="llama-3.3-70b",
            operation="write",
        )

        finalized = ledger.finalize_entry(
            entry_id=entry.entry_id,
            prompt_tokens=1000,
            completion_tokens=500,
            cost_inr=None,  # Unknown
        )

        assert finalized is not None
        assert finalized.cost_inr is None
        assert finalized.cost_status == CostStatus.UNKNOWN

    def test_record_complete_call(self) -> None:
        """Test recording a complete call in one step."""
        ledger = CostLedger()

        entry = ledger.record_call(
            provider="vertex",
            model="gemini-3.1-flash",
            operation="image_gen",
            prompt_tokens=200,
            completion_tokens=0,
            latency_ms=3000,
            cost_inr=0.0003,
        )

        assert entry.total_tokens == 200
        assert entry.cost_inr == 0.0003
        assert entry.cost_status == CostStatus.KNOWN

    def test_get_report_calculates_totals(self) -> None:
        """Test report calculation."""
        ledger = CostLedger()

        # Record multiple calls
        for i in range(3):
            ledger.record_call(
                provider="groq",
                model="llama-3.3-70b",
                operation="write",
                prompt_tokens=1000,
                completion_tokens=500,
                cost_inr=0.009,
            )

        report = ledger.get_report()

        assert report.total_entries == 3
        assert report.total_tokens == 4500  # 3 * 1500
        assert report.total_known_cost_inr == 0.027  # 3 * 0.009

    def test_estimate_unknown_costs(self) -> None:
        """Test estimating costs for unknown entries."""
        ledger = CostLedger()

        # Record an unknown cost entry
        ledger.record_call(
            provider="groq",
            model="llama-3.3-70b",
            operation="write",
            prompt_tokens=2000,
            completion_tokens=1000,
            cost_inr=None,
        )

        report = ledger.get_report()

        assert report.unknown_count == 1
        assert report.total_known_cost_inr == 0.0
        # Should have some estimated cost based on pricing
        assert report.total_estimated_cost_inr > 0

    def test_persistence(self, tmp_path: Path) -> None:
        """Test cost ledger persists to disk."""
        persist_path = tmp_path / "costs.json"

        ledger1 = CostLedger(persist_path=persist_path)
        ledger1.record_call(
            provider="groq",
            model="llama-3.3-70b",
            operation="write",
            prompt_tokens=1000,
            completion_tokens=500,
            cost_inr=0.009,
        )

        # Create new ledger instance
        ledger2 = CostLedger(persist_path=persist_path)
        entry = ledger2.get_entry(entry_id=ledger1._entries[list(ledger1._entries.keys())[0]].entry_id)
        assert entry is not None
        assert entry.provider == "groq"


# ============== GenerationWorker Tests ==============

class TestGenerationState:
    """Test GenerationState constants."""

    def test_state_machine_flow(self) -> None:
        """Test state machine order is correct."""
        states = [
            GenerationState.NOT_REQUESTED,
            GenerationState.RESERVED,
            GenerationState.REQUESTING,
            GenerationState.RESEARCHING,
            GenerationState.WRITING,
            GenerationState.QA,
            GenerationState.IMAGE_GEN,
            GenerationState.SUCCEEDED,
            GenerationState.FAILED,
        ]

        # Verify all states are unique
        assert len(states) == len(set(states))

        # Verify terminal states
        assert GenerationState.SUCCEEDED in GenerationState.TERMINAL_STATES
        assert GenerationState.FAILED in GenerationState.TERMINAL_STATES
        assert len(GenerationState.TERMINAL_STATES) == 2

    def test_active_states_excludes_terminal(self) -> None:
        """Test active states excludes terminal."""
        for state in GenerationState.TERMINAL_STATES:
            assert state not in GenerationState.ACTIVE_STATES

        for state in GenerationState.ACTIVE_STATES:
            assert state not in GenerationState.TERMINAL_STATES


class TestGenerationWorker:
    """Test GenerationWorker functionality."""

    def test_can_start_generation_controlled_e2e(self, tmp_state_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test generation blocked in controlled E2E mode."""
        monkeypatch.setenv("NEWSAGENT_V5_CONTROLLED_E2E", "true")

        worker = GenerationWorker(persistent_store=PersistentStore(tmp_state_dir))

        can_start, reason = worker.can_start_generation()
        assert can_start is False
        assert "Controlled E2E mode" in reason

    def test_can_start_generation_respects_max_active(self, tmp_state_dir: Path) -> None:
        """Test generation respects max active limit."""
        store = PersistentV5Store(tmp_state_dir)

        # Create active jobs at limit (default max is 1)
        job = GenerationJob(
            job_id="job-active-0",
            event_id="evt-0",
            discovery_run_id=None,
            state=GenerationState.WRITING,
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        store.save_generation_job(job)

        worker = GenerationWorker(persistent_store=store)

        can_start, reason = worker.can_start_generation()
        assert can_start is False
        assert "Max active generations reached" in reason

    def test_request_generation_controlled_e2e_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test request generation in controlled E2E mode."""
        monkeypatch.setenv("NEWSAGENT_V5_CONTROLLED_E2E", "true")

        worker = GenerationWorker()
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-test"

        result = worker.request_generation(mock_event)

        assert result["ok"] is False
        assert result["controlled_e2e"] is True
        assert "automatic generation disabled" in result["message"]

    def test_request_generation_returns_immediately(self, persistent_store: PersistentStore) -> None:
        """Test request generation returns immediately."""
        worker = GenerationWorker(persistent_store=persistent_store)
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-test"
        mock_event.canonical_title = "Test Headline"

        result = worker.request_generation(mock_event)

        # Should return immediately with job info
        assert "ok" in result
        assert "state" in result

    def test_request_generation_idempotent(self, persistent_store: PersistentStore) -> None:
        """Test duplicate request returns existing job."""
        worker = GenerationWorker(persistent_store=persistent_store)
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-same"
        mock_event.canonical_title = "Test Headline"

        # First request
        result1 = worker.request_generation(mock_event)

        # Second request should return existing
        result2 = worker.request_generation(mock_event)

        assert result2["new"] is False
        assert result2["message"] == "Generation already in progress"

    def test_get_status_reports_state(self, persistent_store: PersistentStore) -> None:
        """Test status includes all fields."""
        worker = GenerationWorker(persistent_store=persistent_store)

        status = worker.get_status()

        assert "controlled_e2e" in status
        assert "max_active" in status
        assert "active_jobs" in status
        assert "available_slots" in status
        assert "can_accept_new" in status

    def test_progress_callback_called(self, persistent_store: PersistentStore) -> None:
        """Test progress callback is invoked."""
        mock_callback = Mock()
        worker = GenerationWorker(
            persistent_store=persistent_store,
            progress_callback=mock_callback,
        )

        # Simulate emitting progress
        mock_job = Mock()
        mock_job.job_id = "job-test"
        worker._emit_progress(mock_job, GenerationState.REQUESTING, "Test message")

        mock_callback.assert_called_once()
        call_arg = mock_callback.call_args[0][0]
        assert call_arg.job_id == "job-test"
        assert call_arg.state == GenerationState.REQUESTING


# ============== V5 Callbacks Wiring Tests ==============

class TestV5CallbackHandlerRunStory:
    """Test V5CallbackHandler RUN STORY wiring."""

    def test_handle_run_story_no_event_store(self) -> None:
        """Test RUN STORY fails without event store."""
        handler = V5CallbackHandler(event_store=None)

        result = handler.handle_run_story("evt-001")

        assert result["ok"] is False
        assert result["reason"] == "no_event_store"

    def test_handle_run_story_unknown_event(self) -> None:
        """Test RUN STORY fails for unknown event."""
        mock_store = Mock()
        mock_store.get.return_value = None

        handler = V5CallbackHandler(event_store=mock_store)
        result = handler.handle_run_story("evt-unknown")

        assert result["ok"] is False
        assert result["reason"] == "unknown_event"

    def test_handle_run_story_controlled_e2e(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test RUN STORY in controlled E2E mode."""
        monkeypatch.setenv("NEWSAGENT_V5_CONTROLLED_E2E", "true")

        mock_event = Mock()
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        handler = V5CallbackHandler(
            event_store=mock_store,
            environ=os.environ,
        )
        result = handler.handle_run_story("evt-001")

        assert result["ok"] is True
        assert result["controlled_e2e"] is True
        assert result["started"] is False
        assert "E2E mode" in result["message"]

    def test_handle_run_story_providers_not_ready(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test RUN STORY when providers not configured."""
        monkeypatch.setenv("NEWSAGENT_V5_CONTROLLED_E2E", "false")

        mock_event = Mock()
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        # Mock preflight - no providers ready
        mock_preflight = Mock()
        mock_preflight.get_readiness_summary.return_value = {
            "can_write": False,
            "overall": False,
        }

        handler = V5CallbackHandler(
            event_store=mock_store,
            preflight=mock_preflight,
            environ=os.environ,
        )
        result = handler.handle_run_story("evt-001")

        assert result["ok"] is True
        assert result["started"] is False
        assert result["providers_ready"] is False

    def test_handle_run_story_with_generation_worker(self) -> None:
        """Test RUN STORY triggers generation when ready."""
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        mock_preflight = Mock()
        mock_preflight.get_readiness_summary.return_value = {
            "can_write": True,
            "overall": True,
        }

        mock_worker = Mock()
        mock_worker.request_generation.return_value = {
            "ok": True,
            "new": True,
            "job_id": "job-001",
            "state": GenerationState.REQUESTING,
            "message": "Generation started",
        }

        handler = V5CallbackHandler(
            event_store=mock_store,
            preflight=mock_preflight,
            generation_worker=mock_worker,
        )
        result = handler.handle_run_story("evt-001")

        assert result["ok"] is True
        assert result["started"] is True
        assert result["job_id"] == "job-001"
        mock_worker.request_generation.assert_called_once()

    def test_handle_run_story_progress_message_format(self) -> None:
        """Test RUN STORY messages are user-friendly."""
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Bitcoin ETF Approved"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        mock_preflight = Mock()
        mock_preflight.get_readiness_summary.return_value = {
            "can_write": True,
            "overall": True,
        }

        # Test new generation started
        mock_worker = Mock()
        mock_worker.request_generation.return_value = {
            "ok": True,
            "new": True,
            "state": "REQUESTING",
        }

        handler = V5CallbackHandler(
            event_store=mock_store,
            preflight=mock_preflight,
            generation_worker=mock_worker,
        )
        result = handler.handle_run_story("evt-001")

        # Should have rocket emoji indicating start
        assert "ðŸš€" in result["message"] or result["message"].startswith("âœ…")

    def test_run_story_is_idempotent(self) -> None:
        """Test RUN STORY is idempotent."""
        mock_event = Mock(spec=NewsEvent)
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        mock_preflight = Mock()
        mock_preflight.get_readiness_summary.return_value = {"can_write": True, "overall": True}

        mock_worker = Mock()
        mock_worker.request_generation.return_value = {
            "ok": True,
            "new": False,  # Not new - existing
            "state": GenerationState.WRITING,
            "message": "Generation already in progress",
        }

        handler = V5CallbackHandler(
            event_store=mock_store,
            telegram_store=V5TelegramStore(),
            preflight=mock_preflight,
            generation_worker=mock_worker,
        )

        # First call marks selected
        result = handler.handle_run_story("evt-001")

        assert result["ok"] is True
        # Second call should also succeed (idempotent)
        result2 = handler.handle_run_story("evt-001")
        assert result2["ok"] is True


class TestV5CallbackHandlerOtherActions:
    """Test other callback actions remain unchanged."""

    def test_handle_follow_persists_state(self) -> None:
        """Test FOLLOW persists state."""
        mock_event = Mock()
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        telegram_store = V5TelegramStore()
        handler = V5CallbackHandler(
            event_store=mock_store,
            telegram_store=telegram_store,
        )

        result = handler.handle_follow("evt-001")

        assert result["ok"] is True
        assert telegram_store.is_followed("evt-001")
        assert "ðŸ“Œ" in result["message"]

    def test_handle_ignore_persists_state(self) -> None:
        """Test IGNORE persists state."""
        mock_event = Mock()
        mock_event.event_id = "evt-001"
        mock_event.canonical_title = "Test Headline"

        mock_store = Mock()
        mock_store.get.return_value = mock_event

        telegram_store = V5TelegramStore()
        handler = V5CallbackHandler(
            event_store=mock_store,
            telegram_store=telegram_store,
        )

        result = handler.handle_ignore("evt-001")

        assert result["ok"] is True
        assert telegram_store.is_ignored("evt-001")
        assert "ðŸš«" in result["message"]

    def test_handle_see_next_pagination(self) -> None:
        """Test SEE NEXT 5 returns next batch."""
        events = [
            NewsEvent(event_id=f"evt-{i}", canonical_title=f"Event {i}")
            for i in range(10)
        ]

        telegram_store = V5TelegramStore()
        telegram_store.save_batch(events)

        handler = V5CallbackHandler(telegram_store=telegram_store)
        result = handler.handle_see_next(offset=5)

        assert result["ok"] is True
        assert len(result["events"]) == 5
        assert result["offset"] == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


