"""Synthetic article QA fixtures. No live copyrighted article bodies."""

from __future__ import annotations

from copy import deepcopy

from newsagent_v2.article.contract import ARTICLE_OUTPUT_SCHEMA_VERSION
from newsagent_v2.article.input import build_article_input
from newsagent_v2.article.qa.textutil import split_sentences

EVIDENCE_URL = "https://example.com/news/northwind-records-exposed"
EVIDENCE_SUMMARY = (
    "Investigators confirmed that a fraudster using a forged government mail "
    "domain obtained passports selfies and transaction histories for thousands "
    "of Northwind Payments customers during the spoofed-email incident. "
    "Northwind Payments told TestWire that 12,400 customer records were involved. "
    "The company described identity papers and payment history as part of the "
    "exposed set. Staff began notifying affected users and opened an internal "
    "review of how the fake domain request was processed. The disclosure "
    "concerned a retail payments customer file set rather than a named "
    "third-party vendor platform."
)
ABSENCE_EVIDENCE_URL = "https://example.com/news/northwind-absence-note"
CONTEXT_EVIDENCE_URL = "https://example.com/news/northwind-phishing-trend"

CANDIDATE = {
    "event_id": "event-syn-001",
    "deterministic_rank": 1,
    "event_score": 5.0,
    "representative_score": 5.0,
    "corroboration": 0.0,
    "source_count": 1,
    "sources": ["TestWire"],
    "representative_title": (
        "Northwind Payments says 12,400 customer records exposed in email spoof"
    ),
    "evidence": [
        {
            "source": "TestWire",
            "source_type": "newsroom",
            "source_role": "discovery",
            "source_authority": 0.8,
            "title": (
                "Northwind Payments says 12,400 customer records exposed in email spoof"
            ),
            "url": EVIDENCE_URL,
            "published": "Mon, 14 Sep 2026 12:00:00 +0000",
            "summary": EVIDENCE_SUMMARY,
        }
    ],
}

THIN_CANDIDATE = {
    "event_id": "event-syn-thin",
    "deterministic_rank": 2,
    "event_score": 4.0,
    "representative_score": 4.0,
    "corroboration": 0.0,
    "source_count": 1,
    "sources": ["TestWire"],
    "representative_title": "Northwind Payments notes a customer-file incident",
    "evidence": [
        {
            "source": "TestWire",
            "source_type": "newsroom",
            "source_role": "discovery",
            "source_authority": 0.8,
            "title": "Northwind Payments notes a customer-file incident",
            "url": "https://example.com/news/thin-note",
            "published": "Mon, 14 Sep 2026 12:00:00 +0000",
            "summary": "Northwind Payments reported a customer-file incident.",
        }
    ],
}

JUDGMENT = {
    "event_id": "event-syn-001",
    "selected": True,
    "same_event_as": [],
    "is_current_event": True,
    "is_background_context": False,
    "semantic_category": "security_incident",
    "speculation": {"is_speculative": False, "flags": ["none"]},
    "newsworthiness_reasoning": "Concrete customer data exposure.",
    "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
    "confidence": 0.9,
    "rejection_reason": None,
}

