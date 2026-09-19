"""Cost Ledger for V5 generation.

Tracks actual costs per provider call:
- provider name
- model name
- tokens (input/output/total)
- estimated cost (UNKNOWN if cannot be determined)

Exports final cost report.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


# Approximate pricing in INR per 1K tokens
# These are estimates - update with actual pricing
PRICING_ESTIMATES: dict[str, dict[str, float]] = {
    "groq": {
        "llama-3.3-70b-versatile": 0.006,  # per 1K tokens (combined)
        "llama-3.1-8b-instant": 0.0006,
        "mixtral-8x7b-32768": 0.003,
    },
    "vertex": {
        "gemini-3.1-flash-image": 0.0015,
    },
}


class CostStatus(Enum):
    """Status of cost tracking for a call."""
    KNOWN = "known"
    UNKNOWN = "unknown"
    PENDING = "pending"


@dataclass
class CostEntry:
    """Single cost entry for an API call."""
    entry_id: str
    timestamp: str
    provider: str
    model: str
    operation: str  # e.g., "write", "image_gen", "research"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    cost_inr: float | None = None  # None = UNKNOWN
    cost_status: CostStatus = CostStatus.PENDING
    metadata: dict[str, Any] = field(default_factory=dict)

    def finalize_cost(self, cost: float | None = None) -> None:
        """Finalize cost. If None, mark as UNKNOWN."""
        if cost is not None:
            self.cost_inr = cost
            self.cost_status = CostStatus.KNOWN
        else:
            self.cost_inr = None
            self.cost_status = CostStatus.UNKNOWN

    def estimate_cost(self) -> float:
        """Estimate cost based on model pricing."""
        provider = self.provider.lower()
        model_pricing = PRICING_ESTIMATES.get(provider, {})
        rate_per_1k = model_pricing.get(self.model, 0.0)
        return (self.total_tokens / 1000) * rate_per_1k if rate_per_1k else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "provider": self.provider,
            "model": self.model,
            "operation": self.operation,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_inr": self.cost_inr,
            "cost_status": self.cost_status.value,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CostEntry:
        return cls(
            entry_id=data["entry_id"],
            timestamp=data["timestamp"],
            provider=data["provider"],
            model=data["model"],
            operation=data["operation"],
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            latency_ms=data.get("latency_ms"),
            cost_inr=data.get("cost_inr"),
            cost_status=CostStatus(data.get("cost_status", "unknown")),
            metadata=data.get("metadata", {}),
        )


@dataclass
class CostReport:
    """Aggregated cost report."""
    entries: list[CostEntry] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def total_entries(self) -> int:
        return len(self.entries)

    @property
    def total_known_cost_inr(self) -> float:
        """Sum of all KNOWN costs."""
        return sum(
            e.cost_inr for e in self.entries
            if e.cost_inr is not None
        )

    @property
    def total_estimated_cost_inr(self) -> float:
        """Sum of estimated costs for UNKNOWN entries."""
        return sum(
            e.estimate_cost() for e in self.entries
            if e.cost_inr is None
        )

    @property
    def total_tokens(self) -> int:
        return sum(e.total_tokens for e in self.entries)

    @property
    def unknown_count(self) -> int:
        return sum(1 for e in self.entries if e.cost_inr is None)

    @property
    def by_provider(self) -> dict[str, dict[str, Any]]:
        """Aggregate by provider."""
        result: dict[str, dict[str, Any]] = {}
        for entry in self.entries:
            provider = entry.provider
            if provider not in result:
                result[provider] = {
                    "calls": 0,
                    "total_tokens": 0,
                    "known_cost_inr": 0.0,
                    "unknown_costs": 0,
                }
            result[provider]["calls"] += 1
            result[provider]["total_tokens"] += entry.total_tokens
            if entry.cost_inr is not None:
                result[provider]["known_cost_inr"] += entry.cost_inr
            else:
                result[provider]["unknown_costs"] += 1
        return result

    def to_telegram_summary(self) -> str:
        """Format as Telegram-friendly summary."""
        lines = [
            "💰 <b>Cost Report</b>",
            "",
            f"Total calls: {self.total_entries}",
            f"Total tokens: {self.total_tokens:,}",
            f"Known cost: ₹{self.total_known_cost_inr:.2f}",
        ]

        if self.unknown_count > 0:
            lines.append(f"Unknown costs: {self.unknown_count}")
            lines.append(f"Est. unknown: ₹{self.total_estimated_cost_inr:.2f}")
            lines.append(f"Total est: ₹{self.total_known_cost_inr + self.total_estimated_cost_inr:.2f}")

        lines.append("")
        lines.append("<b>By Provider:</b>")
        for provider, stats in self.by_provider.items():
            cost_str = f"₹{stats['known_cost_inr']:.2f}"
            if stats['unknown_costs']:
                cost_str += f" (+{stats['unknown_costs']} unknown)"
            lines.append(f"  {provider}: {stats['calls']} calls, {cost_str}")

        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "total_entries": self.total_entries,
            "total_tokens": self.total_tokens,
            "known_cost_inr": self.total_known_cost_inr,
            "estimated_cost_inr": self.total_estimated_cost_inr,
            "unknown_count": self.unknown_count,
            "by_provider": self.by_provider,
            "entries": [e.to_dict() for e in self.entries],
        }


class CostLedger:
    """Thread-safe cost ledger for tracking generation costs."""

    def __init__(self, persist_path: Path | str | None = None) -> None:
        self._entries: dict[str, CostEntry] = {}
        self._lock = threading.RLock()
        self._persist_path = Path(persist_path) if persist_path else None

        if self._persist_path:
            self._load_persistent()

    def _load_persistent(self) -> None:
        """Load existing entries from disk."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for entry_data in data.get("entries", []):
                    entry = CostEntry.from_dict(entry_data)
                    self._entries[entry.entry_id] = entry
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

    def _save_persistent(self) -> None:
        """Save entries to disk."""
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as f:
            json.dump(
                {"entries": [e.to_dict() for e in self._entries.values()]},
                f,
                indent=2,
                default=str,
            )

    def _generate_id(self, prefix: str = "cost") -> str:
        """Generate unique entry ID."""
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{prefix}_{ts}_{len(self._entries)}"

    def start_entry(
        self,
        provider: str,
        model: str,
        operation: str,
        metadata: dict[str, Any] | None = None,
    ) -> CostEntry:
        """Start tracking a new cost entry."""
        entry = CostEntry(
            entry_id=self._generate_id(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            operation=operation,
            cost_status=CostStatus.PENDING,
            metadata=metadata or {},
        )
        with self._lock:
            self._entries[entry.entry_id] = entry
            self._save_persistent()
        return entry

    def finalize_entry(
        self,
        entry_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int | None = None,
        cost_inr: float | None = None,
    ) -> CostEntry | None:
        """Finalize an entry with token counts and cost."""
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return None

            entry.prompt_tokens = prompt_tokens
            entry.completion_tokens = completion_tokens
            entry.total_tokens = prompt_tokens + completion_tokens
            entry.latency_ms = latency_ms
            entry.finalize_cost(cost_inr)

            self._save_persistent()
            return entry

    def record_call(
        self,
        provider: str,
        model: str,
        operation: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int | None = None,
        cost_inr: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CostEntry:
        """Record a complete call in one step."""
        entry = CostEntry(
            entry_id=self._generate_id(),
            timestamp=datetime.now(timezone.utc).isoformat(),
            provider=provider,
            model=model,
            operation=operation,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            latency_ms=latency_ms,
            metadata=metadata or {},
        )
        entry.finalize_cost(cost_inr)

        with self._lock:
            self._entries[entry.entry_id] = entry
            self._save_persistent()

        return entry

    def get_entry(self, entry_id: str) -> CostEntry | None:
        """Get a cost entry by ID."""
        with self._lock:
            return self._entries.get(entry_id)

    def get_report(self) -> CostReport:
        """Generate comprehensive cost report."""
        with self._lock:
            return CostReport(
                entries=list(self._entries.values()),
                generated_at=datetime.now(timezone.utc).isoformat(),
            )

    def get_latest_n(self, n: int = 10) -> list[CostEntry]:
        """Get the latest N entries."""
        with self._lock:
            sorted_entries = sorted(
                self._entries.values(),
                key=lambda e: e.timestamp,
                reverse=True,
            )
            return sorted_entries[:n]

    def estimate_total_cost(self) -> tuple[float, float]:
        """Get (known_cost, estimated_unknown_cost)."""
        with self._lock:
            known = sum(
                e.cost_inr for e in self._entries.values()
                if e.cost_inr is not None
            )
            unknown = sum(
                e.estimate_cost() for e in self._entries.values()
                if e.cost_inr is None
            )
            return known, unknown

    def get_stats(self) -> dict[str, Any]:
        """Get quick stats."""
        report = self.get_report()
        return {
            "total_entries": report.total_entries,
            "total_tokens": report.total_tokens,
            "known_cost_inr": report.total_known_cost_inr,
            "unknown_count": report.unknown_count,
            "by_provider": report.by_provider,
        }

    def clear(self) -> int:
        """Clear all entries. Returns count cleared."""
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            if self._persist_path and self._persist_path.exists():
                self._persist_path.unlink()
            return count

    def export_report(self, path: Path | str) -> None:
        """Export full report to JSON file."""
        report = self.get_report()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, indent=2, default=str)


def create_ledger(
    persist_path: Path | str | None = None,
) -> CostLedger:
    """Factory for creating CostLedger with optional persistence."""
    return CostLedger(persist_path=persist_path)


def format_cost_for_callback(known_inr: float, estimated_inr: float) -> str:
    """Format cost as a short string for callbacks."""
    if known_inr > 0:
        return f"₹{known_inr:.2f}"
    if estimated_inr > 0:
        return f"~₹{estimated_inr:.2f}"
    return "unknown"
