#!/usr/bin/env python3
"""generate_draft.py — build placement-aware backlink drafts for approval."""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_ROOT / "database") not in sys.path:
    sys.path.insert(0, str(_ROOT / "database"))
if str(_ROOT / "workflows") not in sys.path:
    sys.path.insert(0, str(_ROOT / "workflows"))

import backlink_db  # noqa: E402
import workflow_manager  # noqa: E402

CAMPAIGN_CONFIG = _ROOT / "config" / "campaign.json"

DEFAULT_TOPICS = (
    "post-quantum cryptography and migration planning",
    "zero-knowledge proofs in practical applications",
    "modern TLS and certificate transparency",
    "secure key management for engineering teams",
)

TONE_KEYWORDS = {
    "technical": (
        "From an implementation standpoint, protocol designers must weigh trade-offs "
        "between performance, interoperability, and threat models."
    ),
    "conversational": (
        "In plain terms, good cryptography is less about exotic math and more about "
        "disciplined engineering habits your team can maintain."
    ),
    "formal": (
        "Organizations evaluating cryptographic controls should document assumptions, "
        "rotation procedures, and verification steps in auditable form."
    ),
}


@dataclass
class DraftContext:
    workflow_id: str
    opportunity_id: int
    target_url: str
    brand_name: str
    host_domain: str
    host_url: str
    host_title: str | None
    placement_type: str
    final_score: float | None
    topic: str
    signals: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DraftResult:
    draft_text: str
    tone: str
    confidence: float
    placement_type: str
    topic: str
    version_hint: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_campaign_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CAMPAIGN_CONFIG
    if not config_path.is_file():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _host_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc.lower() or "the host site"


def _pick_topic(config: dict[str, Any], opportunity: backlink_db.OpportunityRow) -> str:
    topics = config.get("default_topics")
    if isinstance(topics, list) and topics:
        index = opportunity.id % len(topics)
        return str(topics[index])

    haystack = f"{opportunity.title or ''} {opportunity.snippet or ''}".lower()
    if "blockchain" in haystack:
        return DEFAULT_TOPICS[1]
    if "security" in haystack or "encryption" in haystack:
        return DEFAULT_TOPICS[2]
    return DEFAULT_TOPICS[0]


def _load_signals(opportunity: backlink_db.OpportunityRow, db_path: str) -> dict[str, Any]:
    if opportunity.context_json:
        try:
            context = json.loads(opportunity.context_json)
            signals = context.get("discovery_signals")
            if isinstance(signals, dict):
                return signals
        except json.JSONDecodeError:
            pass
    parsed = backlink_db.get_parsed_page(opportunity.url, db_path=db_path)
    if parsed and isinstance(parsed.get("signals"), dict):
        return parsed["signals"]
    return {}


