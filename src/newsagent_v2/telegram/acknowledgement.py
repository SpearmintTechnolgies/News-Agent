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
        chat_ids: tuple[str, ...] | None = None,
    ) -> None:
        self.client = client
        self.chat_id = chat_id
        self.chat_ids = tuple(chat_ids) if chat_ids else (chat_id,)
        self.update_id = update_id
        self.make_run_id = make_run_id
        self.message_id: int | None = None
        self._message_ids: dict[str, int] = {}
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
        
        primary = {"ok": False, "error": "not sent"}
        for chat_id in self.chat_ids:
            result = self.client.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
            if result.get("ok") and result.get("message_id"):
                self._message_ids[chat_id] = result["message_id"]
            if chat_id == self.chat_id:
                primary = result
        if self._message_ids:
            self.message_id = self._message_ids.get(self.chat_id) or next(iter(self._message_ids.values()))
            self._sent = True
        return primary
    
    def update_progress(self, stage: str, detail: str = "") -> dict[str, any]:
        """Update acknowledgement with current progress."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = f"🔎 NewsAgent is scanning the news...\n\n{stage}"
        if detail:
            text += f"\n{detail}"
        
        return self._edit_all(text)
    
    def mark_complete(self, event_count: int, note: str = "") -> dict[str, any]:
        """Mark discovery as complete with results."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = (
            f"✅ News scan complete — {event_count} events found\n\n"
            f"{note or 'Showing your Top 5 below.'}"
        )
        
        return self._edit_all(text)
    
    def mark_failed(self, error: str) -> dict[str, any]:
        """Mark discovery as failed."""
        if not self._sent or self.message_id is None:
            return {"ok": False, "reason": "not_sent"}
        
        text = (
            "❌ News scan failed.\n\n"
            "Check the bot logs for details."
        )
        
        return self._edit_all(text)

    def _edit_all(self, text: str) -> dict[str, any]:
        if not self._message_ids and self.message_id is not None:
            self._message_ids = {self.chat_id: self.message_id}
        last: dict[str, any] = {"ok": False, "reason": "not_sent"}
        for chat_id, message_id in self._message_ids.items():
            result = self.client.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML",
            )
            if chat_id == self.chat_id:
                last = result
        return last
