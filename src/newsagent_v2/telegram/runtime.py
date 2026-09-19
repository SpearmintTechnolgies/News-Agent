"""V5 Telegram Bot Runtime - testable construction.

Separates runtime construction from main() for testability.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.discovery.source_registry import SourceRegistry
from newsagent_v2.telegram.client import TelegramTestClient
from newsagent_v2.telegram.config import TelegramConfig, load_telegram_config
from newsagent_v2.telegram.live_transport import create_live_transport
from newsagent_v2.telegram.singleton import acquire_singleton_lock, release_singleton_lock
from newsagent_v2.telegram.state import V5BotState
from newsagent_v2.control.make_v5_bridge import V5DiscoveryPipeline, create_v5_discovery_pipeline


@dataclass
class V5Runtime:
    """Fully constructed V5 Telegram bot runtime.
    
    All components initialized and ready for polling.
    """
    state: V5BotState
    config: TelegramConfig
    client: TelegramTestClient
    discovery: V5DiscoveryPipeline
    event_store: EventStore
    source_registry: SourceRegistry
    # Offset to resume from (None for fresh)
    offset: int | None = None


def build_runtime(
    environ: dict[str, str] | None = None,
    mock_transport: Any | None = None,
) -> V5Runtime:
    """Build complete V5 runtime with all dependencies.
    
    Args:
        environ: Environment variables (defaults to os.environ)
        mock_transport: Optional mock transport for testing
        
    Returns:
        Fully constructed V5Runtime
        
    Raises:
        SingletonError: If another instance is running
        ValueError: If credentials missing
        RuntimeError: If Telegram connection fails
    """
    env = environ or os.environ
    
    # Single-instance guard
    acquire_singleton_lock()
    
    # Load config (raises if credentials missing)
    config = load_telegram_config(env)
    
    # Create transport/client
    if mock_transport is not None:
        transport = mock_transport
    else:
        transport = create_live_transport(config)
    
    client = TelegramTestClient(
        config=config,
        transport=transport,
        live_send_enabled=True,
    )
    
    # Create discovery dependencies
    event_store = EventStore(root=Path("./data/events"))
    source_registry = SourceRegistry()
    
    # Create discovery pipeline (telegram_store created internally if needed)
    discovery = create_v5_discovery_pipeline(
        event_store=event_store,
        source_registry=source_registry,
    )
    
    # Initialize state
    state = V5BotState()
    
    return V5Runtime(
        state=state,
        config=config,
        client=client,
        discovery=discovery,
        event_store=event_store,
        source_registry=source_registry,
    )


def teardown_runtime(runtime: V5Runtime) -> None:
    """Clean up runtime resources."""
    release_singleton_lock()
