"""V5 Telegram acknowledgement handling.

Manages /make acknowledgement messages that are edited throughout discovery.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from newsagent_v2.telegram.client import TelegramTestClient


class MakeAcknowledgement:
    """Tracks and updates /make acknowledgement message."""
    
    def __init__(
        self,
        client: TelegramTestClient,
        chat_id: str,
        update_id: int,
        make_run_id: str,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.update_id = update_id
        self.make_run_id = make_run_id
        self.message_id: int | None = None
        self._sent = False
    
    def send_initial(self) -> dict[str, any]:
        """Send initial acknowledgement before discovery starts.
        
        Returns Telegram send result.
        """
        if self._sent:
            return {"ok": False, "reason": "already_sent"}
        
        text = (
            "🔎 NewsAgent is scanning the news...\n\n"
            "Collecting sources → filtering → clustering → ranking"
        )
        
        result = self.client.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
        )
        
        if result.get("ok"):
            self.message_id = result.get("message_id")
            self._sent = True
        
        return result
    
    def update_progress(self, stage: str, detail: str = "") -> dict[str, any]:
        """Update acknowledgement with current progress."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = f"🔎 NewsAgent is scanning the news...\n\n{stage}"
        if detail:
            text += f"\n{detail}"
        
        result = self.client.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message_id,
            text=text,
            parse_mode="HTML",
        )
        
        return result
    
    def mark_complete(self, event_count: int) -> dict[str, any]:
        """Mark discovery as complete with results."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = (
            f"✅ News scan complete — {event_count} events found\n\n"
            "Showing your Top 5 below."
        )
        
        result = self.client.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message_id,
            text=text,
            parse_mode="HTML",
        )
        
        return result
    
    def mark_failed(self, error: str) -> dict[str, any]:
        """Mark discovery as failed."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = (
            "❌ News scan failed.\n\n"
            "Check the bot logs for details."
        )
        
        result = self.client.edit_message_text(
            chat_id=self.chat_id,
            message_id=self.message_id,
            text=text,
            parse_mode="HTML",
        )
        
        return result
