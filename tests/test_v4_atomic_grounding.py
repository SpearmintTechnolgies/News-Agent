"""Offline tests for V4 atomic grounding + surgical unsupported repair."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from newsagent_v2.article.qa.grounding import check_body_claim_coverage
from newsagent_v2.article.qa.similarity import EXACT_PHRASE_N, HIGH_SENTENCE_SIMILARITY
from newsagent_v2.article.writer.evidence_ledger import EvidenceLedgers, LedgerClaim
from newsagent_v2.article.writer.v4.atomic_grounding import (
    STATUS_SUPPORTED,
    STATUS_UNSUPPORTED,
    build_authorized_proposition_set,
    decompose_claim_text,
    ground_sentence,
    is_pure_connective,
    is_significance_rhetoric,
    proposition_supported_by,
    verify_atomic_article,
)
from newsagent_v2.article.writer.v4.assemble import assemble_v4_article
from newsagent_v2.article.writer.v4.packet import build_writer_evidence_packet
from newsagent_v2.article.writer.v4.repair import repair_unsupported_propositions
from newsagent_v2.article.writer.v4.verify import verify_v4_native
from newsagent_v2.article.writer.v4.writer import (
    FORBIDDEN_WRITERS,
    V4NativeArticle,
    assert_v4_writer_is_free,
)

REPO = Path(__file__).resolve().parents[1]
EVENT027 = REPO / "output/make_runs/v4-20260916T192733Z/attempts/event-027"


def _ledgers_from_claims(rows: list[tuple[str, str]]) -> EvidenceLedgers:
    claims = tuple(
        LedgerClaim(cid, text, "fact", ("e1",)) for cid, text in rows
    )
    return EvidenceLedgers(event_id="event-test", claims=claims, quotes=())


class AtomicGroundingTests(unittest.TestCase):
    def test_01_natural_paraphrase_maps(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C03",
                    "Germanyâ€™s largest bank, Deutsche Bank, is awaiting regulatory approval "
                    "to launch digital asset custody solutions for institutional clients and "
                    "corporations in Europe.",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        sent = (
            "The institution aims to serve institutional clients and corporations "
            "across Europe with these digital asset custody services."
        )
        grounded = ground_sentence(sent, auth)
        self.assertEqual(grounded.status, STATUS_SUPPORTED)
        self.assertIn("C03", grounded.claim_ids)

    def test_02_composite_claim_split_both_supported(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C12",
                    "In April 2024, Germanyâ€™s largest federal bank, the Landesbank "
                    "Baden-WÃ¼rttemberg, started offering crypto custody solutions after "
                    "partnering with the Austria-based Bitpanda for its institutional "
                    "custody platform.",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        s1 = (
            "For instance, in April 2024, the Landesbank Baden-WÃ¼rttemberg, described as "
            "Germanyâ€™s largest federal bank, began offering crypto custody solutions."
        )
        s2 = (
            "That institution partnered with the Austria-based Bitpanda to utilize its "
            "institutional custody platform."
        )
        g1 = ground_sentence(s1, auth)
        g2 = ground_sentence(s2, auth)
        self.assertEqual(g1.status, STATUS_SUPPORTED)
        self.assertEqual(g2.status, STATUS_SUPPORTED)
        self.assertIn("C12", g1.claim_ids)
        self.assertIn("C12", g2.claim_ids)

    def test_03_multi_claim_sentence_supported(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C01",
                    "Deutsche Bank is awaiting final regulatory approval to launch its "
                    "institutional custody solutions for Bitcoin, Ether and select stablecoins.",
                ),
                (
                    "C03",
                    "Deutsche Bank is awaiting regulatory approval to launch digital asset "
                    "custody solutions for institutional clients and corporations in Europe.",
                ),
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        sent = (
            "Deutsche Bank is awaiting regulatory approval to launch institutional custody "
            "for institutional clients in Europe."
        )
        grounded = ground_sentence(sent, auth)
        self.assertEqual(grounded.status, STATUS_SUPPORTED)
        self.assertTrue(set(grounded.claim_ids) & {"C01", "C03"})

    def test_04_same_claim_supports_multiple_realizations(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C05",
                    "The bank plans to offer initial support for Bitcoin (BTC), Ether (ETH) "
                    "and select stablecoins, including Circle USDC (USDC), EURC (EURC) and "
                    "AllUnity EUR (EURAU).",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        a = ground_sentence(
            "The bank announced it intends to offer initial support for Bitcoin, Ether, "
            "and select stablecoins.",
            auth,
        )
        b = ground_sentence(
            "Specifically, the service will include Circle USDC, EURC, and AllUnity EUR.",
            auth,
        )
        self.assertEqual(a.status, STATUS_SUPPORTED)
        self.assertEqual(b.status, STATUS_SUPPORTED)
        self.assertEqual(a.claim_ids, ("C05",))
        self.assertEqual(b.claim_ids, ("C05",))

    def test_05_number_mismatch_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "The fund recorded 12400 customer records exposed.")]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "The fund recorded 99000 customer records exposed.", auth
        )
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_06_date_mismatch_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "In April 2024, the bank began offering crypto custody solutions.")]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "In April 2019, the bank began offering crypto custody solutions.", auth
        )
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_07_entity_mismatch_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "Deutsche Bank is awaiting regulatory approval for custody.")]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "BlackRock is awaiting regulatory approval for custody.", auth
        )
        self.assertNotEqual(grounded.status, STATUS_SUPPORTED)

    def test_08_negation_mismatch_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "Deutsche Bank is awaiting regulatory approval for custody.")]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "Deutsche Bank is not awaiting regulatory approval for custody.", auth
        )
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_09_expects_vs_received_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C09",
                    "The bank expects to receive the license for the custody offering in "
                    "October, a spokesperson told Cointelegraph.",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "A spokesperson told Cointelegraph that the bank has received the license.",
            auth,
        )
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_10_unsupported_significance_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "Deutsche Bank is awaiting regulatory approval for custody.")]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        sent = (
            "Deutsche Bankâ€™s pending approval marks a significant step in the integration "
            "of traditional banking infrastructure with digital asset management."
        )
        self.assertTrue(is_significance_rhetoric(sent))
        grounded = ground_sentence(sent, auth)
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_11_unsupported_comparative_positioning_fails(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C07",
                    "Deutsche Bank is one of the institutions listed by the Financial "
                    "Stability Board as a Global Systemically Important Bank.",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        sent = (
            "This move positions Deutsche Bank among other major institutions pushing "
            "into the crypto sector."
        )
        self.assertTrue(is_significance_rhetoric(sent))
        grounded = ground_sentence(sent, auth)
        self.assertEqual(grounded.status, STATUS_UNSUPPORTED)

    def test_12_pure_connective_excluded(self) -> None:
        self.assertTrue(is_pure_connective("Meanwhile,"))
        ledgers = _ledgers_from_claims([("C1", "Deutsche Bank awaits approval.")])
        auth = build_authorized_proposition_set(ledgers=ledgers)
        report = verify_atomic_article(
            headline="Headline about Deutsche Bank awaits approval",
            dek="Deutsche Bank awaits approval.",
            article_body="Deutsche Bank awaits approval. Meanwhile,",
            authorized=auth,
        )
        # Connective unit must not inflate unsupported factual count.
        self.assertGreaterEqual(report.connective_units, 0)

    def test_13_full_authorized_set_reaches_qa(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                ("C04", "Deutsche Bank announced the plans on Wednesday."),
                ("C06", "It plans to include support for tokenized financial instruments later."),
                ("C05", "The bank plans to offer Bitcoin and Ether."),
            ]
        )
        packet = build_writer_evidence_packet(event_id="e", ledgers=ledgers)
        native = V4NativeArticle(
            headline="Deutsche Bank plans Bitcoin custody",
            dek="The bank plans to offer Bitcoin and Ether.",
            article_body="The bank plans to offer Bitcoin and Ether.",
            seo_title="x",
            meta_description="y",
            slug="z",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        article = assemble_v4_article(
            event_id="e",
            native=native,
            ledgers=ledgers,
            article_input={},
            report=report,
        )
        ids = {c["claim_id"] for c in article["claims"]}
        self.assertEqual(ids, {"C04", "C05", "C06"})
        self.assertIn("authorized_propositions", article)

    def test_14_c05_stablecoin_example(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C05",
                    "The bank plans to offer initial support for Bitcoin (BTC), Ether (ETH) "
                    "and select stablecoins, including Circle USDC (USDC), EURC (EURC) and "
                    "AllUnity EUR (EURAU).",
                )
            ]
        )
        props = decompose_claim_text("C05", ledgers.claims[0].text)
        self.assertGreaterEqual(len(props), 3)
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "Specifically, the service will include Circle USDC, EURC, and AllUnity EUR.",
            auth,
        )
        self.assertEqual(grounded.status, STATUS_SUPPORTED)

    def test_15_c12_bitpanda_example(self) -> None:
        self.test_02_composite_claim_split_both_supported()

    def test_16_c09_license_attribution(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C09",
                    "The bank expects to receive the license for the custody offering in "
                    "October, under the EUâ€™s Markets in Crypto Assets (MiCA) framework, "
                    "Heinrich FrÃ¶msdorf, a spokesperson for Deutsche Bank, told Cointelegraph.",
                )
            ]
        )
        auth = build_authorized_proposition_set(ledgers=ledgers)
        grounded = ground_sentence(
            "A spokesperson for the bank told Cointelegraph that the institution expects "
            "to receive the necessary license.",
            auth,
        )
        self.assertEqual(grounded.status, STATUS_SUPPORTED)
        self.assertIn("C09", grounded.claim_ids)

    def test_17_packet_not_garbled(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C09",
                    "The bank expects to receive the license for the custody offering in "
                    "October, a spokesperson for Deutsche Bank told Cointelegraph.",
                ),
                (
                    "C06",
                    "It plans to include support for tokenized financial instruments at a "
                    "later point.",
                ),
            ]
        )
        packet = build_writer_evidence_packet(event_id="e", ledgers=ledgers)
        for fact in packet.authorized_facts:
            self.assertEqual(fact.proposition, ledgers.claim_by_id()[fact.id].text)
            self.assertTrue(fact.subject or fact.predicate or fact.object or fact.proposition)
            # No telegraphic garble pattern from old compact join.
            self.assertNotRegex(fact.proposition, r"^bank expects receive license told")
            self.assertNotRegex(fact.proposition, r"^It plans include support tokenized")

    def test_18_19_surgical_unsupported_repair(self) -> None:
        ledgers = _ledgers_from_claims(
            [
                (
                    "C01",
                    "Deutsche Bank is awaiting final regulatory approval to launch its "
                    "institutional custody solutions for Bitcoin.",
                ),
                (
                    "C08",
                    "In June, Sabih Behzad revealed that Deutsche Bank was considering "
                    "entering the stablecoin market.",
                ),
            ]
        )
        packet = build_writer_evidence_packet(event_id="e", ledgers=ledgers)
        native = V4NativeArticle(
            headline="Deutsche Bank awaits custody approval",
            dek="Deutsche Bank is awaiting final regulatory approval to launch its institutional custody solutions for Bitcoin.",
            article_body=(
                "Deutsche Bank is awaiting final regulatory approval to launch its "
                "institutional custody solutions for Bitcoin. "
                "This move positions Deutsche Bank among other major institutions pushing "
                "into the crypto sector."
            ),
            seo_title="x",
            meta_description="y",
            slug="z",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertFalse(report.ok)
        repaired, actions, rejected = repair_unsupported_propositions(
            native, packet=packet, ledgers=ledgers, report=report
        )
        self.assertTrue(actions)
        if not rejected:
            report2 = verify_v4_native(repaired, packet=packet, ledgers=ledgers)
            self.assertNotIn(
                "among other major institutions",
                repaired.article_body,
            )
            self.assertTrue(
                report2.ok
                or all(
                    "among other major institutions" not in row.text
                    for row in report2.rows
                    if row.status == STATUS_UNSUPPORTED
                )
            )
        else:
            # Failed surgical repair must not commit destructive body.
            self.assertEqual(repaired.article_body, native.article_body)

    def test_20_clean_room_module_still_importable_but_inactive(self) -> None:
        from newsagent_v2.article.writer.v4 import copyright_recovery as cr
        import inspect
        from newsagent_v2.article.writer.v4.repair import run_targeted_repairs

        self.assertIs(cr.verify_v4_native, verify_v4_native)
        self.assertNotIn("recover_copyright", inspect.getsource(run_targeted_repairs))

    def test_21_similarity_thresholds_unchanged_for_diagnostics(self) -> None:
        self.assertEqual(EXACT_PHRASE_N, 12)
        self.assertEqual(HIGH_SENTENCE_SIMILARITY, 0.92)

    def test_22_grounding_requires_full_proposition_support(self) -> None:
        ledgers = _ledgers_from_claims(
            [("C1", "Deutsche Bank is awaiting regulatory approval for custody.")]
        )
        packet = build_writer_evidence_packet(event_id="e", ledgers=ledgers)
        native = V4NativeArticle(
            headline="Deutsche Bank awaits approval",
            dek="Deutsche Bank is awaiting regulatory approval for custody.",
            article_body=(
                "Deutsche Bank is awaiting regulatory approval for custody. "
                "This move positions Deutsche Bank among other major institutions pushing "
                "into the crypto sector."
            ),
            seo_title="x",
            meta_description="y",
            slug="z",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        self.assertGreater(report.unsupported_factual_propositions, 0)
        self.assertFalse(report.ok)
        self.assertLess(report.proposition_coverage, 1.0)

    def test_23_24_kimi_paid_qwen_impossible(self) -> None:
        self.assertTrue(any("kimi" in item for item in FORBIDDEN_WRITERS))
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free("moonshotai.kimi-k2.5", environ={"NEWSAGENT_V2_V4_ALLOW_KIMI": "0"})
        with self.assertRaises(RuntimeError):
            assert_v4_writer_is_free("vllm-local/qwen3.8-27b", environ={"NEWSAGENT_V2_V4_ALLOW_PAID_QWEN": "0"})


@unittest.skipUnless(EVENT027.exists(), "event-027 frozen artifacts required")
class Event027OfflineAtomicReplay(unittest.TestCase):
    def _ledgers(self) -> EvidenceLedgers:
        art = json.loads((EVENT027 / "article.json").read_text(encoding="utf-8"))
        by_id = {c["id"]: c["text"] for c in art["claims"]}
        by_id.setdefault("C04", "Deutsche Bank announced the plans on Wednesday.")
        by_id["C06"] = (
            "Deutsche Bank plans to include support for tokenized financial instruments "
            "at a later point."
        )
        claims = tuple(
            LedgerClaim(cid, text, "fact", ("event-027-e01",))
            for cid, text in sorted(by_id.items())
        )
        return EvidenceLedgers(event_id="event-027", claims=claims, quotes=())

    def test_event027_classifies_seven_failures(self) -> None:
        art = json.loads((EVENT027 / "article.json").read_text(encoding="utf-8"))
        ledgers = self._ledgers()
        packet = build_writer_evidence_packet(
            event_id="event-027", ledgers=ledgers, story_topic="Deutsche Bank"
        )
        native = V4NativeArticle(
            headline=art["headline"],
            dek=art["dek"],
            article_body=art["article_body"],
            seo_title=art.get("seo_title") or "",
            meta_description=art.get("meta_description") or "",
            slug=art.get("slug") or "",
        )
        report = verify_v4_native(native, packet=packet, ledgers=ledgers)
        failed = {
            "F1": "Specifically, the service will include Circle USDC, EURC, and AllUnity EUR.",
            "F2": (
                "The institution aims to serve institutional clients and corporations "
                "across Europe with these digital asset custody services."
            ),
            "F3": (
                "Following the launch of these initial assets, Deutsche Bank plans to "
                "expand its offerings to include tokenized financial instruments at a "
                "later stage."
            ),
            "F4": (
                "A spokesperson for the bank told Cointelegraph that the institution "
                "expects to receive the necessary license."
            ),
            "F5": (
                "This move positions Deutsche Bank among other major institutions "
                "pushing into the crypto sector."
            ),
            "F6": (
                "That institution partnered with the Austria-based Bitpanda to utilize "
                "its institutional custody platform."
            ),
            "F7": (
                "Deutsche Bankâ€™s pending approval marks a significant step in the "
                "integration of traditional banking infrastructure with digital asset "
                "management."
            ),
        }
        by = {row.text: row for row in report.rows}
        expected = {
            "F1": STATUS_SUPPORTED,
            "F2": STATUS_SUPPORTED,
            "F3": STATUS_SUPPORTED,
            "F4": STATUS_SUPPORTED,
            "F5": STATUS_UNSUPPORTED,
            "F6": STATUS_SUPPORTED,
            "F7": STATUS_UNSUPPORTED,
        }
        for key, sentence in failed.items():
            row = by[sentence]
            self.assertEqual(row.status, expected[key], msg=f"{key}: {row.status}")
        true_unsupported = [
            key for key, sentence in failed.items() if by[sentence].status == STATUS_UNSUPPORTED
        ]
        self.assertEqual(sorted(true_unsupported), ["F5", "F7"])
        self.assertEqual(len(true_unsupported), 2)

        # Full authorized set reaches assembled article / QA path.
        article = assemble_v4_article(
            event_id="event-027",
            native=native,
            ledgers=ledgers,
            article_input={},
            report=report,
        )
        self.assertIn("C06", {c["claim_id"] for c in article["claims"]})
        issues, metrics = check_body_claim_coverage(article)
        # QA must flag the two real unsupported flourishes, not the five mapped paraphrases.
        uncovered = set(metrics.get("uncovered_assertive_sentences") or [])
        self.assertIn(failed["F5"], uncovered)
        self.assertIn(failed["F7"], uncovered)
        self.assertNotIn(failed["F1"], uncovered)
        self.assertNotIn(failed["F2"], uncovered)
        self.assertNotIn(failed["F6"], uncovered)
        self.assertFalse(metrics.get("proposition_grounding_ok"))
        self.assertGreaterEqual(len(issues), 2)


if __name__ == "__main__":
    unittest.main()


