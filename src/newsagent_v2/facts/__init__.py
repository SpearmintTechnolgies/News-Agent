"""V6 fact bank: deterministic, source-attributed facts built from a research dossier."""

from newsagent_v2.facts.bank import Fact, FactBank, Quote, build_fact_bank
from newsagent_v2.facts.gate import GatePolicy, GateResult, assess_evidence

__all__ = [
    "Fact",
    "FactBank",
    "GatePolicy",
    "GateResult",
    "Quote",
    "assess_evidence",
    "build_fact_bank",
]
