"""Fair multi-backend image benchmark contract. No declared winner."""

from __future__ import annotations

from newsagent_v2.image.contract import HISTORICAL_AADI_PIXEL_METADATA

IMAGE_BENCHMARK_SCHEMA_VERSION = "image-benchmark-v1"
HISTORICAL_BASELINE_METADATA = HISTORICAL_AADI_PIXEL_METADATA

CANDIDATE_FAMILIES_NOT_INSTALLED = (
    "FLUX",
    "Qwen Image",
    "HiDream",
)

FAIRNESS_RULES = (
    "Every provider receives the same canonical story facts.",
    "Every provider receives an equivalent visual brief.",
    "Do not secretly improve the prompt for one model.",
    "Backend-specific syntax translation is allowed only when technically required.",
    "Record the exact final prompt/request representation.",
    "Do not download models in Phase A.",
    "Do not assume a winner.",
    "Do not invent benchmark results.",
)

SUBJECTIVE_SCORE_FIELDS = (
    "editorial_relevance",
    "prompt_adherence",
    "realism",
    "composition",
    "artifact_quality",
    "crypto_news_aesthetic",
    "originality",
    "brand_compatibility",
    "professionalism",
    "overall_quality",
)

OBJECTIVE_METRIC_FIELDS = (
    "generation_time_ms",
    "cold_start",
    "warm_generation",
    "load_time_ms",
    "backend_startup_ms",
    "first_image_latency_ms",
    "total_latency_ms",
    "peak_memory_mb",
    "peak_vram_mb",
    "success",
    "failure_reason",
    "retry_count",
    "provider_reported_cost",
    "actual_cost_inr",
    "width",
    "height",
    "size_bytes",
    "sha256",
    "http_status",
    "request_latency_ms",
    "cloudflare_request_id",
    "response_format",
    "provider_reported_usage",
    "provider_reported_neurons",
    "requested_width",
    "requested_height",
    "provider_reported_cost_usd",
    "provider_reported_image_usage",
    "provider_reported_tokens",
    "provider_reported_latency",
    "estimated_list_price_usd",
    "estimated_list_price_inr",
    "estimated_list_price_is_estimate",
    "requested_aspect_ratio",
    "requested_resolution_tier",
    "provider_request_id",
    "total_pipeline_latency_ms",
)
