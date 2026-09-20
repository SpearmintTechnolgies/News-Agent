"""V5 Telegram cards for NewsEvent discovery results.

Simple, safe formatting compatible with actual NewsEvent structure.
"""

from __future__ import annotations

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.telegram.cards import html_caption
from newsagent_v2.telegram.contract import CALLBACK_DATA_LIMIT
from newsagent_v2.telegram.v5_callbacks import RUN_PREFIX, FOLLOW_PREFIX, IGNORE_PREFIX, SEENEXT_PREFIX


def compact_event_card(
    event: NewsEvent,
    rank: int,
    total: int,
) -> str:
    """Compact card format for telegram.
    
    Keeps well under 4096 bytes for message limit safety.
    """
    # Get source count from the property
    source_count = len(event.sources) if hasattr(event, 'sources') else len(event.reports)
    
    lines = [
        f"<b>{rank}/{total}</b> {html_caption(event.canonical_title)}",
        "",
        f"Topic: {html_caption(event.topic)} | Reports: {len(event.reports)} | Sources: {source_count}",
    ]
    
    # Add entities if available
    if event.entities:
        entities_list = list(event.entities)[:5]
        lines.append(f"Entities: {', '.join(entities_list)}")
    
    return "\n".join(lines)


def make_callback_data(prefix: str, event_id: str) -> str:
    """Create callback data: prefix:event_id.
    
    Must stay under 64 bytes.
    """
    payload = f"{prefix}:{event_id}"
    if len(payload) > CALLBACK_DATA_LIMIT:
        max_event = CALLBACK_DATA_LIMIT - len(prefix) - 2
        payload = f"{prefix}:{event_id[:max_event]}"
    return payload


def discovery_keyboard(event_id: str, source_url: str | None = None) -> dict[str, list]:
    """Create inline keyboard for discovery card.
    
    [RUN STORY] [FOLLOW] [IGNORE]
    [🔗 OPEN SOURCE] (if URL provided)
    """
    # Main row with existing buttons
    main_row = [
        {
            "text": "▶ RUN STORY",
            "callback_data": make_callback_data(RUN_PREFIX, event_id),
        },
        {
            "text": "👁 FOLLOW",
            "callback_data": make_callback_data(FOLLOW_PREFIX, event_id),
        },
        {
            "text": "🚫 IGNORE",
            "callback_data": make_callback_data(IGNORE_PREFIX, event_id),
        },
    ]
    
    rows = [main_row]
    
    # Add OPEN SOURCE button if valid URL
    if source_url and source_url.startswith(("http://", "https://")):
        rows.append([
            {
                "text": "🔗 OPEN SOURCE",
                "url": source_url,  # URL button opens directly, not callback
            }
        ])
    
    return {"inline_keyboard": rows}


def seepage_keyboard(offset: int = 5) -> dict[str, list]:
    """Create SEE NEXT 5 keyboard."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "SEE NEXT 5",
                    "callback_data": make_callback_data(SEENEXT_PREFIX, str(offset)),
                },
            ]
        ]
    }


def render_card_from_event(
    event: NewsEvent,
    rank: int,
    total_events: int,
) -> dict[str, any]:
    """Full render for one NewsEvent card."""
    compact = compact_event_card(event, rank, total_events)
    
    # Get primary/canonical source URL for OPEN SOURCE button
    source_url = event.primary_url
    
    return {
        "text": compact,
        "parse_mode": "HTML",
        "reply_markup": discovery_keyboard(event.event_id, source_url),
        "event_id": event.event_id,
        "rank": rank,
    }
