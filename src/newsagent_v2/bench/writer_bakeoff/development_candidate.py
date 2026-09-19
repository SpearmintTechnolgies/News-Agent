"""Build a development-only corrected CanonicalArticle from the frozen Gemini run.

No LLM. No API. Does not write production WINNER_CANDIDATE.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from newsagent_v2.article.qa import run_article_qa
from newsagent_v2.article.qa.textutil import split_paragraphs, word_count
from newsagent_v2.article.render import materialize_article
from newsagent_v2.article.writer.grounding_resolve import resolve_grounding
from newsagent_v2.bench.writer_bakeoff.contract import EVENT_ID, SOURCE_BATCH_ID
from newsagent_v2.bench.writer_bakeoff.fixture import load_fixture
from newsagent_v2.bench.writer_bakeoff.runner import evidence_mapping, verify_fixture_hashes
from newsagent_v2.bench.writer_bakeoff.score import score_result
from newsagent_v2.benchmark.input import write_json_utf8

REPO_ROOT = Path(__file__).resolve().parents[4]
ORIGINAL_RUN = (
    REPO_ROOT
    / "benchmarks"
    / "writer_bakeoff"
    / EVENT_ID
    / "live_runs"
    / "20260915T092107Z"
)
ORIGINAL_ARTICLE = ORIGINAL_RUN / "gemini_3_6_flash_article_first" / "article.json"
FIXTURE = REPO_ROOT / "benchmarks" / "writer_bakeoff" / EVENT_ID
DEST = FIXTURE / "DEVELOPMENT_WINNER_CANDIDATE"

# Evidence-backed replacements only. Existing claims/quotes/evidence IDs.
PARAGRAPHS = [
    (
        "United States Senate Republicans have unveiled a revised 635-page proposal "
        "for the CLARITY Act, positioning the draft as a final offer to Democrats "
        "ahead of a pivotal procedural vote. Released on Sunday, the updated text "
        "introduces major changes to regulations governing government officials' "
        "involvement with digital assets. The upcoming procedural vote is scheduled "
        "for Tuesday at 2:15 p.m. Eastern Time, coming just two days after the "
        "text's release, to determine whether the Senate will advance the bill "
        "toward full floor consideration."
    ),
    (
        "Cynthia Lummis, chair of the United States Senate Banking Digital Assets "
        "Subcommittee, released the revised legislation with Chairmen John Boozman "
        "and Tim Scott. Speaking to reporters on Sunday, a Republican aide described "
        "the 635-page proposal as the party's final offer on the bill. The revised "
        "635-page CLARITY Act text was released on Sunday as a final offer to Democrats "
        "ahead of a Tuesday procedural vote on whether the Senate advances the "
        "635-page bill to floor consideration."
    ),
    (
        "Senator Lummis said President Trump had agreed to the new ethics provisions. "
        "According to Senator Lummis, President Trump voluntarily agreed to "
        "unprecedented ethics restrictions integrated into the bill. The proposal "
        "includes ethics restrictions holding federally elected officials, judges, "
        "and their spouses to strict rules regarding digital assets."
    ),
    (
        "Lummis said the final bill text reflects a year of bipartisan negotiations. "
        "She revealed that the final bill text incorporates 126 specific changes made "
        "at the direct request of Democrats. \"After a year of intense daily bipartisan "
        "negotiations, this bill is ready,\" Lummis stated. She added, \"President Trump "
        "voluntarily agreed to unprecedented ethics restrictions, holding every federally "
        "elected official, judge, and their spouses to some of the toughest ethics "
        "restrictions in US history.\""
    ),
    (
        "The 635-page revised proposal also includes changes to the Blockchain "
        "Regulatory Certainty Act (BRCA). The revised proposal also includes "
        "provisions governing stablecoin yield."
    ),
    (
        "The procedural vote scheduled for Tuesday at 2:15 p.m. ET marks a decisive "
        "juncture for the CLARITY Act. Senate lawmakers will vote on whether to "
        "formally advance the 635-page proposal to floor consideration, testing whether "
        "a year of bipartisan adjustments and 126 requested edits will secure the "
        "necessary votes to move the legislation forward."
    ),
]

CLAIM_IDS = [
    ["claim-001", "claim-002", "claim-003"],
    ["claim-001", "claim-003", "claim-004", "claim-005"],
    ["claim-006", "claim-007"],
    ["claim-007", "claim-008"],
    ["claim-009"],
    ["claim-001", "claim-003", "claim-008"],
]


def build_corrected_article(original: dict[str, Any]) -> dict[str, Any]:
    article = deepcopy(original)
    body = "\n\n".join(PARAGRAPHS)
    article["article_body"] = body
    sections = []
    maps = []
    for index, (text, claim_ids) in enumerate(zip(PARAGRAPHS, CLAIM_IDS)):
        section_id = f"s{index + 1}"
        sections.append(
            {
                "id": section_id,
                "section_id": section_id,
                "purpose": "",
                "paragraphs": [{"text": text, "claim_ids": list(claim_ids)}],
            }
        )
        maps.append({"paragraph_index": index, "claim_ids": list(claim_ids)})
    article["article_sections"] = sections
    article["paragraph_maps"] = maps
    return materialize_article(article)


def persist_development_winner(
    *,
    article: dict[str, Any],
    qa: dict[str, Any],
    score: dict[str, Any],
    resolver: dict[str, Any],
    fixture_hashes: dict[str, Any],
    original_words: int,
    corrected_words: int,
    corrections: list[dict[str, Any]],
) -> str:
    dest = DEST
    dest.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "DEVELOPMENT_WINNER_CANDIDATE",
        "development_corrected_candidate": True,
        "autonomous_writer_pass": False,
        "human_or_deterministic_correction": True,
        "source_writer": "gemini-3.6-flash",
        "source_provider": "google_gemini",
        "source_run": str(ORIGINAL_RUN),
        "source_article": str(ORIGINAL_ARTICLE),
        "event_id": EVENT_ID,
        "source_batch_id": SOURCE_BATCH_ID,
        "image_generated": False,
        "telegram_sent": False,
        "wordpress_called": False,
        "make_invoked": False,
        "original_word_count": original_words,
        "corrected_word_count": corrected_words,
        "fixture_hashes": fixture_hashes,
        "qa_publishable": bool(qa.get("publishable")),
    }
    write_json_utf8(dest / "manifest.json", manifest)
    write_json_utf8(dest / "article.json", article)
    write_json_utf8(dest / "normalized.json", article)
    (dest / "article_body.txt").write_text(str(article.get("article_body") or ""), encoding="utf-8")
    write_json_utf8(dest / "qa.json", qa)
    write_json_utf8(dest / "score.json", score)
    write_json_utf8(dest / "evidence_mapping.json", evidence_mapping(article))
    write_json_utf8(dest / "resolver.json", resolver)
    write_json_utf8(dest / "corrections.json", corrections)
    write_json_utf8(
        dest / "telemetry.json",
        {
            "development_corrected_candidate": True,
            "autonomous_writer_pass": False,
            "provider": "google_gemini",
            "model": "gemini-3.6-flash",
            "qa_publishable": bool(qa.get("publishable")),
            "original_word_count": original_words,
            "corrected_word_count": corrected_words,
            "live_http_calls": 0,
            "image_generated": False,
        },
    )
    return str(dest)


def main() -> dict[str, Any]:
    original = json.loads(ORIGINAL_ARTICLE.read_text(encoding="utf-8"))
    fixture = load_fixture(FIXTURE)
    article_input = fixture["article_input"]
    hash_check = verify_fixture_hashes(fixture)
    original_words = word_count(str(original.get("article_body") or ""))
    article = build_corrected_article(original)
    corrected_words = word_count(str(article.get("article_body") or ""))
    if corrected_words < 350:
        return {
            "ok": False,
            "stopped": True,
            "reason": "corrected article is below hard minimum 350; no filler added",
            "original_word_count": original_words,
            "corrected_word_count": corrected_words,
            "qa_publishable": False,
            "live_http_calls": 0,
        }
    resolved = resolve_grounding(article, article_input)
    resolver_report = {
        "coverage_texts": resolved.get("_resolved_coverage_texts") or [],
        "article_body_unchanged_by_resolver": resolved.get("article_body") == article.get("article_body"),
        "claims_unchanged_by_resolver": resolved.get("claims") == article.get("claims"),
    }
    qa = run_article_qa(deepcopy(article), article_input, article_mode="normal")
    score = score_result(
        provider="google_gemini",
        model="gemini-3.6-flash",
        mode="article_first_grounding_map",
        qa=qa,
        article=article,
        article_input=article_input,
        http_status=200,
        latency_ms=None,
        retries=0,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        estimated_list_price_usd=None,
        provider_reported_cost_usd=None,
        candidate_id="development_corrected_gemini_3_6_flash",
        replay=False,
    )
    corrections = [
        {
            "id": "A_swaying",
            "from": "The release follows an extended legislative process aimed at swaying Senate Democrats to support advancing the digital asset measure.",
            "to": "The revised 635-page CLARITY Act text was released on Sunday as a final offer to Democrats ahead of a Tuesday procedural vote on whether the Senate advances the 635-page bill to floor consideration.",
            "evidence": "Senate Republicans released revised CLARITY Act text as a final offer to Democrats, with a key procedural vote set for Tuesday. ... which will determine whether the Senate can advance the bill toward floor consideration.",
            "claims": ["claim-001", "claim-003"],
        },
        {
            "id": "B_endorsement",
            "from": "A central element of the updated CLARITY Act text involves ethics rules that have received the explicit endorsement of US President Donald Trump.",
            "to": "Senator Lummis said President Trump had agreed to the new ethics provisions.",
            "evidence": "Lummis said the new ethics provisions had been agreed to by US President Donald Trump.",
            "claims": ["claim-007"],
        },
        {
            "id": "title_string_reorder",
            "from": "The revised legislation was released by United States Senate Banking Digital Assets Subcommittee Chair Cynthia Lummis, alongside Chairmen John Boozman and Tim Scott.",
            "to": "Cynthia Lummis, chair of the United States Senate Banking Digital Assets Subcommittee, released the revised legislation with Chairmen John Boozman and Tim Scott.",
            "evidence": "released by US Senate Banking Digital Assets Subcommittee Chair Cynthia Lummis alongside Chairmen John Boozman and Tim Scott",
            "claims": ["claim-004"],
        },
        {
            "id": "source_prose_floor_vote",
            "from": "ahead of a Tuesday procedural vote that will determine whether the Senate can advance the bill toward floor consideration",
            "to": "ahead of a Tuesday procedural vote on whether the Senate advances the 635-page bill to floor consideration",
            "evidence": "a procedural vote on the CLARITY Act on Tuesday at 2:15pm ET, which will determine whether the Senate can advance the bill toward floor consideration",
            "claims": ["claim-003"],
        },
        {
            "id": "ethics_claim006_alignment",
            "from": "Officials elected at the federal level, along with judges and their spouses, would be covered by the digital-asset ethics restrictions.",
            "to": "The proposal includes ethics restrictions holding federally elected officials, judges, and their spouses to strict rules regarding digital assets.",
            "evidence": "holding every federally elected official, judge, and their spouses to some of the toughest ethics restrictions in US history",
            "claims": ["claim-006"],
        },
        {
            "id": "C_emphasized",
            "from": "Senator Lummis emphasized the extensive work behind the new text, noting that the draft reflects a full year of intense daily bipartisan negotiations.",
            "to": "Lummis said the final bill text reflects a year of bipartisan negotiations.",
            "evidence": "Lummis said the final bill text reflects a year of bipartisan negotiations and 126 changes made at the request of Democrats.",
            "claims": ["claim-008"],
        },
        {
            "id": "D_stablecoin",
            "from": "Additionally, the revised bill establishes regulatory provisions governing stablecoin yield, addressing critical components of cryptocurrency oversight under discussion in Congress.",
            "to": "The revised proposal also includes provisions governing stablecoin yield.",
            "evidence": "also includes ... provisions governing stablecoin yield",
            "claims": ["claim-009"],
        },
        {
            "id": "generic_beyond_ethics",
            "from": "Beyond government ethics, the 635-page revised proposal contains significant policy updates regarding broader digital asset regulation.",
            "to": "The 635-page revised proposal also includes changes to the Blockchain Regulatory Certainty Act (BRCA).",
            "evidence": "also includes changes to the Blockchain Regulatory Certainty Act (BRCA)",
            "claims": ["claim-009"],
        },
    ]
    result = {
        "ok": bool(qa.get("publishable")),
        "original_word_count": original_words,
        "corrected_word_count": corrected_words,
        "qa_publishable": bool(qa.get("publishable")),
        "uncovered": (qa.get("metrics") or {}).get("uncovered_assertive_sentences") or [],
        "critical": [item.get("code") for item in (qa.get("critical_failures") or [])],
        "warnings": [item.get("code") for item in (qa.get("warnings") or [])],
        "metrics": qa.get("metrics"),
        "score": score,
        "fixture_hashes": hash_check,
        "resolver": resolver_report,
        "paragraphs": split_paragraphs(article["article_body"]),
        "live_http_calls": 0,
    }
    if qa.get("publishable"):
        result["path"] = persist_development_winner(
            article=article,
            qa=qa,
            score=score,
            resolver=resolver_report,
            fixture_hashes=hash_check,
            original_words=original_words,
            corrected_words=corrected_words,
            corrections=corrections,
        )
    return result


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