CLEAN_BODY = (
    "Northwind Payments reported that 12,400 customer records were exposed. "
    "The firm said 12,400 records were involved, including identity documents "
    "and payment history. A spoofed government-domain email reached company "
    "staff and was treated as the entry point for the incident. "
    "Passports, selfies, and transaction histories were obtained from customer "
    "files in the retail payments set. Thousands of Northwind Payments "
    "customers were named as the affected group in the investigator recap. "
    "Security staff are notifying affected users after the public disclosure. "
    "The company is reviewing how the fake domain request was processed by "
    "internal mail handlers. The incident was framed as an email spoof rather "
    "than a break in the core ledger. TestWire carried the Northwind Payments "
    "customer-data disclosure in its Monday report. Identity papers and "
    "payment history were listed among the exposed record types. "
    "The payments firm said a lookalike government mailbox was used to request "
    "the files. Retail payments customer records were the dataset described in "
    "the disclosure. Internal reviewers opened a check of the forged-domain "
    "message path. Notified users include customers whose passports or selfies "
    "appeared in the obtained set. The 12,400 figure is the record count "
    "Northwind Payments gave TestWire. Transaction histories were grouped with "
    "identity documents in that account. The spoofed-email incident is the "
    "event Northwind Payments described to reporters. Customer files from the "
    "retail product line were the records discussed. Staff notification is "
    "underway according to the company recap. The fake domain request is under "
    "internal review. Northwind Payments is the firm named in the TestWire "
    "account. Government-domain impersonation is the method described for "
    "obtaining the files. Selfies were included with passports in the obtained "
    "material. Payment history was included with identity documents. "
    "The Monday TestWire report is the source recap used here. "
    "The recap described an email spoof path for obtaining the files. "
    "The disclosure stays limited to the customer-file incident described above. "
    "Northwind Payments gave the 12,400 record count to TestWire in that recap. "
    "Forged government mail handling is the pathway described for the request. "
    "Affected users are being notified by security staff. "
    "Internal mail handlers are included in the review of the fake domain request. "
    "The retail payments file set is the customer dataset named in the account. "
    "Passports remain among the obtained identity materials. "
    "Selfies remain among the obtained identity materials. "
    "Transaction histories remain among the obtained financial materials. "
    "The investigator recap remains the source for the thousands-of-customers wording. "
    "Northwind Payments remains the company that described the spoofed-email incident. "
    "TestWire remains the outlet that carried the Monday customer-data disclosure. "
    "The fake domain request remains the mail-handling issue under review. "
    "Identity documents remain grouped with payment history in the company account. "
    "The 12,400 customer records remain the count stated by Northwind Payments. "
    "Government-domain impersonation remains the method given for obtaining files."
)


def article_input():
    return build_article_input(CANDIDATE, JUDGMENT)


def thin_article_input():
    judgment = dict(JUDGMENT)
    judgment["event_id"] = "event-syn-thin"
    return build_article_input(THIN_CANDIDATE, judgment)


def _ref():
    return {"url": EVIDENCE_URL, "source": "TestWire"}


def _claim(claim_id: str, text: str, claim_type: str = "fact") -> dict:
    return {
        "claim_id": claim_id,
        "text": text,
        "claim_type": claim_type,
        "evidence_refs": [_ref()],
    }


def _base_article() -> dict:
    return {
        "schema_version": ARTICLE_OUTPUT_SCHEMA_VERSION,
        "event_id": "event-syn-001",
        "headline": "Northwind says 12,400 customer records were exposed",
        "dek": "A spoofed government email was used to obtain customer files.",
        "article_body": CLEAN_BODY,
        "category": "security_incident",
        "seo_title": "Northwind customer records exposed in email spoof",
        "meta_description": (
            "Northwind Payments said 12,400 customer records were exposed after "
            "a spoofed government-domain email request."
        ),
        "slug": "northwind-customer-records-exposed",
        "entities": [
            {"name": "Northwind Payments", "type": "org"},
        ],
        "keywords": ["northwind", "data exposure"],
        "evidence_used": [{"url": EVIDENCE_URL, "source": "TestWire"}],
        "claims": [
            _claim(
                f"c{i}",
                sentence,
                "number" if "12,400" in sentence else "fact",
            )
            for i, sentence in enumerate(split_sentences(CLEAN_BODY), start=1)
        ],
        "quotes": [
            {
                "text": "passports selfies and transaction histories",
                "kind": "paraphrase",
                "attribution": "TestWire summary of Northwind's account",
                "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
            }
        ],
        "generation_notes": "synthetic fixture",
    }


def clean_article() -> dict:
    return deepcopy(_base_article())


def unsupported_numeric_headline() -> dict:
    article = _base_article()
    article["headline"] = "Northwind says 999 billion accounts were wiped out"
    return article


def claim_without_evidence() -> dict:
    article = _base_article()
    article["claims"][0]["evidence_refs"] = []
    return article


def invented_evidence_ref() -> dict:
    article = _base_article()
    article["claims"][0]["evidence_refs"] = [
        {"url": "https://invented.example/not-in-pack", "source": "Ghost"}
    ]
    return article


def copied_source_paragraph() -> dict:
    article = _base_article()
    article["article_body"] = (
        EVIDENCE_SUMMARY
        + "\n\n"
        + EVIDENCE_SUMMARY
        + " Additional local recap text follows for length without new facts."
    )
    return article


def repeated_paragraph() -> dict:
    article = _base_article()
    block = (
        "Security staff are notifying affected users and reviewing how the fake "
        "domain request was processed without delay today."
    )
    article["article_body"] = block + "\n\n" + block
    return article


