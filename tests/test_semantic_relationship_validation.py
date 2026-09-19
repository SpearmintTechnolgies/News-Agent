"""Semantic multi-fact relationship validation + event-005 false-quarantine replay."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.policy import NORMAL_ARTICLE_POLICY
from newsagent_v2.article.qa.textutil import word_count
from newsagent_v2.article.writer.controlled.assembler import assemble_canonical_article
from newsagent_v2.article.writer.controlled.groq_oss20 import parse_controlled_v3_native
from newsagent_v2.article.writer.controlled.paragraph import validate_paragraph
from newsagent_v2.article.writer.controlled.plan import ArticlePlan, ParagraphPlan
from newsagent_v2.article.writer.controlled.realization import REALIZATION_INVALID, apply_realization_filter
from newsagent_v2.article.writer.controlled.renderer import RenderedParagraph
from newsagent_v2.article.writer.controlled.renderer_contract import (
    AUTHORIZED_RELATIONSHIP,
    MULTI_FACT_ENUMERATION,
    UNAUTHORIZED_RELATIONSHIP,
    UNAUTHORIZED_RELATIONSHIP_KIND,
    UNKNOWN_FACT_ID,
    classify_multi_fact_relationship,
    validate_sentence_declarations,
)
from newsagent_v2.article.writer.controlled.quarantine import (
    AssertionUnit,
    STATUS_GROUNDED,
    STATUS_UNSUPPORTED,
)
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from tests.test_controlled_writer_v3 import _sentence

REPO = Path(__file__).resolve().parents[1]
EVENT_024_DIAG = (
    REPO
    / "output"
    / "make_runs"
    / "make-20260916T135955Z"
    / "attempts"
    / "event-024"
    / "writer_diagnostic.json"
)
EVENT_005_ATTEMPT = (
    REPO
    / "output"
    / "make_runs"
    / "make-20260916T135955Z"
    / "attempts"
    / "event-005"
)


def _claims() -> tuple[LedgerClaim, ...]:
    return tuple(LedgerClaim(f"C{n:02d}", _sentence(n), "fact", ("e1",)) for n in range(1, 5))


def _article_plan(
    *,
    relationship: str = "NONE",
    claim_ids: tuple[str, ...] = ("C01", "C02", "C03"),
) -> ArticlePlan:
    para = ParagraphPlan(
        paragraph_id="p01",
        editorial_purpose="key_facts",
        allowed_claim_ids=claim_ids,
        allowed_quote_ids=(),
        target_word_range=(20, 120),
        required_claim_ids=claim_ids,
        optional_claim_ids=(),
        relationship=relationship,
    )
    return ArticlePlan(
        event_id="event-rel",
        headline_claim_ids=(claim_ids[0],),
        dek_claim_ids=(claim_ids[1] if len(claim_ids) > 1 else claim_ids[0],),
        paragraph_plans=(para,),
        subheading_plans=(),
        seo_inputs={},
        category="other",
        selected_claim_ids=claim_ids,
        selected_quote_ids=(),
        headline_requirements={},
        dek_requirements={},
        planned_safe_words=80,
        minimum_surviving_words=60,
        paragraph_loss_tolerance=0,
    )


def _native(text: str, fact_ids: list[str], *, pid: str = "p01") -> dict:
    return {
        "paragraphs": [
            {
                "paragraph_id": pid,
                "sentences": [
                    {
                        "sentence_id": "s01",
                        "text": text,
                        "fact_ids_used": fact_ids,
                        "quote_ids_used": [],
                    }
                ],
            }
        ]
    }


class SemanticRelationshipValidationTests(unittest.TestCase):
    def test_01_multi_fact_simple_conjunction_none_passes(self) -> None:
        text = f"{_sentence(1).rstrip('.')} and {_sentence(2)}"
        kind = classify_multi_fact_relationship(
            text, fact_ids=["C01", "C02"], paragraph_relationship="NONE"
        )
        self.assertEqual(kind, MULTI_FACT_ENUMERATION)
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertFalse(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_02_multi_fact_enumeration_passes(self) -> None:
        text = f"{_sentence(1)} {_sentence(2)} {_sentence(3)}"
        kind = classify_multi_fact_relationship(
            text, fact_ids=["C01", "C02", "C03"], paragraph_relationship="NONE"
        )
        self.assertEqual(kind, MULTI_FACT_ENUMERATION)
        issues = validate_sentence_declarations(
            _native(text, ["C01", "C02", "C03"]), _article_plan()
        )
        self.assertEqual(issues, [])

    def test_03_unauthorized_therefore_fails(self) -> None:
        text = f"{_sentence(1).rstrip('.')} therefore {_sentence(2)}"
        kind = classify_multi_fact_relationship(
            text, fact_ids=["C01", "C02"], paragraph_relationship="NONE"
        )
        self.assertEqual(kind, UNAUTHORIZED_RELATIONSHIP_KIND)
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_04_unauthorized_causal_fails(self) -> None:
        text = f"{_sentence(1).rstrip('.')} because {_sentence(2)}"
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_05_unauthorized_comparison_fails(self) -> None:
        text = f"{_sentence(1).rstrip('.')} outperformed {_sentence(2)}"
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_06_unauthorized_motivation_fails(self) -> None:
        text = f"{_sentence(1).rstrip('.')} aimed at {_sentence(2)}"
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_07_unauthorized_prediction_fails(self) -> None:
        text = f"{_sentence(1).rstrip('.')} could lead to {_sentence(2)}"
        issues = validate_sentence_declarations(_native(text, ["C01", "C02"]), _article_plan())
        self.assertTrue(any(item["code"] == UNAUTHORIZED_RELATIONSHIP for item in issues))

    def test_08_changed_attribution_fails(self) -> None:
        ledgers = EvidenceLedgers(event_id="e", claims=_claims(), quotes=())
        plan = ParagraphPlan(
            paragraph_id="p01",
            editorial_purpose="key_facts",
            allowed_claim_ids=("C01", "C02"),
            allowed_quote_ids=(),
            target_word_range=(10, 80),
            required_claim_ids=("C01", "C02"),
            optional_claim_ids=(),
            relationship="NONE",
        )
        # Invented attribution / speaker not present in ledger claims.
        bad = (
            "Senator Jane Doe announced that the bill would halt all crypto trading "
            "nationwide tomorrow across every major exchange."
        )
        result = validate_paragraph(RenderedParagraph(paragraph_id="p01", text=bad), plan, ledgers)
        self.assertFalse(result.assemblable)
        self.assertEqual(result.text, "")

    def test_09_changed_polarity_fails_realization(self) -> None:
        from newsagent_v2.article.writer.controlled.realization import sentence_realization_issues

        # Modal/polarity invention under relationship=NONE is realization-invalid.
        text = (
            f"{_sentence(1).rstrip('.')} therefore markets will now collapse overnight."
        )
        issues = sentence_realization_issues(text, relationship="NONE")
        self.assertTrue(any(item["code"] == REALIZATION_INVALID for item in issues))
        # Also catch via multi-fact relationship classifier when two facts are declared.
        kind = classify_multi_fact_relationship(
            text, fact_ids=["C01", "C02"], paragraph_relationship="NONE"
        )
        self.assertEqual(kind, UNAUTHORIZED_RELATIONSHIP_KIND)

    def test_10_unknown_fact_id_fails(self) -> None:
        text = _sentence(1)
        issues = validate_sentence_declarations(
            _native(text, ["C99"]), _article_plan(claim_ids=("C01", "C02"))
        )
        self.assertTrue(any(item["code"] == UNKNOWN_FACT_ID for item in issues))

    def test_11_supported_single_fact_passes(self) -> None:
        text = _sentence(1)
        kind = classify_multi_fact_relationship(
            text, fact_ids=["C01"], paragraph_relationship="NONE"
        )
        self.assertEqual(kind, AUTHORIZED_RELATIONSHIP)
        issues = validate_sentence_declarations(
            _native(text, ["C01"]), _article_plan(claim_ids=("C01", "C02"))
        )
        self.assertEqual(issues, [])

    def test_12_event_005_authorized_units_retained(self) -> None:
        """Replay authorized multi-fact prose that old rule falsely killed (supplemental path)."""
        article = json.loads((EVENT_005_ATTEMPT / "article.json").read_text(encoding="utf-8"))
        claims_raw = article.get("claims") or []
        claims = tuple(
            LedgerClaim(
                str(row["claim_id"]),
                str(row["text"]),
                str(row.get("claim_type") or "fact"),
                tuple(row.get("evidence_ids") or ("e1",)),
            )
            for row in claims_raw
            if isinstance(row, dict) and row.get("claim_id") and row.get("text")
        )
        self.assertGreaterEqual(len(claims), 2)
        ledgers = EvidenceLedgers(event_id="event-005", claims=claims, quotes=())
        # Authorized enumeration across first three claims (relationship NONE).
        ids = [c.claim_id for c in claims[:3]]
        text = " ".join(c.text for c in claims[:3])
        plan = _article_plan(relationship="NONE", claim_ids=tuple(ids))
        # Old structural rule would reject; new classifier must accept.
        kind = classify_multi_fact_relationship(
            text, fact_ids=ids, paragraph_relationship="NONE"
        )
        self.assertEqual(kind, MULTI_FACT_ENUMERATION)
        parsed = parse_controlled_v3_native(_native(text, ids), plan)
        self.assertTrue(parsed.ok, parsed.error)
        validated = validate_paragraph(
            parsed.paragraphs[0],
            plan.paragraph_plans[0],
            ledgers,
            article_claim_ids=set(ids),
        )
        self.assertGreater(word_count(validated.text), 0)
        self.assertTrue(validated.assemblable)

    def test_13_event_005_unsupported_remains_quarantined(self) -> None:
        article = json.loads((EVENT_005_ATTEMPT / "article.json").read_text(encoding="utf-8"))
        claims = tuple(
            LedgerClaim(
                str(row["claim_id"]),
                str(row["text"]),
                str(row.get("claim_type") or "fact"),
                tuple(row.get("evidence_ids") or ("e1",)),
            )
            for row in (article.get("claims") or [])
            if isinstance(row, dict) and row.get("claim_id") and row.get("text")
        )
        ledgers = EvidenceLedgers(event_id="event-005", claims=claims, quotes=())
        ids = tuple(c.claim_id for c in claims[:2])
        plan = ParagraphPlan(
            paragraph_id="p01",
            editorial_purpose="key_facts",
            allowed_claim_ids=ids,
            allowed_quote_ids=(),
            target_word_range=(20, 120),
            required_claim_ids=ids,
            optional_claim_ids=(),
            relationship="NONE",
        )
        invented = (
            "Regulators will inevitably transform global crypto markets overnight "
            "after years of industry neglect and political theater."
        )
        result = validate_paragraph(
            RenderedParagraph(paragraph_id="p01", text=invented),
            plan,
            ledgers,
            article_claim_ids=set(ids),
        )
        self.assertFalse(result.assemblable)
        self.assertEqual(result.text, "")

    def test_14_qa_350_minimum_unchanged(self) -> None:
        self.assertEqual(NORMAL_ARTICLE_POLICY.hard_minimum_words, 350)

    def test_event_024_native_declaration_now_passes(self) -> None:
        if not EVENT_024_DIAG.exists():
            self.skipTest("event-024 diagnostic missing")
        diag = json.loads(EVENT_024_DIAG.read_text(encoding="utf-8"))
        native = diag.get("native") or {}
        # Reconstruct plan relationships as NONE for multi-fact paras (historical failure mode).
        paras = []
        selected: list[str] = []
        for row in native.get("paragraphs") or []:
            pid = str(row.get("paragraph_id") or "")
            fact_ids: list[str] = []
            for sent in row.get("sentences") or []:
                fact_ids.extend(str(x) for x in (sent.get("fact_ids_used") or []))
            fact_ids = list(dict.fromkeys(fact_ids))
            selected.extend(fact_ids)
            paras.append(
                ParagraphPlan(
                    paragraph_id=pid,
                    editorial_purpose="key_facts",
                    allowed_claim_ids=tuple(fact_ids) or ("C01",),
                    allowed_quote_ids=(),
                    target_word_range=(20, 200),
                    required_claim_ids=tuple(fact_ids) or ("C01",),
                    optional_claim_ids=(),
                    relationship="NONE",
                )
            )
        selected_t = tuple(dict.fromkeys(selected))
        plan = ArticlePlan(
            event_id="event-024",
            headline_claim_ids=(selected_t[0],),
            dek_claim_ids=(selected_t[1] if len(selected_t) > 1 else selected_t[0],),
            paragraph_plans=tuple(paras),
            subheading_plans=(),
            seo_inputs={},
            category="other",
            selected_claim_ids=selected_t,
            selected_quote_ids=(),
            headline_requirements={},
            dek_requirements={},
            planned_safe_words=400,
            minimum_surviving_words=350,
            paragraph_loss_tolerance=0,
        )
        issues = validate_sentence_declarations(native, plan)
        # May still fail on genuine unauthorized cues (e.g. "positioning"); multi-fact alone must not.
        multi_only = [
            item
            for item in issues
            if item.get("code") == UNAUTHORIZED_RELATIONSHIP
        ]
        # Re-check each failing sentence is actually cue-bearing, not mere multi-fact.
        for item in multi_only:
            sent = str(item.get("sentence") or "")
            kind = classify_multi_fact_relationship(
                sent,
                fact_ids=["C01", "C02"],
                paragraph_relationship="NONE",
            )
            self.assertEqual(kind, UNAUTHORIZED_RELATIONSHIP_KIND)
        # Conjunction / enumeration sentences from the diagnostic must pass.
        s01 = native["paragraphs"][0]["sentences"][0]
        if "therefore" not in s01["text"].lower() and "because" not in s01["text"].lower():
            # "positioning" is motive-like; if present, classify may still fail â€” that is correct.
            if "positioning" not in s01["text"].lower() and "aimed" not in s01["text"].lower():
                kind = classify_multi_fact_relationship(
                    s01["text"],
                    fact_ids=list(s01["fact_ids_used"]),
                    paragraph_relationship="NONE",
                )
                self.assertIn(kind, {MULTI_FACT_ENUMERATION, AUTHORIZED_RELATIONSHIP})


if __name__ == "__main__":
    unittest.main()


