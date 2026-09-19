from __future__ import annotations
from abc import ABC, abstractmethod

class EditorialProvider(ABC):
    @abstractmethod
    def generate(self, evidence_pack: dict) -> dict:
        """Return structured editorial output."""
        raise NotImplementedError
