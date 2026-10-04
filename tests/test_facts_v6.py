from __future__ import annotations

from newsagent_v2.facts import GatePolicy, assess_evidence, build_fact_bank
from newsagent_v2.research.dossier import ResearchDossier, SourceDoc


def _doc(host: str, paragraphs: list[str], kind: str = "news") -> SourceDoc:
    return SourceDoc(url=f"https://{host}/story", publisher=host, paragraphs=paragraphs, kind=kind, relevance=0.8)


BASE = [
    "The Securities and Exchange Commission approved the first triple-leveraged bitcoin fund on Thursday, a filing showed.",
    "Volatility Shares said the fund would charge an annual fee of 1.85 percent and begin trading next week.",
    "“This is an important milestone for the crypto market,” said Justin Young, chief executive of Volatility Shares.",
    "“We expect strong demand from active traders,” he said.",
]


def _dossier(docs: list[SourceDoc]) -> ResearchDossier:
    return ResearchDossier(
        event_id="evt-t",
        title="SEC approves triple-leveraged bitcoin fund from Volatility Shares",
        entities=["SEC", "Volatility Shares"],
        sources=docs,
    )


def _padded(paragraphs: list[str]) -> list[str]:
    filler = " ".join(["Market participants tracked the decision closely across several trading desks."] * 20)
    return paragraphs + [filler]


def test_non_facts_are_dropped():
    doc = _doc(
        "a.example",
        _padded(
            BASE
            + [
                "If you hold leveraged funds, your losses can compound quickly over several sessions.",
                "Our earlier coverage of leveraged products explained how daily resets work in practice.",
                "Will the fund attract the same inflows as spot bitcoin products this quarter?",
                "Understand the daily arithmetic before buying any leveraged product in a volatile market.",
            ]
        ),
    )
    bank = build_fact_bank(_dossier([doc]))
    texts = " ".join(f.text for f in bank.facts)
    assert "your losses" not in texts
    assert "Our earlier coverage" not in texts
    assert "Will the fund" not in texts
    assert "Understand the daily" not in texts
    assert set(bank.dropped) >= {"reader_address", "publisher_voice", "rhetorical_question", "imperative"}


def test_us_country_is_not_publisher_voice():
    doc = _doc("a.example", _padded(BASE + ["The US Treasury said it would review the fund structure before the end of the year."]))
    bank = build_fact_bank(_dossier([doc]))
    assert any("US Treasury" in f.text for f in bank.facts)


def test_duplicates_merge_with_corroboration_and_quotes_keep_speaker():
    a = _doc("a.example", _padded(BASE))
    b = _doc(
        "b.example",
        _padded(["The Securities and Exchange Commission approved the first triple-leveraged bitcoin fund on Thursday, filings showed."]),
    )
    bank = build_fact_bank(_dossier([a, b]))
    approval = [f for f in bank.facts if "triple-leveraged bitcoin fund on Thursday" in f.text]
    assert len(approval) == 1 and approval[0].corroboration == 2
    speakers = {q.text: q.speaker for q in bank.quotes}
    assert speakers["This is an important milestone for the crypto market"] == "Justin Young"
    assert speakers["We expect strong demand from active traders"] == "Justin Young"


def test_gate_skips_thin_and_passes_rich():
    thin = _dossier([_doc("a.example", _padded(BASE))])
    gate = assess_evidence(thin, build_fact_bank(thin))
    assert not gate.passed
    assert any("full sources" in r for r in gate.reasons)

    loose = GatePolicy(min_full_sources=1, min_publishers=1, min_research_words=100, min_core_facts=2,
                       min_numeric_core_facts=1, min_corroborated_core_facts=0, min_core_fact_words=10)
    assert assess_evidence(thin, build_fact_bank(thin), loose).passed