def malformed_punctuation() -> dict:
    article = _base_article()
    article["article_body"] = CLEAN_BODY.replace(
        "described above.",
        "described above???",
    )
    return article


def all_caps_headline() -> dict:
    article = _base_article()
    article["headline"] = "NORTHWIND CONFIRMS HUGE CUSTOMER DATA BREACH"
    return article


def direct_quote_without_attribution() -> dict:
    article = _base_article()
    article["quotes"] = [
        {
            "text": "12,400 customer records exposed",
            "kind": "direct",
            "attribution": "",
            "evidence_refs": [{"url": EVIDENCE_URL, "source": "TestWire"}],
        }
    ]
    return article


def bad_slug() -> dict:
    article = _base_article()
    article["slug"] = "Northwind Data!!"
    return article


def windows_path_leak() -> dict:
    article = _base_article()
    article["article_body"] = CLEAN_BODY + "\n\nSee C:\\NewsAgent-V2\\notes.txt for draft."
    return article


def localhost_leak() -> dict:
    article = _base_article()
    article["article_body"] = CLEAN_BODY + "\n\nPreview at http://localhost:8080/draft."
    return article


def clean_paraphrase_with_entity_overlap() -> dict:
    article = _base_article()
    article["headline"] = "Northwind reports 12,400 records in spoofed-email case"
    return article


