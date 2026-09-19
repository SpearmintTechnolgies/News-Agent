"""V5 Telegram listener integration.

Wires V5 discovery-only pipeline into existing Telegram infrastructure.
Uses existing client/config and listener mechanisms.
"""

from __future__ import annotations

from typing import Any, Callable

from pathlib import Path

from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig
from newsagent_v2.telegram.listener import poll_once
from newsagent_v2.telegram.v5_callbacks import V5CallbackHandler, V5TelegramStore
from newsagent_v2.telegram.v5_cards import render_card_from_event, seepage_keyboard
from newsagent_v2.control.make_v5_bridge import create_v5_discovery_pipeline


class V5TelegramIntegration:
    """V5 Telegram bot integration.
    
    Manages:
    - Discovery pipeline
    - Callback state
    - Telegram send/receive
    """
    
    def __init__(
        self,
        config: TelegramConfig,
        client: TelegramTestClient,
        event_store: EventStore | None = None,
        source_registry: SourceRegistry | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.event_store = event_store or EventStore(root=Path("./data/events"))
        self.source_registry = source_registry or SourceRegistry()
        
        # State storage
        self.telegram_store = V5TelegramStore()
        self.callback_handler = V5CallbackHandler(
            event_store=self.event_store,
            telegram_store=self.telegram_store,
        )
        
        # Pipeline reference (set after /make runs)
        self._discovery_pipeline: Any | None = None
    
    def run_discovery(self) -> list[Any]:
        """Run V5 discovery and save results."""
        self._discovery_pipeline = create_v5_discovery_pipeline(
            event_store=self.event_store,
            source_registry=self.source_registry,
            telegram_store=self.telegram_store,
        )
        
        events = self._discovery_pipeline.run_discovery()
        
        # Send Top 5
        self.send_top_events(count=5, offset=0)
        
        return events
    
    def send_top_events(self, count: int = 5, offset: int = 0) -> list[dict[str, Any]]:
        """Send events at offset to Telegram."""
        if self._discovery_pipeline is None:
            return [{"ok": False, "reason": "no_discovery_run"}]
        
        events = self._discovery_pipeline.get_top_events(count=count, offset=offset)
        total = len(self.telegram_store.get_ranked_events() or [])
        
        results = []
        
        for i, event in enumerate(events):
            rank = offset + i + 1
            card = render_card_from_event(event, rank, total)
            
            result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=card["text"],
                parse_mode=card["parse_mode"],
                reply_markup=card["reply_markup"],
            )
            results.append(result)
        
        # Add SEE NEXT 5 if more exist
        if offset + count < total:
            next_result = self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=f"Showing {min(offset + count, total)}/{total} events",
                parse_mode="HTML",
                reply_markup=seepage_keyboard(offset=offset + count),
            )
            results.append(next_result)
        
        return results
    
    def handle_callback(self, data: str, update: dict[str, Any] | None = None) -> dict[str, Any]:
        """Handle V5 callback."""
        result = self.callback_handler.handle(data)
        
        # Send acknowledgment message
        if result.get("ok") and result.get("message"):
            self.client.send_message(
                chat_id=self.config.test_chat_id,
                text=result["message"],
                parse_mode="HTML",
            )
        
        # Handle SEE NEXT action
        if result.get("action") == "see_next":
            offset = result.get("offset", 0)
            self.send_top_events(count=5, offset=offset)
        
        return result
    
    def run_once(self) -> dict[str, Any]:
        """Poll Telegram once and handle updates."""
        response = poll_once(self.client, timeout=30)
        
        payload = response.get("payload") if isinstance(response, dict) else None
        if not isinstance(payload, dict):
            return {"handled": 0}
        
        updates = payload.get("result", [])
        if not isinstance(updates, list):
            return {"handled": 0}
        
        handled = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            
            # Check for callback
            callback = update.get("callback_query")
            if isinstance(callback, dict):
                data = str(callback.get("data") or "")
                self.handle_callback(data, update)
                
                # Answer callback query
                query_id = str(callback.get("id") or "")
                if query_id:
                    self.client.answer_callback_query(callback_query_id=query_id, text="Done")
                handled += 1
                continue
            
            # Check for /make command
            message = update.get("message") or {}
            if isinstance(message, dict):
                text = message.get("text", "")
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                
                # Validate chat
                if chat_id != self.config.test_chat_id:
                    continue
                
                cmd = (text or "").strip().split()[0] if text else ""
                if cmd == "/make":
                    self.run_discovery()
                    handled += 1
        
        return {"handled": handled, "updates": len(updates)}


def start_v5_bot(
    telegram_config: TelegramConfig | None = None,
    telegram_client: TelegramTestClient | None = None,
    event_store: EventStore | None = None,
    source_registry: SourceRegistry | None = None,
    max_iterations: int | None = None,
) -> None:
    """Start V5 Telegram bot.
    
    Polls for /make and callbacks.
    """
    import os
    
    # Load from environment if not provided
    if telegram_config is None:
        from newsagent_v2.telegram.config import load_telegram_config
        telegram_config = load_telegram_config(os.environ)
    
    if telegram_client is None:
        telegram_client = TelegramTestClient(config=telegram_config)
    
    integration = V5TelegramIntegration(
        config=telegram_config,
        client=telegram_client,
        event_store=event_store,
        source_registry=source_registry,
    )
    
    print(f"V5 Telegram bot started")
    print(f"Listening on chat: {telegram_config.test_chat_id}")
    print("Send /make to run discovery")
    
    iterations = 0
    offset: int | None = None
    
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        
        response = poll_once(telegram_client, offset=offset, timeout=30)
        
        # Get next offset
        payload = response.get("payload") if isinstance(response, dict) else None
        if isinstance(payload, dict):
            updates = payload.get("result", [])
            if isinstance(updates, list):
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
        
        result = integration.run_once()
        
        if result.get("handled", 0) > 0:
            print(f"Handled {result['handled']} updates")


if __name__ == "__main__":
    start_v5_bot()
