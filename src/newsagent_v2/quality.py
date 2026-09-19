from __future__ import annotations

from urllib.parse import urlparse


def valid_url(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def validate_evidence_pack(pack: dict) -> list[str]:
    errors: list[str] = []
    stories = pack.get("stories", [])

    if not stories:
        errors.append("No stories selected")
        return errors

    for i, story in enumerate(stories, 1):
        event_id = story.get("event_id", f"story-{i}")
        representative = story.get("representative", {})

        if not representative.get("title"):
            errors.append(
                f"{event_id}: representative missing title"
            )

        if not representative.get("source"):
            errors.append(
                f"{event_id}: representative missing source"
            )

        rep_url = representative.get("url", "")
        if not valid_url(rep_url):
            errors.append(
                f"{event_id}: representative invalid URL"
            )

        sources = story.get("sources", [])
        source_count = story.get("source_count", 0)

        if not sources:
            errors.append(
                f"{event_id}: no evidence sources"
            )

        if source_count != len(sources):
            errors.append(
                f"{event_id}: source_count mismatch"
            )

        primary = story.get("primary_evidence", [])
        secondary = story.get("secondary_evidence", [])

        if not primary and not secondary:
            errors.append(
                f"{event_id}: no evidence items"
            )

        if story.get("has_primary_evidence") != bool(primary):
            errors.append(
                f"{event_id}: primary evidence flag mismatch"
            )

        for group_name, evidence_items in (
            ("primary", primary),
            ("secondary", secondary),
        ):
            for j, item in enumerate(evidence_items, 1):
                if not item.get("source"):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} missing source"
                    )

                if not item.get("title"):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} missing title"
                    )

                if not valid_url(item.get("url", "")):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} invalid URL"
                    )

                if not item.get("source_type"):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} missing source_type"
                    )

                if not item.get("source_role"):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} missing source_role"
                    )

                authority = item.get("source_authority")

                if not isinstance(authority, (int, float)):
                    errors.append(
                        f"{event_id}: {group_name} evidence {j} invalid source_authority"
                    )

    return errors