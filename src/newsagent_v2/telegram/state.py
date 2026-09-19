"""V5 Telegram bot state management.

Handles:
- Processed update ID tracking
- Make run ID generation
- Sent card tracking
- Idempotency guards
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Set

# Log locations
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
PROCESSED_LOG = LOG_DIR / "v5_processed_updates.log"


class V5BotState:
    """Bot state including idempotency tracking."""
    
    def __init__(self) -> None:
        self.processed_update_ids: set[int] = set()
        self.current_make_run_id: str | None = None
        self.current_make_update_id: int | None = None
        self.sent_cards: set[str] = set()  # make_run_id:event_id
        self.offset: int | None = None
        
    def is_update_processed(self, update_id: int) -> bool:
        """Check if update was already processed."""
        if update_id in self.processed_update_ids:
            return True
        # Also check persisted
        if PROCESSED_LOG.exists():
            try:
                for line in PROCESSED_LOG.read_text().splitlines():
                    if line.strip() == str(update_id):
                        self.processed_update_ids.add(update_id)
                        return True
            except OSError:
                pass
        return False
    
    def mark_update_processed(self, update_id: int) -> None:
        """Mark update as processed with persistence."""
        self.processed_update_ids.add(update_id)
        # Persist (bounded - keep last 1000)
        try:
            existing = []
            if PROCESSED_LOG.exists():
                existing = PROCESSED_LOG.read_text().splitlines()[-999:]
            existing.append(str(update_id))
            PROCESSED_LOG.write_text("\n".join(existing))
        except OSError:
            pass
    
    def can_execute_make(self, update_id: int) -> tuple[bool, str]:
        """Check if /make can execute for this update.
        
        Returns (can_run, reason)
        """
        if self.is_update_processed(update_id):
            return False, "update_already_processed"
        if self.current_make_run_id is not None and self.current_make_update_id == update_id:
            return False, "same_make_run_in_progress"
        return True, "ok"
    
    def start_make_run(self, update_id: int) -> str:
        """Start a new make run.
        
        Returns make_run_id.
        """
        self.current_make_update_id = update_id
        self.current_make_run_id = f"make-{uuid.uuid4().hex[:8]}"
        self.sent_cards.clear()
        return self.current_make_run_id
    
    def can_send_card(self, event_id: str) -> bool:
        """Check if card already sent for current run.
        
        Returns True if can send (not sent yet), False if already sent.
        """
        if self.current_make_run_id is None:
            return True  # No run tracking, allow
        key = f"{self.current_make_run_id}:{event_id}"
        if key in self.sent_cards:
            return False
        self.sent_cards.add(key)
        return True
