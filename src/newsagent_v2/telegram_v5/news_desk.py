"""V5 News Desk - Telegram interface for NewsEvents.

Renders story cards with:
- Headline
- Summary
- Why this matters
- Momentum
- Sources
- Action buttons
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import Any

from newsagent_v2.discovery.event_clusterer import NewsEvent
from newsagent_v2.discovery.event_store import EventStore
from newsagent_v2.intelligence.engine import EventIntelligence, IntelligenceEngine


@dataclass
class RenderedCard:
    """Rendered story card for Telegram."""
    event_id: str
    text: str
    parse_mode: str = "HTML"
    reply_markup: dict[str, Any] | None = None


class NewsDesk:
    """V5 News Desk for rendering event cards.

    Features:
    - Story cards with full metadata
    - Safe message limits
    - Deterministic summaries only
    - Source links
    """

    MAX_MESSAGE_LENGTH = 4096
    TRUNCATE_ELLIPSIS = "\n\n<i>(Message truncated for Telegram)</i>"

    def __init__(
        self,
        event_store: EventStore,
        intelligence: IntelligenceEngine,
    ):
        self.event_store = event_store
        self.intelligence = intelligence

    def _escape_html(self, text: str) -> str:
        """Escape HTML special characters."""
        return html.escape(str(text))

    def _format_time_ago(self, hours: float) -> str:
        """Format hours as human-readable time."""
        if hours < 1:
            return f"{int(hours * 60)} min"
        if hours < 24:
            return f"{int(hours)} hour{'s' if hours >= 2 else ''}"
        return f"{int(hours / 24)} day{'s' if hours >= 48 else ''}"

    def _build_summary_lines(
        self,
        event: NewsEvent,
        intel: EventIntelligence,
    ) -> list[str]:
        """Build summary lines for card."""
        lines: list[str] = []

        # Headline
        lines.append(f"<b>{self._escape_html(event.canonical_title)}</b>")
        lines.append("")

        # Short description (from representative)
        if event.reports:
            desc = event.reports[0].description
            if desc:
                # Truncate if needed
                desc = desc[:200] + "..." if len(desc) > 200 else desc
                lines.append(self._escape_html(desc))
                lines.append("")

        return lines

    def _build_why_matters(
        self,
        event: NewsEvent,
        intel: EventIntelligence,
    ) -> str | None:
        """Build 'Why this matters' context.

        Uses deterministic intelligence only.
        Does NOT hallucinate claims.
        """
        parts: list[str] = []

        # Impact primary area
        if intel.impact.primary_area:
            parts.append(f"Affects {intel.impact.primary_area}")

        # Development status
        if intel.evolution.developing_story:
            parts.append("developing")

        # Breaking signal
        if intel.breaking.is_breaking:
            parts.append("breaking news")

        # Has official source
        if event.has_primary_evidence:
            parts.append("official source")

        # Multi-source
        if event.source_count >= 2:
            parts.append(f"{event.source_count} source confirmation")

        if not parts:
            return None

        return "; ".join(parts)

    def _build_source_links(
        self,
        event: NewsEvent,
        max_links: int = 5,
    ) -> list[str]:
        """Build source link lines."""
        lines: list[str] = []
        lines.append("<b>Sources:</b>")

        # Sort by authority (primary sources first)
        sorted_reports = sorted(
            event.reports,
            key=lambda r: r.source_authority,
            reverse=True,
        )

        seen_sources: set[str] = set()
        count = 0

        for report in sorted_reports[:max_links]:
            if report.source in seen_sources:
                continue
            seen_sources.add(report.source)

            # Link format
            source_name = self._escape_html(report.source)
            url = report.url
            time_ago = ""
            if report.published_at and isinstance(report.published_at, str):
                try:
                    from datetime import datetime, timezone
                    dt = datetime.fromisoformat(report.published_at.replace("Z", "+00:00"))
                    hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
                    time_ago = f" ({self._format_time_ago(hours)} ago)"
                except Exception:
                    pass

            lines.append(f"• <a href=\"{url}\">{source_name}</a>{time_ago}")
            count += 1

        if len(event.reports) > max_links:
            lines.append(f"<i>+ {len(event.reports) - count} more sources</i>")

        return lines

    def _build_action_buttons(
        self,
        event: NewsEvent,
    ) -> dict[str, Any]:
        """Build inline keyboard buttons."""
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "▶️  RUN STORY",
                        "callback_data": f"v5:run:{event.event_id}",
                    }
                ],
                [
                    {
                        "text": "🔔  FOLLOW",
                        "callback_data": f"v5:follow:{event.event_id}",
                    },
                    {
                        "text": "🚫  IGNORE",
                        "callback_data": f"v5:ignore:{event.event_id}",
                    },
                ],
            ],
        }

    def render_card(
        self,
        event: NewsEvent,
        intel: EventIntelligence | None = None,
    ) -> RenderedCard:
        """Render a story card for an event."""
        if intel is None:
            intel = self.intelligence.analyze(event)

        lines: list[str] = []

        # Story ID and time
        time_ago = self._format_time_ago(event.age_hours) if event.age_hours else "recent"
        story_id_line = f"📰  <b>STORY #{event.event_id[-6:].upper()}</b>  |  {time_ago} ago"

        lines.append(story_id_line)
        lines.append("")

        # Headline
        lines.append(f"<b>{self._escape_html(event.canonical_title)}</b>")
        lines.append("")

        # Summary
        if event.reports:
            desc = event.reports[0].description
            if desc:
                desc_clean = desc[:300] + "..." if len(desc) > 300 else desc
                lines.append(self._escape_html(desc_clean))
                lines.append("")

        # Why this matters (deterministic only)
        why = self._build_why_matters(event, intel)
        if why:
            lines.append(f"<b>Why this matters:</b> {self._escape_html(why)}")
            lines.append("")

        # Momentum
        lines.append(f"<b>Momentum:</b> {intel.momentum.level}")

        # Sources
        lines.append(f"<b>Sources:</b> {event.source_count}")

        # Developing
        if intel.evolution.developing_story:
            lines.append("<b>Developing:</b> YES")
        else:
            lines.append("<b>Developing:</b> NO")

        # Novelty
        if intel.novelty.is_repeat:
            lines.append("<b>Novelty:</b> Repeat coverage")
        else:
            lines.append(f"<b>Novelty:</b> {intel.novelty.level}")

        # Impact
        if intel.impact.primary_area:
            lines.append(f"<b>Impact:</b> {intel.impact.primary_area}")

        # Breaking
        if intel.breaking.is_breaking:
            lines.append("🚨 <b>BREAKING SIGNAL</b>")

        lines.append("")

        # Source links
        lines.extend(self._build_source_links(event))

        # Join and check length
        text = "\n".join(lines)

        # Truncate if needed
        if len(text) > self.MAX_MESSAGE_LENGTH:
            max_content = self.MAX_MESSAGE_LENGTH - len(self.TRUNCATE_ELLIPSIS)
            text = text[:max_content] + self.TRUNCATE_ELLIPSIS

        # Build buttons
        buttons = self._build_action_buttons(event)

        return RenderedCard(
            event_id=event.event_id,
            text=text,
            reply_markup=buttons,
        )

    def render_cards_batch(
        self,
        events: list[NewsEvent],
    ) -> list[RenderedCard]:
        """Render cards for multiple events."""
        cards: list[RenderedCard] = []
        for event in events:
            intel = self.intelligence.analyze(event)
            card = self.render_card(event, intel)
            cards.append(card)
        return cards

    def render_summary(
        self,
        new_events: int,
        updated_events: int,
        ignored_events: int,
        discovery_stats: dict[str, Any] | None = None,
    ) -> str:
        """Render summary for /make completion.

        V5 diagnostic summary.
        """
        lines: list[str] = []
        lines.append("📊 <b>NewsAgent V5 Discovery Complete</b>")
        lines.append("")
        lines.append(f"New events: <b>{new_events}</b>")
        lines.append(f"Updated events: <b>{updated_events}</b>")
        if ignored_events > 0:
            lines.append(f"Ignored: <b>{ignored_events}</b>")

        # Discovery stats
        if discovery_stats:
            lines.append("")
            lines.append("<b>Discovery:</b>")
            lines.append(f"  Sources: {discovery_stats.get('sources_succeeded', 0)}/{discovery_stats.get('sources_attempted', 0)}")
            lines.append(f"  Reports: {discovery_stats.get('raw_items_collected', 0)}")
            lines.append(f"  Events: {discovery_stats.get('events_created', 0)}")

        # Cost boundary
        lines.append("")
        lines.append("💰 <b>Cost:</b> ₹0 (deterministic discovery)")

        return "\n".join(lines)
