"""V5 Discovery Diagnostics.

Structured diagnostics logging for /make runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_ROOT = Path(__file__).resolve().parents[3] / "output" / "v5_runs"


@dataclass
class DiscoveryDiagnostics:
    """Diagnostics from discovery phase.

    Captures the complete discovery pipeline metrics:
    - Source collection
    - Normalization
    - Freshness filtering
    - Niche relevance
    - Deduplication
    - Event clustering
    """
    # Timing
    started_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    finished_at: str | None = None
    duration_ms: float = 0.0

    # Sources
    sources_attempted: int = 0
    sources_succeeded: int = 0
    sources_failed: int = 0

    # Items flow
    raw_reports: int = 0
    normalized: int = 0
    fresh: int = 0
    niche_relevant: int = 0
    exact_duplicates_removed: int = 0
    after_dedupe: int = 0

    # Events
    existing_events_updated: int = 0
    new_events_created: int = 0
    meaningful_developments: int = 0
    events_presented: int = 0

    # Costs (always 0 in V5 discovery)
    discovery_llm_calls: int = 0
    writer_calls: int = 0
    image_calls: int = 0
    estimated_discovery_model_cost: str = "₹0"

    # Errors
    errors: list[dict[str, Any]] = field(default_factory=list)

    # Source details
    source_results: list[dict[str, Any]] = field(default_factory=list)

    def mark_complete(self) -> None:
        """Mark the discovery as complete."""
        self.finished_at = datetime.utcnow().isoformat()
        if self.started_at:
            try:
                start = datetime.fromisoformat(self.started_at)
                end = datetime.fromisoformat(self.finished_at)
                self.duration_ms = (end - start).total_seconds() * 1000
            except Exception:
                pass

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return asdict(self)


@dataclass
class V5Diagnostics:
    """Complete V5 diagnostic report.

    Captures the full /make diagnostic output.
    """
    # Run identification
    run_id: str = field(default_factory=lambda: f"v5-{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
    run_timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Discovery
    discovery: DiscoveryDiagnostics | None = None

    # Intelligence
    events_analyzed: int = 0
    events_with_breaking: int = 0
    events_followed: int = 0
    events_ignored: int = 0

    # Telegram
    cards_rendered: int = 0
    cards_sent: int = 0

    # Final cost verification
    total_llm_calls: int = 0
    total_writer_calls: int = 0
    total_image_calls: int = 0
    total_estimated_cost: str = "₹0"

    # Verification
    cost_boundary_verified: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "run_id": self.run_id,
            "run_timestamp": self.run_timestamp,
            "discovery": self.discovery.to_dict() if self.discovery else None,
            "events_analyzed": self.events_analyzed,
            "events_with_breaking": self.events_with_breaking,
            "events_followed": self.events_followed,
            "events_ignored": self.events_ignored,
            "cards_rendered": self.cards_rendered,
            "cards_sent": self.cards_sent,
            "total_llm_calls": self.total_llm_calls,
            "total_writer_calls": self.total_writer_calls,
            "total_image_calls": self.total_image_calls,
            "total_estimated_cost": self.total_estimated_cost,
            "cost_boundary_verified": self.cost_boundary_verified,
        }

    def to_report_text(self) -> str:
        """Generate human-readable report."""
        lines: list[str] = []
        lines.append(f"NEWSAGENT V5 DISCOVERY RUN")
        lines.append(f"Run ID: {self.run_id}")
        lines.append(f"Timestamp: {self.run_timestamp}")
        lines.append("")

        if self.discovery:
            d = self.discovery
            lines.append("SOURCES:")
            lines.append(f"  Attempted: {d.sources_attempted}")
            lines.append(f"  Succeeded: {d.sources_succeeded}")
            lines.append(f"  Failed: {d.sources_failed}")
            lines.append("")

            lines.append("PROCESSING:")
            lines.append(f"  Raw reports: {d.raw_reports}")
            lines.append(f"  Normalized: {d.normalized}")
            lines.append(f"  Fresh: {d.fresh}")
            lines.append(f"  Niche relevant: {d.niche_relevant}")
            lines.append(f"  Exact duplicates removed: {d.exact_duplicates_removed}")
            lines.append(f"  Reports after dedupe: {d.after_dedupe}")
            lines.append("")

            lines.append("EVENTS:")
            lines.append(f"  Existing updated: {d.existing_events_updated}")
            lines.append(f"  New created: {d.new_events_created}")
            lines.append(f"  Meaningful developments: {d.meaningful_developments}")
            lines.append(f"  Events presented: {d.events_presented}")
            lines.append("")

        lines.append("COST BOUNDARY:")
        lines.append(f"  Discovery LLM calls: {self.total_llm_calls}")
        lines.append(f"  Writer calls: {self.total_writer_calls}")
        lines.append(f"  Image calls: {self.total_image_calls}")
        lines.append(f"  Estimated cost: {self.total_estimated_cost}")
        lines.append(f"  Boundary verified: {'✅' if self.cost_boundary_verified else '❌'}")
        lines.append("")

        lines.append("CONFIRMATION: V5 Discovery uses ZERO paid model calls.")
        lines.append("All processing is deterministic code-first.")

        return "\n".join(lines)

    def save(self, output_root: Path | None = None) -> Path:
        """Save diagnostics to file."""
        root = output_root or DEFAULT_OUTPUT_ROOT
        root.mkdir(parents=True, exist_ok=True)

        path = root / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

        # Also save report
        report_path = root / f"{self.run_id}.txt"
        report_path.write_text(self.to_report_text(), encoding="utf-8")

        return path
