"""Constrained prose renderer contract. Fake renderer only — no provider calls."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim, LedgerQuote
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, ParagraphPlan
from newsagent_v2.article.writer.controlled.renderer_contract import (
    FORBIDDEN_IMPLICATION_PHRASES,
    paragraph_fact_budget,
)
from newsagent_v2.article.writer.controlled.proposition import proposition_frame_from_semantic_fact
from newsagent_v2.article.writer.controlled.semantic import semantic_fact_from_claim, verbalize_semantic_fact

CONTROLLED_WRITER_V3_SYSTEM_PROMPT = """You are a constrained prose REALIZER for CoinNetwork.
You verbalize a PRE-APPROVED ArticlePlan. You do not choose facts.

Return EXACTLY ONE JSON OBJECT at the root. Follow the supplied schema.
Use only supplied fact IDs. Each factual sentence must include fact_ids_used and quote_ids_used.
You may combine multiple authorized facts in one sentence when that sentence only enumerates,
conjoins, sequences, or gives ordinary grammatical context without changing their meaning.
relationship=NONE allows multi-fact enumeration and ordinary conjunction; it does not authorize
invented cause, motive, comparison, contrast, dependent ordering, prediction, or inference.
Preserve polarity, modal, numbers, names, attribution, and dates. Never invert negation.
Proposition slots are semantic roles, not text to concatenate. Natural grammatical English is allowed.
Non-factual grammatical glue (articles, clear pronouns, ordinary conjunctions, syntactic transitions)
is allowed. Do not add new factual propositions, inference, causality, prediction, significance,
motive, or background beyond the EditorialDossier.
Do not reconstruct source headlines or distinctive source syntax.
Do not pad with extra facts. If authorized facts cannot fill the word target, write fewer words.
Reproduce allowed quotes exactly. Do not browse, use tools, or call other models.
""".strip()


@dataclass
class RenderedParagraph:
    paragraph_id: str
    text: str
    subheading: str = ""


@dataclass
class RendererResult:
    ok: bool
    paragraphs: list[RenderedParagraph] = field(default_factory=list)
    headline_text: str = ""
    dek_text: str = ""
    seo_title: str = ""
    meta_description: str = ""
    slug: str = ""
    entities: list[dict[str, Any]] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    error: str | None = None
    provider_error: bool = False
    invalid_output: bool = False
    budget_exceeded: bool = False
    native: dict[str, Any] | None = None


class ProseRenderer(Protocol):
    renderer_name: str

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        """Verbalize the plan. Must not invent claims or quotes."""


def _claim(ledgers: EvidenceLedgers, claim_id: str) -> LedgerClaim | None:
    return ledgers.claim_by_id().get(claim_id)


def _quote(ledgers: EvidenceLedgers, quote_id: str) -> LedgerQuote | None:
    for row in ledgers.quotes:
        if row.quote_id == quote_id:
            return row
    return None


def _join_claims(claim_ids: tuple[str, ...], ledgers: EvidenceLedgers) -> str:
    parts: list[str] = []
    for claim_id in claim_ids:
        row = _claim(ledgers, claim_id)
        if row is None:
            continue
        # Prefer ledger claim prose for offline fake rendering (not slot glue).
        text = str(row.text or "").strip()
        if not text:
            text = verbalize_semantic_fact(semantic_fact_from_claim(row))
        parts.append(text)
    return " ".join(parts).strip()


def _with_quotes(text: str, quote_ids: tuple[str, ...], ledgers: EvidenceLedgers) -> str:
    extras: list[str] = []
    lower = text.lower()
    for quote_id in quote_ids:
        row = _quote(ledgers, quote_id)
        if row is None:
            continue
        if row.text.lower() in lower:
            continue
        speaker = row.speaker or "the speaker"
        extras.append(f'{speaker} said, "{row.text}"')
    if not extras:
        return text
    return (text + " " + " ".join(extras)).strip()


class FakeProseRenderer:
    """Offline renderer. Does not call Qwen, Groq, Gemini, or Kimi."""

    renderer_name = "fake_prose_renderer"

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        paragraphs: list[RenderedParagraph] = []
        for para in plan.paragraph_plans:
            body = _join_claims(para.required_claim_ids or para.allowed_claim_ids, ledgers)
            body = _with_quotes(body, para.allowed_quote_ids, ledgers)
            paragraphs.append(
                RenderedParagraph(
                    paragraph_id=para.paragraph_id,
                    text=body,
                    subheading=para.subheading,
                )
            )
        headline = _join_claims(plan.headline_claim_ids, ledgers)
        if plan.headline_requirements:
            req = plan.headline_requirements
            parts = [req.get("primary_actor") or "", req.get("primary_action") or "", req.get("primary_object") or ""]
            built = " ".join(part for part in parts if part).strip()
            if built:
                headline = built
        dek = _join_claims(plan.dek_claim_ids, ledgers)
        if plan.dek_requirements:
            req = plan.dek_requirements
            parts = [req.get("primary_actor") or "", req.get("primary_action") or "", req.get("primary_object") or ""]
            built = " ".join(part for part in parts if part).strip()
            if built:
                dek = built
        return RendererResult(
            ok=True,
            paragraphs=paragraphs,
            headline_text=headline,
            dek_text=dek,
        )


class ScriptedProseRenderer:
    """Test double. Maps paragraph_id → text. Can simulate provider/invalid failures."""

    renderer_name = "scripted_prose_renderer"

    def __init__(
        self,
        texts: dict[str, str] | None = None,
        *,
        headline: str = "",
        dek: str = "",
        provider_error: str | None = None,
        invalid_output: bool = False,
        budget_exceeded: bool = False,
        fallback: ProseRenderer | None = None,
    ) -> None:
        self.texts = texts or {}
        self.headline = headline
        self.dek = dek
        self.provider_error = provider_error
        self.invalid_output = invalid_output
        self.budget_exceeded = budget_exceeded
        self.fallback = fallback or FakeProseRenderer()

    def render(self, plan: ArticlePlan, ledgers: EvidenceLedgers) -> RendererResult:
        if self.budget_exceeded:
            return RendererResult(
                ok=False,
                error=self.provider_error or "WRITER_PROVIDER_BUDGET_EXCEEDED",
                provider_error=True,
                budget_exceeded=True,
            )
        if self.provider_error:
            return RendererResult(
                ok=False,
                error=self.provider_error,
                provider_error=True,
            )
        if self.invalid_output:
            return RendererResult(
                ok=False,
                error="invalid_renderer_payload",
                invalid_output=True,
            )
        base = self.fallback.render(plan, ledgers)
        paragraphs: list[RenderedParagraph] = []
        for row in base.paragraphs:
            text = self.texts.get(row.paragraph_id, row.text)
            paragraphs.append(
                RenderedParagraph(
                    paragraph_id=row.paragraph_id,
                    text=text,
                    subheading=row.subheading,
                )
            )
        return RendererResult(
            ok=True,
            paragraphs=paragraphs,
            headline_text=self.headline or base.headline_text,
            dek_text=self.dek or base.dek_text,
        )


def renderer_fact_ids(plan: ArticlePlan) -> set[str]:
    """Union of paragraph required/optional fact IDs. Does not mutate the plan or ledger."""
    ids: set[str] = set()
    for para in plan.paragraph_plans:
        ids.update(para.required_claim_ids)
        ids.update(para.optional_claim_ids)
    return ids


def renderer_quote_ids(plan: ArticlePlan) -> set[str]:
    ids: set[str] = set()
    for para in plan.paragraph_plans:
        ids.update(para.allowed_quote_ids)
    return ids


def assert_renderer_projection(plan: ArticlePlan, ledgers: EvidenceLedgers) -> set[str]:
    """Provider projection only. Full EvidenceClaimLedger stays intact."""
    ledger_ids = {row.claim_id for row in ledgers.claims}
    fact_ids = renderer_fact_ids(plan)
    if not fact_ids <= ledger_ids:
        missing = sorted(fact_ids - ledger_ids)
        raise RuntimeError(f"renderer_fact_ids not in EvidenceClaimLedger: {missing}")
    for para in plan.paragraph_plans:
        for fact_id in para.required_claim_ids:
            if fact_id not in ledger_ids:
                raise RuntimeError(f"required_fact_id missing from EvidenceClaimLedger: {fact_id}")
    return fact_ids


def controlled_renderer_payload(plan: ArticlePlan, ledgers: EvidenceLedgers) -> dict[str, Any]:
    claims = ledgers.claim_by_id()
    quotes = {row.quote_id: row for row in ledgers.quotes}
    fact_ids = assert_renderer_projection(plan, ledgers)
    paragraph_plans: list[dict[str, Any]] = []
    for para in plan.paragraph_plans:
        para_fact_ids = tuple(
            fid
            for fid in (*para.required_claim_ids, *para.optional_claim_ids)
            if fid in fact_ids and fid in claims
        )
        paragraph_plans.append(
            {
                "paragraph_id": para.paragraph_id,
                "subheading": para.subheading,
                "target_word_range": list(para.target_word_range),
                "required_fact_ids": list(para.required_claim_ids),
                "optional_fact_ids": list(para.optional_claim_ids),
                "max_factual_assertions": paragraph_fact_budget(para),
                "relationship": para.relationship,
                "proposition_frames": [
                    proposition_frame_from_semantic_fact(semantic_fact_from_claim(claims[cid])).as_dict()
                    for cid in para_fact_ids
                ],
                "allowed_quote_ids": list(para.allowed_quote_ids),
                "allowed_quotes": [
                    {
                        "quote_id": qid,
                        "exact_text": quotes[qid].text,
                        "speaker": quotes[qid].speaker,
                    }
                    for qid in para.allowed_quote_ids
                    if qid in quotes
                ],
            }
        )
    return {
        "task": (
            "Realize each proposition frame as one natural complete English sentence. "
            "Return exactly one JSON object; paragraphs[].sentences[] need sentence_id, text, "
            "fact_ids_used, quote_ids_used."
        ),
        "event_id": plan.event_id,
        "headline_requirements": dict(plan.headline_requirements),
        "dek_requirements": dict(plan.dek_requirements),
        "category": plan.category,
        "seo_inputs": dict(plan.seo_inputs),
        "renderer_fact_ids": sorted(fact_ids),
        "paragraph_plans": paragraph_plans,
        "requirements": {
            "hard_minimum_words": 350,
            "target_min_words": 450,
            "target_max_words": 800,
            "invent_filler": False,
            "pad_to_word_target": False,
            "one_sentence_one_assertion": True,
            "preserve_polarity": True,
            "allowed_relationships": [
                "NONE",
                "ATTRIBUTION",
                "SEQUENCE",
                "CONTRAST",
                "ELABORATION",
            ],
            "forbidden_inferred_relationships": [
                "CAUSE",
                "CONSEQUENCE",
                "MOTIVE",
                "PREDICTION",
                "SIGNIFICANCE",
            ],
            "forbidden_implication_phrases": list(FORBIDDEN_IMPLICATION_PHRASES),
        },
    }


def controlled_renderer_messages(plan: ArticlePlan, ledgers: EvidenceLedgers) -> list[dict[str, str]]:
    """Prompt contract. This function does not call a model."""
    payload = controlled_renderer_payload(plan, ledgers)
    return [
        {"role": "system", "content": CONTROLLED_WRITER_V3_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def allowed_ids_for_paragraph(plan: ParagraphPlan) -> tuple[set[str], set[str]]:
    return set(plan.allowed_claim_ids), set(plan.allowed_quote_ids)
