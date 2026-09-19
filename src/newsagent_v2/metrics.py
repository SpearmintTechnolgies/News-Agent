from __future__ import annotations
from dataclasses import dataclass, asdict
import time

@dataclass
class RunMetrics:
    collected: int = 0
    deduped: int = 0
    selected: int = 0
    ai_calls: int = 0
    paid_ai_calls: int = 0
    estimated_cost_inr: float = 0.0
    elapsed_seconds: float = 0.0

    def to_dict(self):
        return asdict(self)

class Timer:
    def __enter__(self):
        self.start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed = time.perf_counter() - self.start
