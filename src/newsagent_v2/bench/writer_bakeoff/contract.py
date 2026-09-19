"""Writer bake-off constants. Does not change live /make."""

from __future__ import annotations

SOURCE_BATCH_ID = "20260915T081205Z-2b732ddc"
SOURCE_BATCH_REL = f"output/approval/{SOURCE_BATCH_ID}"
EVENT_ID = "event-005"
DEFAULT_FIXTURE_REL = f"benchmarks/writer_bakeoff/{EVENT_ID}"

MODE_STRUCTURED = "structured_current"
MODE_ARTICLE_FIRST = "article_first_grounding_map"

PROVIDER_GROQ = "groq"
PROVIDER_GEMINI = "google_gemini"
PROVIDER_QWEN_VLLM = "qwen_vllm"
PROVIDER_BEDROCK_MANTLE = "bedrock_mantle"

QWEN_MODEL = "vllm-local/qwen3.8-27b"
CANDIDATE_QWEN_ARTICLE_FIRST = "qwen_vllm_qwen3_8_27b_article_first"

GROQ_MODEL = "openai/gpt-oss-120b"
# One-call bake-off only. Not wired into /make.
GROQ_LLAMA_33_MODEL = "llama-3.3-70b-versatile"
# Groq-hosted Qwen. Not the Cloudflare/vLLM Qwen tunnel.
GROQ_QWEN_38_MODEL = "qwen/qwen3.8-27b"
GROQ_GPT_OSS_20B_MODEL = "openai/gpt-oss-20b"
CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V3 = "groq_gpt_oss_20b_controlled_writer_v3"
CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V32 = "groq_gpt_oss_20b_controlled_writer_v32"
CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V33 = "groq_gpt_oss_20b_controlled_writer_v33"
CANDIDATE_GROQ_GPT_OSS_20B_CONTROLLED_V331 = "groq_gpt_oss_20b_controlled_writer_v331"
GEMINI_TEXT_MODEL = "gemini-2.5-flash"
# Exact model named by the Gemini 2.5 Flash HTTP 404. Not invented.
GEMINI_NEXT_TEXT_MODEL = "gemini-3.6-flash"
# CEO-requested starter-writer audition. Not wired into /make.
GEMINI_38_TEXT_MODEL = "gemini-3.8-flash"
# Controlled Kimi K2.5 bake-off only. Not wired into /make, Top-5, or images.
BEDROCK_MANTLE_BASE_URL = "https://bedrock-mantle.us-east-1.api.aws/v1"
KIMI_K25_MODEL = "moonshotai.kimi-k2.5"
CANDIDATE_KIMI_K25_ARTICLE_FIRST = "bedrock_mantle_kimi_k2_5_article_first"
KIMI_HARD_MAX_GENERATION_CALLS = 1

CANDIDATE_GROQ_STRUCTURED = "groq_gpt_oss_120b_structured"
CANDIDATE_GROQ_ARTICLE_FIRST = "groq_gpt_oss_120b_article_first"
CANDIDATE_GROQ_LLAMA_33_ARTICLE_FIRST = "groq_llama_3_3_70b_article_first"
CANDIDATE_GROQ_QWEN_38_ARTICLE_FIRST = "groq_qwen_3_8_27b_article_first"
CANDIDATE_GROQ_QWEN_38_LEDGER_FIRST = "groq_qwen_3_8_27b_ledger_first"
CANDIDATE_GEMINI_STRUCTURED = "gemini_2_5_flash_structured"
CANDIDATE_GEMINI_ARTICLE_FIRST = "gemini_2_5_flash_article_first"
CANDIDATE_GEMINI_36_ARTICLE_FIRST = "gemini_3_6_flash_article_first"
CANDIDATE_GEMINI_36_LEDGER_FIRST = "gemini_3_6_flash_ledger_first"
CANDIDATE_GEMINI_38_ARTICLE_FIRST = "gemini_3_8_flash_article_first"

STRUCTURE_CODES = frozenset(
    {
        "paragraph_missing_claim_ids",
        "unknown_claim_id",
        "claim_missing_evidence",
        "unknown_evidence_ref",
        "foreign_evidence_ref",
        "orphan_claim",
        "quote_body_unmapped",
    }
)
QUOTE_CODES = frozenset(
    {
        "quote_body_unmapped",
        "quote_missing_evidence",
        "quote_claim_no_evidence",
        "direct_quote_missing_attribution",
    }
)
GROUNDING_CODES = frozenset(
    {
        "body_assertion_not_in_claims",
        "ungrounded_contextual_assertion",
        "unsupported_absence_claim",
        "claim_missing_evidence",
        "claim_unknown_evidence",
        "foreign_evidence_ref",
        "unknown_evidence_ref",
    }
)
SIMILARITY_CODES = frozenset(
    {
        "exact_phrase_overlap",
        "high_sentence_similarity",
    }
)
HEADLINE_CODES = frozenset(
    {
        "headline_empty",
        "headline_length",
        "headline_all_caps",
        "empty_headline",
    }
)
MECHANICS_CODES = frozenset(
    {
        "empty_headline",
        "empty_body",
        "repeated_sentence",
        "duplicate_punctuation",
    }
)

HARD_MIN_WORDS = 350
TARGET_MIN_WORDS = 450
TARGET_MAX_WORDS = 800
PREFERRED_MIN_WORDS = 500
PREFERRED_MAX_WORDS = 650
