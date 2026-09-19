"""Cost Ledger for V5 Discovery.

Tracks and verifies zero-cost discovery boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class V5CostReport:
    """Cost report for a V5 discovery run.

    NORMAL /make expectation:
    - Discovery LLM calls: 0
    - Writer calls: 0
    - Image calls: 0
    - Estimated cost: ₹0
    """
    discovery_llm_calls: int = 0
    research_llm_calls: int = 0
    writer_calls: int = 0
    writer_tokens: int = 0
    image_reservations: int = 0
    image_network_calls: int = 0
    image_successes: int = 0
    discovery_model_cost_inr: float = 0.0
    writer_model_cost_inr: float = 0.0
    image_cost_inr: float = 0.0

    @property
    def total_cost_inr(self) -> float:
        """Total cost in INR."""
        return (
            self.discovery_model_cost_inr +
            self.writer_model_cost_inr +
            self.image_cost_inr
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "discovery_llm_calls": self.discovery_llm_calls,
            "research_llm_calls": self.research_llm_calls,
            "writer_calls": self.writer_calls,
            "writer_tokens": self.writer_tokens,
            "image_reservations": self.image_reservations,
            "image_network_calls": self.image_network_calls,
            "image_successes": self.image_successes,
            "discovery_model_cost_inr": self.discovery_model_cost_inr,
            "writer_model_cost_inr": self.writer_model_cost_inr,
            "image_cost_inr": self.image_cost_inr,
            "total_cost_inr": self.total_cost_inr,
        }

    def to_report(self) -> str:
        """Generate human-readable report."""
        return (
            f"V5 Discovery Pipeline Cost Report\n"
            f"=================================\n"
            f"Discovery LLM calls: {self.discovery_llm_calls}\n"
            f"Research LLM calls: {self.research_llm_calls}\n"
            f"Writer calls: {self.writer_calls}\n"
            f"Writer tokens: {self.writer_tokens}\n"
            f"Image reservations: {self.image_reservations}\n"
            f"Image network calls: {self.image_network_calls}\n"
            f"Image successes: {self.image_successes}\n"
            f"\n"
            f"Discovery model cost: ₹{self.discovery_model_cost_inr:.2f}\n"
            f"Writer model cost: ₹{self.writer_model_cost_inr:.2f}\n"
            f"Image cost: ₹{self.image_cost_inr:.2f}\n"
            f"\n"
            f"TOTAL: ₹{self.total_cost_inr:.2f}\n"
            f"\n"
            f"Expected: ₹0 (deterministic discovery)\n"
            f"Verified: {'✅ PASS' if self.total_cost_inr == 0 else '❌ FAIL'}"
        )


class CostLedger:
    """Ledger for tracking costs in V5.

    All methods are NO-OP for discovery phase.
    They exist to maintain interface compatibility.
    """

    def __init__(self):
        self.report = V5CostReport()

    def record_discovery_llm_call(self, model: str, tokens: int) -> None:
        """Record a discovery LLM call.

        WARNING: This should be 0 in V5 normal operation.
        """
        self.report.discovery_llm_calls += 1
        # Estimate cost (for tracking only - this is exceptional)
        # Approximate: ₹0.001 per token (rough estimate)
        self.report.discovery_model_cost_inr += tokens * 0.001

    def record_writer_call(self, tokens: int = 0) -> None:
        """Record a writer call.

        WARNING: This should be 0 in V5 discovery.
        Only called when RUN STORY is invoked.
        """
        self.report.writer_calls += 1
        self.report.writer_tokens += tokens

    def record_image_reservation(self) -> None:
        """Record image reservation."""
        self.report.image_reservations += 1

    def record_image_network_call(self) -> None:
        """Record image network call."""
        self.report.image_network_calls += 1

    def record_image_success(self) -> None:
        """Record successful image generation."""
        self.report.image_successes += 1

    def get_report(self) -> V5CostReport:
        """Get current cost report."""
        return self.report

    def verify_zero_discovery_cost(self) -> tuple[bool, str]:
        """Verify that discovery cost is zero.

        Returns:
            Tuple of (verified, message)
        """
        if self.report.discovery_llm_calls == 0:
            return True, "✅ Discovery cost verified: ₹0"

        return (
            False,
            f"❌ Discovery cost boundary violated: "
            f"{self.report.discovery_llm_calls} LLM calls, "
            f"₹{self.report.discovery_model_cost_inr:.2f}"
        )

    def get_stats(self) -> dict[str, Any]:
        """Get ledger statistics."""
        return {
            "discovery_calls": self.report.discovery_llm_calls,
            "writer_calls": self.report.writer_calls,
            "image_calls": self.report.image_network_calls,
            "total_cost": self.report.total_cost_inr,
        }
