"""Pre-writer evidence gate: can this event honestly support a long-form article?"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from newsagent_v2.facts.bank import FactBank
from newsagent_v2.research.dossier import ResearchDossier


@dataclass
class GatePolicy:
    min_full_sources: int = 4
    min_publishers: int = 4
    min_research_words: int = 2500
    min_core_facts: int = 30
    min_numeric_core_facts: int = 4
    min_corroborated_core_facts: int = 3
    min_core_fact_words: int = 700


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> str:
        if self.passed:
            return "Evidence sufficient for a long-form article."
        return "Skipped: " + "; ".join(self.reasons)

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "reasons": list(self.reasons), "metrics": dict(self.metrics)}


def assess_evidence(
    dossier: ResearchDossier,
    bank: FactBank,
    policy: GatePolicy | None = None,
) -> GateResult:
    rules = policy or GatePolicy()
    stats = bank.stats()
    metrics = {
        "full_sources": len(dossier.full_sources),
        "publishers": len(dossier.publishers),
        "primary_sources": dossier.primary_count,
        "research_words": dossier.total_words,
        **{k: v for k, v in stats.items() if k != "dropped"},
    }
    checks = [
        (metrics["full_sources"] >= rules.min_full_sources,
         f"only {metrics['full_sources']} full sources (need {rules.min_full_sources})"),
        (metrics["publishers"] >= rules.min_publishers,
         f"only {metrics['publishers']} independent publishers (need {rules.min_publishers})"),
        (metrics["research_words"] >= rules.min_research_words,
         f"only {metrics['research_words']} words of research (need {rules.min_research_words})"),
        (metrics["core_facts"] >= rules.min_core_facts,
         f"only {metrics['core_facts']} distinct on-topic facts (need {rules.min_core_facts})"),
        (metrics["fact_words"] >= rules.min_core_fact_words,
         f"only {metrics['fact_words']} words of on-topic facts (need {rules.min_core_fact_words})"),
        (metrics["numeric_core_facts"] >= rules.min_numeric_core_facts,
         f"only {metrics['numeric_core_facts']} facts with figures (need {rules.min_numeric_core_facts})"),
        (metrics["corroborated_core_facts"] >= rules.min_corroborated_core_facts,
         f"only {metrics['corroborated_core_facts']} facts confirmed by 2+ outlets "
         f"(need {rules.min_corroborated_core_facts})"),
    ]
    reasons = [message for ok, message in checks if not ok]
    return GateResult(passed=not reasons, reasons=reasons, metrics=metrics)