def attach_article_sections(article: dict, *, section_count: int = 6) -> dict:
    """Group existing body sentences into claim-bound sections (tests/synthetics)."""
    from copy import deepcopy

    from newsagent_v2.article.qa.textutil import split_sentences
    from newsagent_v2.article.render import materialize_article

    article = deepcopy(article)
    sentences = split_sentences(article.get("article_body") or "")
    claims = article.get("claims") if isinstance(article.get("claims"), list) else []
    if not sentences:
        article["article_sections"] = []
        return article
    n = max(5, min(8, int(section_count)))
    size = max(1, (len(sentences) + n - 1) // n)
    purposes = [
        "lead",
        "key_facts",
        "attribution",
        "detail",
        "supported_context",
        "additional_facts",
        "more_detail",
        "last_supported_fact",
    ]
    sections = []
    offset = 0
    for index in range(n):
        chunk = sentences[offset : offset + size]
        if not chunk:
            break
        claim_ids = []
        for pos, sentence in enumerate(chunk):
            claim_index = offset + pos
            if claim_index < len(claims) and isinstance(claims[claim_index], dict):
                claim_ids.append(str(claims[claim_index].get("claim_id") or f"c{claim_index + 1}"))
            else:
                claim_ids.append(str(claims[-1]["claim_id"]) if claims else "c1")
        sections.append(
            {
                "section_id": f"s{index + 1}",
                "purpose": purposes[index],
                "paragraphs": [{"text": " ".join(chunk), "claim_ids": claim_ids}],
            }
        )
        offset += size
    article["article_sections"] = sections
    materialize_article(article)
    return article


def structured_regulatory_article() -> dict:
    article = attach_article_sections(clean_article())
    article["category"] = "regulatory"
    article["headline"] = "Northwind reports 12,400-record exposure after spoofed government email"
    article["slug"] = "northwind-reports-12400-record-exposure"
    article["seo_title"] = "Northwind reports 12,400-record email-spoof exposure"
    return article


def structured_exploit_article() -> dict:
    article = attach_article_sections(clean_article())
    article["category"] = "exploit_hack"
    article["headline"] = "Spoofed government mailbox used to obtain Northwind customer files"
    article["slug"] = "spoofed-mailbox-northwind-customer-files"
    article["seo_title"] = "Spoofed mailbox used in Northwind customer-file incident"
    return article


def _append_claimed_sentence(
    article: dict,
    sentence: str,
    *,
    claim_type: str = "fact",
    url: str = EVIDENCE_URL,
    source: str = "TestWire",
) -> dict:
    article["article_body"] = article["article_body"].rstrip() + " " + sentence
    article["claims"].append(
        {
            "claim_id": f"extra-{len(article['claims']) + 1}",
            "text": sentence,
            "claim_type": claim_type,
            "evidence_refs": [{"url": url, "source": source}],
        }
    )
    return article


def pack_with_extra_evidence(url: str, title: str, summary: str) -> dict:
    pack = article_input()
    pack["evidence"] = list(pack["evidence"])
    pack["evidence"].append(
        {
            "source": "TestWire",
            "source_type": "newsroom",
            "source_role": "discovery",
            "source_authority": 0.8,
            "title": title,
            "url": url,
            "published": "Mon, 14 Sep 2026 13:00:00 +0000",
            "summary": summary,
        }
    )
    return pack


def short_unclaimed_body_article(body: str) -> dict:
    article = _base_article()
    article["article_body"] = body
    return article


LONG_META = (
    "Northwind Payments disclosed that a fake government email led to the "
    "exposure of passports, selfies and transaction histories, with extra "
    "commentary that high-net-worth users may have been targeted in this case."
)


FILLER_CONTEXT_BODY = (
    "The episode underscores ongoing security challenges for fintech platforms "
    "across several markets this year. It highlights growing worries in the "
    "broader industry after similar headlines. The case reflects broader "
    "weakness in customer onboarding. It comes amid increasing pressure on "
    "payments brands worldwide. Analysts say the moment raises questions about "
    "market-wide defenses. Observers mention growing security concerns without "
    "naming a new Northwind fact. Commentators argue it remains to be seen how "
    "boards will respond. Industry newsletters repeat that this underscores "
    "trust issues for fintech platforms. Another note highlights growing costs "
    "for compliance teams. A third item reflects broader vendor fatigue. "
    "A fourth line comes amid increasing conference talk. A fifth line raises "
    "questions for auditors. A sixth line cites growing security concerns in "
    "generic terms. A seventh line says it remains to be seen after the news. "
    "An eighth line returns to ongoing security challenges for fintech platforms. "
    "A ninth line repeats broader industry anxiety without evidence. "
    "A tenth line keeps the market-wide conclusion in play. "
    "An eleventh line restates that the episode underscores ongoing security "
    "challenges for fintech platforms. A twelfth line highlights growing "
    "commentary. A thirteenth line reflects broader narrative padding. "
    "A fourteenth line comes amid increasing filler. A fifteenth line raises "
    "questions again. A sixteenth line cites growing security concerns. "
    "A seventeenth line says it remains to be seen. An eighteenth line mentions "
    "ongoing security challenges for fintech platforms one more time. "
    "A nineteenth line stays with the broader industry theme. A twentieth line "
    "closes with market-wide language and no new sourced fact from the pack. "
    "A twenty-first line again underscores ongoing security challenges for fintech platforms. "
    "A twenty-second line highlights growing commentary from unnamed roundtables. "
    "A twenty-third line reflects broader vendor narratives without a sourced figure. "
    "A twenty-fourth line comes amid increasing conference chatter this season. "
    "A twenty-fifth line raises questions for unnamed working groups. "
    "A twenty-sixth line cites growing security concerns in generic briefings. "
    "A twenty-seventh line says it remains to be seen after the roundtables. "
    "A twenty-eighth line repeats ongoing security challenges for fintech platforms. "
    "A twenty-ninth line keeps the broader industry theme in the closing graph. "
    "A thirtieth line ends on a market-wide conclusion still unsupported by the pack."
)

SOURCE_PROSE_SENTENCE = (
    "Investigators confirmed that a fraudster using a forged government mail "
    "domain obtained passports selfies and transaction histories for thousands "
    "of Northwind Payments customers during the spoofed-email incident."
)

DIRECT_QUOTE_FROM_EVIDENCE = (
    "Northwind Payments told TestWire that 12,400 customer records were involved."
)

CLOSE_PARAPHRASE_SENTENCE = (
    "Investigators confirmed that a fraudster using a forged government mail "
    "domain obtained passports, selfies, and transaction histories for thousands "
    "of Northwind Payments customers during the spoofed-email incident."
)


def article_with_attributed_direct_quote() -> dict:
    article = _base_article()
    quoted = f'A company briefing stated, "{DIRECT_QUOTE_FROM_EVIDENCE}"'
    article = _append_claimed_sentence(article, quoted, claim_type="quote")
    article["quotes"] = [
        {
            "text": DIRECT_QUOTE_FROM_EVIDENCE,
            "kind": "direct",
            "attribution": "Northwind Payments, via TestWire",
            "evidence_refs": [_ref()],
        }
    ]
    return article


def article_with_copied_source_sentence() -> dict:
    article = _base_article()
    return _append_claimed_sentence(article, SOURCE_PROSE_SENTENCE)


def article_with_close_source_paraphrase() -> dict:
    article = _base_article()
    return _append_claimed_sentence(article, CLOSE_PARAPHRASE_SENTENCE)



