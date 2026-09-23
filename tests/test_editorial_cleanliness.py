"""Offline regression tests for editorial cleanliness gate. ZERO provider calls."""

from __future__ import annotations

import unittest

from newsagent_v2.article.qa.editorial_cleanliness import (
    FAILURE_CODE,
    evaluate_editorial_cleanliness,
)
from newsagent_v2.article.writer.v4.sanitize import sanitize_editorial_artifacts
from newsagent_v2.article.writer.controlled.failures import (
    EDITORIAL_CLEANLINESS_FAILED,
    classify_qa_failure,
    recovery_policy,
)


def _article(*, headline: str, dek: str, body: str) -> dict:
    return {
        "event_id": "fixture",
        "headline": headline,
        "dek": dek,
        "article_body": body,
        "slug": "fixture",
        "seo_title": headline,
        "meta_description": dek,
    }


HEADLINE = "Revolut Reports No Direct Contact From Ransom Demanding Group"
DEK = (
    "The fintech said it had not been contacted directly after a public "
    "$3 million ransom demand circulated online."
)


class EditorialCleanlinessGateTests(unittest.TestCase):
    def test_malformed_revolut_structure_fails(self) -> None:
        body = (
            "Revolut reviewed public claims circulating on social channels and said "
            "its customer systems remained available when checked by Cointelegraph "
            "at time of publication.\n\n"
            "Source: Internet Archive Cybersecurity-focused account\n\n"
            "Security researchers archived screenshots of the demand for later review "
            "while the company continued normal operations.\n\n"
            f"{HEADLINE}\n\n"
            f"{DEK}\n\n"
            "Security researchers archived screenshots of the demand for later review "
            "while the company continued normal operations."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertEqual(result["failure_code"], FAILURE_CODE)
        self.assertGreaterEqual(result["critical_count"], 1)
        self.assertTrue(result["artifact_fragments"])
        self.assertTrue(
            result["embedded_headline_matches"] or result["embedded_dek_matches"]
        )
        self.assertTrue(
            result["duplicate_sentences"] or result["duplicate_paragraphs"]
        )

    def test_sanitize_removes_embedded_headline_dek_and_metadata(self) -> None:
        headline = "The moves comes as the SEC and CFTC advance crypto-related rulemaking despite the Clarity"
        dek = "Digital assets advance while regulatory agencies pursue alternative paths after the Clarity Act stalls in the Senate."
        body = (
            "Bitcoin has reclaimed the $80,000 level.\n\n"
            f"{headline}\n\n"
            f"{dek}\n\n"
            "Source: The Block\n\n"
            "Bitcoin has reclaimed the $80,000 level.\n\n"
            "The SEC and CFTC are advancing rulemaking while the Clarity Act stalls."
        )

        article = _article(headline=headline, dek=dek, body=body)
        clean = sanitize_editorial_artifacts(article)

        self.assertNotIn(headline.lower(), clean["article_body"].lower())
        self.assertNotIn(dek.lower(), clean["article_body"].lower())
        self.assertNotIn("source:", clean["article_body"].lower())
        self.assertIn("Bitcoin has reclaimed the $80,000 level.", clean["article_body"])

    def test_source_artifact_detection(self) -> None:
        body = (
            "Investigators preserved public posts related to the incident.\n\n"
            "Source: Internet Archive\n\n"
            "The company said customer access was uninterrupted."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["artifact_fragments"])

    def test_embedded_headline_detection(self) -> None:
        body = (
            "Banks and fintech operators monitored chatter after a public claim appeared.\n\n"
            f"{HEADLINE}\n\n"
            "Compliance teams later said they were reviewing archived materials only."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["embedded_headline_matches"])

    def test_embedded_dek_detection(self) -> None:
        body = (
            "Incident response staff collected public statements during the afternoon.\n\n"
            f"{DEK}\n\n"
            "No outage window was described in the materials reviewed for this report."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["embedded_dek_matches"])

    def test_duplicate_sentence_detection(self) -> None:
        sentence = (
            "Security researchers archived screenshots of the demand for later review "
            "while the company continued normal operations."
        )
        body = (
            f"Initial monitoring focused on public channels only. {sentence} "
            f"Later briefings repeated the same point. {sentence}"
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["duplicate_sentences"])

    def test_duplicate_paragraph_detection(self) -> None:
        para = (
            "Security researchers archived screenshots of the demand for later review "
            "while the company continued normal operations across retail channels."
        )
        body = f"Opening context remained limited to public posts.\n\n{para}\n\n{para}"
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["duplicate_paragraphs"])

    def test_feed_debris_detection(self) -> None:
        body = (
            "The firm published a short status note for customers.\n\n"
            "Subscribe\n\n"
            "Support channels remained open through the evening."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "FAIL")
        self.assertTrue(result["feed_debris"])

    def test_legitimate_attribution_passes(self) -> None:
        body = (
            "Revolut said it had not received a private message from the group. "
            "According to Reuters, similar public claims have appeared before without "
            "confirmed private contact. Cointelegraph reported that customer apps "
            "remained reachable during checks described in the coverage."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS")
        self.assertEqual(result["artifact_fragments"], [])

    def test_repeated_entity_passes(self) -> None:
        body = (
            "Revolut reviewed the public claim. Revolut told reporters that retail "
            "services continued. Partners working with Revolut also said they saw no "
            "separate private outreach tied to the same demand."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS")

    def test_repeated_number_passes(self) -> None:
        body = (
            "The public post referenced a $3 million demand. Later commentary repeated "
            "the $3 million figure without adding a private channel. Compliance notes "
            "also logged the $3 million claim as unverified public chatter."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS")

    def test_natural_headline_concepts_in_prose_pass(self) -> None:
        body = (
            "The fintech told reporters it had no direct contact from the group that "
            "posted a ransom claim. Public materials described a demand and did not "
            "show a private negotiation channel in the documents reviewed."
        )
        result = evaluate_editorial_cleanliness(
            _article(headline=HEADLINE, dek=DEK, body=body)
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS")
        self.assertEqual(result["embedded_headline_matches"], [])

    def test_clean_250_word_article_passes(self) -> None:
        body = (
            "European banking supervisors continued a routine authorization review for the "
            "proposed institutional digital-asset unit. The applicant submitted refreshed "
            "capital schedules and controls evidence covering custody segregation, "
            "escalation paths, and board oversight responsibilities described in the packet. "
            "External counsel memos attached to the file summarized open questions without "
            "asserting a final supervisory outcome.\n\n"
            "Reviewers compared the filing against the stated prudential checklist and "
            "requested clarifications on reporting lines. The materials list interview "
            "dates and document versions but do not describe a completed final decision. "
            "Independent summaries aligned on the absence of an additional enforcement step "
            "in the same window. Staff calendars showed follow-up workshops rather than a "
            "sanction hearing.\n\n"
            "Market intermediaries recorded routine settlement activity while the review "
            "remained open. Liquidity conditions were characterized as orderly in the "
            "observed session notes. Counterparties completed scheduled instructions under "
            "existing agreements without a separate outage notice. Payment rails referenced "
            "in the packet were described as operating inside ordinary maintenance windows.\n\n"
            "Compliance staff archived the disclosures used for this report and limited "
            "commentary to figures and dates already present. Risk notes emphasized "
            "documentation quality rather than speculative causes. The closing section "
            "restated only supported facts about the pending authorization timeline and "
            "the documents still marked as outstanding in the checklist."
        )
        self.assertGreaterEqual(len(body.split()), 200)
        result = evaluate_editorial_cleanliness(
            _article(
                headline="Deutsche Bank Awaits Final Regulatory Approval for Unit",
                dek="Supervisors continued a routine authorization review with no final decision described.",
                body=body,
            )
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS", result)

    def test_clean_500_word_article_passes(self) -> None:
        body = (
            "Spot bitcoin exchange-traded funds recorded a heavy outflow session after "
            "desks reduced risk following a legislative setback in Washington. Issuers "
            "later published routine creation and redemption figures that matched the "
            "direction described by trading desks during the afternoon. Primary-market "
            "agents said authorized participant activity remained available under existing "
            "prospectus mechanics.\n\n"
            "Fund sponsors attributed the move to positioning rather than a custody "
            "incident. Public product pages listed ordinary primary-market activity and "
            "did not describe a settlement failure. Broker notes compared the print with "
            "prior drawdowns and avoided forward-looking claims about the next session. "
            "Secondary-market volume rose while spreads widened only modestly in the "
            "same window according to the venue summaries on file.\n\n"
            "Equity and rates desks separately reported quieter crypto-beta hedging once "
            "the redemption print circulated. That commentary stayed limited to observed "
            "flows and did not assert a single cause beyond the legislative calendar. "
            "Compliance archives retained the issuer notices reviewed for this article. "
            "Internal distribution lists show the notices were shared with risk and "
            "product teams on the same day.\n\n"
            "Market structure observers pointed to wider bid-ask conditions in related "
            "futures during the same window. Those observations were presented as "
            "contemporaneous trading color and were not framed as a formal exchange ruling. "
            "No regulator statement in the packet declared an emergency trading halt. "
            "Clearing summaries listed ordinary variation-margin flows without an "
            "exception code tied to the ETF print.\n\n"
            "Investor-relations summaries from two large sponsors emphasized that creations "
            "and redemptions continued under existing prospectus mechanics. Both summaries "
            "avoided language that would invent unmet redemption demand beyond the printed "
            "totals. Custody providers named in the disclosures were not accused of an "
            "operational breach in the same materials. Service-level exhibits attached to "
            "the packet remained unchanged from the prior month's version.\n\n"
            "By the close, analysts had compiled a short comparison table of prior outflow "
            "days without ranking probability scenarios. The table used only published ETF "
            "flow figures and calendar references already in the source set. Desk wrap-ups "
            "also recorded that options markets priced higher near-term volatility without "
            "citing a separate operational incident. The report ends on those documented "
            "figures and the absence of a separate custody event in the reviewed packet.\n\n"
            "A final operations note from one sponsor restated that basket composition files "
            "were delivered on schedule to authorized participants. That note did not add "
            "a new claim about client withdrawals beyond the published redemption totals. "
            "Together with the flow table, it closes the factual record used for this article."
        )
        self.assertGreaterEqual(len(body.split()), 400)
        result = evaluate_editorial_cleanliness(
            _article(
                headline="Bitcoin ETFs Record Large Outflows After Legislative Setback",
                dek="Funds posted a heavy redemption session as desks reduced risk.",
                body=body,
            )
        )
        self.assertEqual(result["editorial_cleanliness"], "PASS", result)

    def test_failure_class_and_no_auto_regen_policy(self) -> None:
        qa = {
            "critical_failures": [
                {"code": FAILURE_CODE, "severity": "critical", "module": "editorial_cleanliness"}
            ]
        }
        self.assertEqual(classify_qa_failure(qa), EDITORIAL_CLEANLINESS_FAILED)
        policy = recovery_policy(EDITORIAL_CLEANLINESS_FAILED)
        self.assertFalse(policy["retry_same_candidate"])


if __name__ == "__main__":
    unittest.main()


