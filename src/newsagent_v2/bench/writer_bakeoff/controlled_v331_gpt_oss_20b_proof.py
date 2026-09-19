"""One fresh-story Groq GPT-OSS 20B Controlled Writer V3.3.1 live article run."""

from __future__ import annotations

import json
from pathlib import Path

from newsagent_v2.article.writer.controlled.realization import classify_article_sentences
from newsagent_v2.bench.writer_bakeoff.contract import CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V331
from newsagent_v2.bench.writer_bakeoff import controlled_v33_gpt_oss_20b_proof as v33

REPO_ROOT = Path(__file__).resolve().parents[4]
PROOF_ROOT = REPO_ROOT / "benchmarks" / "writer_bakeoff" / "controlled_v331_fresh"
WINNER_DIR = PROOF_ROOT / "CONTROLLED_WRITER_V3_3_1_GPT_OSS_20B_AUTONOMOUS_WINNER"
CONSUMED_FLAG = PROOF_ROOT / "controlled_v331_gpt_oss_20b_one_shot_consumed.json"


def run_proof(**kwargs):
    v33.PROOF_ROOT = PROOF_ROOT
    v33.WINNER_DIR = WINNER_DIR
    v33.CONSUMED_FLAG = CONSUMED_FLAG
    v33.CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V33 = CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V331
    report = v33.run_proof(**kwargs)
    report["writer_architecture"] = "controlled_writer_v331"
    article = None
    run_dir = Path(str(report.get("run_dir") or ""))
    article_path = run_dir / "article.json"
    if article_path.is_file():
        article = json.loads(article_path.read_text(encoding="utf-8"))
    if article:
        prose = classify_article_sentences(
            headline=str(article.get("headline") or ""),
            dek=str(article.get("dek") or ""),
            body=str(article.get("article_body") or ""),
        )
        report["prose_forensic"] = prose["counts"]
        report["seo_title"] = article.get("seo_title")
        report["meta_description"] = article.get("meta_description")
        report["slug"] = article.get("slug")
        report["article_body"] = article.get("article_body")
    generated = report.get("generated_assertions") or 0
    supported = report.get("grounded_assertions_retained") or 0
    generated_words = report.get("generated_words") or 0
    retained_words = report.get("retained_words") or 0
    report["v331_vs_prior"] = {
        "v32_supported": "16/34",
        "v32_retained_words": "277/634",
        "v32_exact_overlaps": 7,
        "v32_max_similarity": 1.0,
        "v33_supported": "19/20",
        "v33_retained_words": "387/402",
        "v33_exact_overlaps": 1,
        "v33_max_similarity": 0.7595,
        "v331_supported": f"{supported}/{generated}",
        "v331_retained_words": f"{retained_words}/{generated_words}",
        "v331_exact_overlaps": report.get("exact_phrase_overlap"),
        "v331_max_similarity": report.get("max_similarity"),
    }
    return report


def main() -> dict:
    return run_proof()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, default=str))