def build_draft_context(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> DraftContext:
    row = workflow_manager.load(workflow_id, db_path=db_path)
    if not row.opportunity_id:
        raise ValueError(f"Workflow has no linked opportunity: {workflow_id}")

    opp = backlink_db.get_opportunity(row.opportunity_id, db_path=db_path)
    if not opp:
        raise ValueError(f"Opportunity not found for workflow: {workflow_id}")

    config = load_campaign_config()
    target_url = str(config.get("target_url") or f"https://{config.get('target_domain', 'cryptography.com')}")
    brand_name = str(config.get("brand_name") or config.get("name") or "Cryptography.com")
    topic = _pick_topic(config, opp)
    signals = _load_signals(opp, db_path)

    return DraftContext(
        workflow_id=workflow_id,
        opportunity_id=opp.id,
        target_url=target_url.rstrip("/"),
        brand_name=brand_name,
        host_domain=_host_label(opp.url),
        host_url=opp.url,
        host_title=opp.title,
        placement_type=opp.placement_type or "guest_post",
        final_score=opp.final_score,
        topic=topic,
        signals=signals,
    )


def _title_for_context(context: DraftContext) -> str:
    if context.host_title and len(context.host_title) < 90:
        base = context.host_title.strip()
        if "write for us" in base.lower():
            return f"{context.topic.title()} — A Practical Perspective"
    return f"{context.topic.title()} — Insights for {context.host_domain}"


def _guest_post_body(context: DraftContext, *, tone: str) -> str:
    tone_paragraph = TONE_KEYWORDS.get(tone, TONE_KEYWORDS["technical"])
    return (
        f"Organizations adopting stronger cryptography often underestimate how much operational "
        f"discipline matters once keys leave the lab. This article explores {context.topic} and "
        f"what engineering teams should prioritize when hardening production systems.\n\n"
        f"{tone_paragraph}\n\n"
        f"A practical starting point is to map data flows, identify where secrets are created and "
        f"stored, and verify that rotation and revocation paths are tested—not just documented. "
        f"Readers who want deeper reference material can explore {context.target_url} for guides "
        f"on modern cryptographic design patterns.\n\n"
        f"For teams publishing on {context.host_domain}, the goal is not perfection on day one. "
        f"It is measurable improvement: fewer long-lived keys, clearer ownership, and review "
        f"checklists that survive staff turnover."
    )


def _comment_body(context: DraftContext, *, tone: str) -> str:
    tone_paragraph = TONE_KEYWORDS.get(tone, TONE_KEYWORDS["conversational"])
    return (
        f"Strong article. {context.topic.capitalize()} is exactly where many teams struggle once "
        f"they move beyond checkbox compliance. {tone_paragraph} "
        f"We've been collecting practical notes on this at {context.target_url}."
    )


def _form_submission_body(context: DraftContext, *, tone: str) -> str:
    return _guest_post_body(context, tone=tone)


def _detect_tone(edit_prompt: str | None) -> str:
    if not edit_prompt:
        return "technical"
    lower = edit_prompt.lower()
    if any(word in lower for word in ("casual", "friendly", "conversational")):
        return "conversational"
    if any(word in lower for word in ("formal", "professional", "executive")):
        return "formal"
    if "technical" in lower:
        return "technical"
    return "technical"


def _apply_edit_prompt(base: str, edit_prompt: str | None) -> str:
    if not edit_prompt:
        return base

    lower = edit_prompt.lower()
    extra_sections: list[str] = []

    if "technical" in lower:
        extra_sections.append(
            "## Technical revision\n\n"
            "Technical readers may also want explicit guidance on algorithm agility: "
            "design interfaces so cipher suites, key sizes, and trust stores can be updated "
            "without redeploying entire services."
        )
    if "shorter" in lower or "concise" in lower:
        paragraphs = [p.strip() for p in base.split("\n\n") if p.strip()]
        base = "\n\n".join(paragraphs[:2])
    if "link" in lower and "cryptography.com" not in base.lower():
        base = f"{base}\n\nReference: https://cryptography.com"

    if extra_sections:
        base = f"{base}\n\n" + "\n\n".join(extra_sections)

    if edit_prompt.strip():
        base = f"{base}\n\n---\nEditor request: {edit_prompt.strip()}"
    return base


def generate_draft_text(
    context: DraftContext,
    *,
    edit_prompt: str | None = None,
    previous_draft: str | None = None,
) -> DraftResult:
    tone = _detect_tone(edit_prompt)
    title = _title_for_context(context)

    if context.placement_type == "comment":
        body = _comment_body(context, tone=tone)
        draft_text = body
        confidence = 0.72
    else:
        body = _guest_post_body(context, tone=tone)
        if context.placement_type == "form_submission":
            body = _form_submission_body(context, tone=tone)

        bio = (
            f"Author bio: Contributor at {context.brand_name}. "
            f"Learn more at {context.target_url}."
        )
        draft_text = (
            f"Title: {title}\n\n"
            f"{body}\n\n"
            f"{bio}\n\n"
            f"Suggested anchor: {context.target_url}\n"
            f"Target placement: {context.placement_type} on {context.host_url}"
        )
        confidence = 0.78 if context.placement_type == "guest_post" else 0.68

    if previous_draft and edit_prompt:
        draft_text = _apply_edit_prompt(previous_draft, edit_prompt)
    elif edit_prompt:
        draft_text = _apply_edit_prompt(draft_text, edit_prompt)

    if context.final_score is not None and context.final_score < 60:
        confidence = max(0.45, confidence - 0.1)

    return DraftResult(
        draft_text=draft_text.strip(),
        tone=tone,
        confidence=round(confidence, 2),
        placement_type=context.placement_type,
        topic=context.topic,
    )


def draft_workflow(
    workflow_id: str,
    *,
    db_path: str = backlink_db.DEFAULT_DB_PATH,
) -> tuple[DraftResult, backlink_db.DraftRow]:
    context = build_draft_context(workflow_id, db_path=db_path)
    session = backlink_db.get_edit_session(workflow_id, db_path=db_path)
    previous = backlink_db.get_latest_draft(workflow_id, db_path=db_path)
    edit_prompt = session.edit_prompt if session else None
    previous_text = previous.draft_text if previous and edit_prompt else None

    generated = generate_draft_text(
        context,
        edit_prompt=edit_prompt,
        previous_draft=previous_text,
    )
    saved = backlink_db.save_draft(
        workflow_id,
        generated.draft_text,
        tone=generated.tone,
        confidence=generated.confidence,
        db_path=db_path,
    )
    backlink_db.insert_log(
        workflow_id,
        "drafting",
        f"Draft v{saved.version} ({generated.placement_type})",
        detail={
            "topic": generated.topic,
            "tone": generated.tone,
            "confidence": generated.confidence,
            "edit_applied": bool(edit_prompt),
        },
        db_path=db_path,
    )
    return generated, saved
