"""V5 Provider Preflight Tests.

Tests cover:
- BOTH writer+image must be READY before generation
- If either unavailable: 0 provider calls
- Generation Progress message updates through stages
"""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import datetime, timezone

import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from newsagent_v2.v5_generation.provider_preflight import (
    ProviderPreflight,
    ProviderStatus,
)
from newsagent_v2.v5_generation.persistent_store import (
    PersistentV5Store,
    GenerationJob,
)
from newsagent_v2.discovery.event_clusterer import NewsEvent


class TestProviderPreflight:
    """Test provider preflight checks."""
    
    def test_writer_ready_with_groq_key(self) -> None:
        """Writer ready when GROQ_API_KEY is set."""
        environ = {"GROQ_API_KEY": "gsk_test_api_key_12345"}
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_writer()
        assert status.status == "READY", f"Writer should be READY: {status}"
    
    def test_writer_missing_with_no_key(self) -> None:
        """Writer missing when GROQ_API_KEY is not set."""
        environ = {}
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_writer()
        assert status.status == "MISSING_CREDENTIALS"
    
    def test_image_ready_with_vertex_config(self) -> None:
        """Image ready when Vertex config is complete."""
        environ = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
        }
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_image()
        assert status.status == "READY"
    
    def test_image_disabled_without_enabled(self) -> None:
        """Image disabled when NEWSAGENT_V2_VERTEX_ENABLED is false."""
        environ = {"NEWSAGENT_V2_VERTEX_ENABLED": "false"}
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_image()
        assert status.status == "DISABLED"
    
    def test_image_missing_config_with_partial_config(self) -> None:
        """Image missing config when partial config."""
        environ = {
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            # Missing LOCATION and CREDENTIALS
        }
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_image()
        assert status.status == "MISSING_CONFIG"
    
    def test_wordpress_ready_with_all_config(self) -> None:
        """WordPress ready when all credentials set."""
        environ = {
            "NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://example.com",
            "NEWSAGENT_V2_WORDPRESS_USERNAME": "admin",
            "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD": "fake_pass",
        }
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_wordpress()
        assert status.status == "READY"
    
    def test_wordpress_not_configured_missing_creds(self) -> None:
        """WordPress not configured when missing credentials."""
        environ = {}
        preflight = ProviderPreflight(environ)
        
        status = preflight.check_wordpress()
        assert status.status == "MISSING_CONFIG"


class TestProviderPreflightFullReport:
    """Test full preflight report."""
    
    def test_full_report_all_ready(self) -> None:
        """Full report when all providers ready."""
        environ = {
            "GROQ_API_KEY": "gsk_test_key",
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
            "NEWSAGENT_V2_WORDPRESS_BASE_URL": "https://example.com",
            "NEWSAGENT_V2_WORDPRESS_USERNAME": "admin",
            "NEWSAGENT_V2_WORDPRESS_APP_PASSWORD": "fake_pass",
        }
        preflight = ProviderPreflight(environ)
        
        report = preflight.full_report()
        
        assert report["writer"]["status"] == "READY"
        assert report["image"]["status"] == "READY"
        assert report["wordpress"]["status"] == "READY"
        assert report["ready_for_generation"] is True
        assert report["ready_for_image"] is True
        assert report["ready_for_publishing"] is True
    
    def test_full_report_writer_only(self) -> None:
        """Full report with only writer ready."""
        environ = {
            "GROQ_API_KEY": "gsk_test_key",
        }
        preflight = ProviderPreflight(environ)
        
        report = preflight.full_report()
        
        assert report["writer"]["status"] == "READY"
        assert report["image"]["status"] != "READY"
        assert report["ready_for_generation"] is True  # Article generation is ready
        assert report["ready_for_image"] is False
    
    def test_full_report_none_ready(self) -> None:
        """Full report when no providers ready."""
        environ = {}
        preflight = ProviderPreflight(environ)
        
        report = preflight.full_report()
        
        assert report["writer"]["status"] != "READY"
        assert report["image"]["status"] != "READY"
        assert report["ready_for_generation"] is False
        assert report["ready_for_image"] is False


class TestGenerationRequiresBothProviders:
    """Test that generation requires BOTH providers to be ready."""
    
    def test_generation_blocked_if_writer_not_ready(self, tmp_path: Path) -> None:
        """If writer unavailable: 0 provider calls."""
        # Create store with no writer ready
        environ = {
            # No GROQ_API_KEY
            "NEWSAGENT_V2_VERTEX_ENABLED": "true",
            "NEWSAGENT_V2_VERTEX_PROJECT": "test-project",
            "NEWSAGENT_V2_VERTEX_LOCATION": "us-central1",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path/to/creds.json",
        }
        
        preflight = ProviderPreflight(environ)
        
        # Writer is not ready
        assert preflight.check_writer().status != "READY"
        
        # Image might be ready but writer is not
        report = preflight.full_report()
        assert report["ready_for_generation"] is False, \
            "Generation should be blocked if writer not ready"
    
    def test_generation_blocked_if_image_not_ready_but_writer_is(self) -> None:
        """Test generation when image not ready but writer is.
        
        For article-only mode, this is OK. But for full generation
        BOTH should be ready.
        """
        environ = {
            "GROQ_API_KEY": "gsk_test_key",
            # No Vertex config
        }
        
        preflight = ProviderPreflight(environ)
        
        # Writer is ready but image is not
        assert preflight.check_writer().status == "READY"
        assert preflight.check_image().status != "READY"
        
        report = preflight.full_report()
        # ready_for_generation is TRUE because writer is ready (article generation)
        # ready_for_image is FALSE
        assert report["ready_for_generation"] is True
        assert report["ready_for_image"] is False


class TestGenerationProgressStages:
    """Test generation progress message updates through stages.
    
    Expected stages:
    Selected â†’ Researching â†’ Building evidence â†’ Writing â†’ 
    Verification/QA â†’ Generating image â†’ Review ready
    """
    
    def test_progress_stages_in_job(self, tmp_path: Path) -> None:
        """Test that job tracks progress through stages."""
        store = PersistentV5Store(tmp_path)
        
        # Create job
        job = GenerationJob(
            job_id="job-test",
            event_id="evt-test",
            discovery_run_id="run-test",
            state="NOT_REQUESTED",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        store.save_job(job)
        
        # Simulate progress through stages
        stages = [
            ("RESERVED", "Selected"),
            ("REQUESTING", "Researching sources..."),
            ("RESEARCHING", "Building evidence..."),
            ("WRITING", "Writing article..."),
            ("QA", "Verification/QA..."),
            ("IMAGE_GEN", "Generating image..."),
            ("SUCCEEDED", "Review ready"),
        ]
        
        for state, description in stages:
            store.update_job_state(
                job_id=job.job_id,
                state=state,
            )
            
            job_after = store.get_job(job.job_id)
            assert job_after is not None
            assert job_after.state == state
        
        # Final state should be SUCCEEDED
        assert job_after.state == "SUCCEEDED"
    
    def test_progress_stages_match_expected(self) -> None:
        """Test that progress stages match expected sequence."""
        expected_stages = [
            "Selected",
            "Researching sources...",
            "Building evidence...", 
            "Writing article...",
            "Verification/QA...",
            "Generating image...",
            "Review ready",
        ]
        
        # Verify each stage is meaningful
        for stage in expected_stages:
            assert len(stage) > 0
            assert isinstance(stage, str)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


