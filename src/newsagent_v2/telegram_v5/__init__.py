"""V5 Telegram News Desk.

Telegram interface for /make command with NewsEvent cards.
"""

from __future__ import annotations

from .news_desk import NewsDesk, RenderedCard
from .state_machine import StoryStateMachine, EventState, StateTransition
from .callbacks import CallbackHandler

__all__ = [
    "NewsDesk",
    "RenderedCard",
    "StoryStateMachine",
    "EventState",
    "StateTransition",
    "CallbackHandler",
]
