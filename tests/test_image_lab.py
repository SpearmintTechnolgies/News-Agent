from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from newsagent_v2.image.artifacts import (
    DEFAULT_RUNS_ROOT,
    persist_image_run,
    prepare_run_dir,
    raw_destination,
)
from newsagent_v2.image.benchmark.contract import (
    CANDIDATE_FAMILIES_NOT_INSTALLED,
    FAIRNESS_RULES,
    SUBJECTIVE_SCORE_FIELDS,
)
from newsagent_v2.image.benchmark.fixture import load_canonical_brief, load_canonical_source
from newsagent_v2.image.benchmark.runner import run_fair_benchmark, run_provider_benchmark
from newsagent_v2.image.benchmark.scorecard import build_scorecard, empty_subjective_scores
from newsagent_v2.image.brief import VisualBriefError, build_visual_brief, validate_brief_dict
from newsagent_v2.image.compose_existing import compose_existing_run
from newsagent_v2.image.compositor import (
    CompositorError,
    CompositionSpec,
    compose_card,
    discover_approved_logo,
    format_category_label,
    sanitize_overlay_text,
    wrap_text,
)
from newsagent_v2.image.contract import (
    ARTWORK_ONLY_INSTRUCTIONS,
    COMPOSITOR_VERSION,
    IMAGE_BRIEF_SCHEMA_VERSION,
)
from newsagent_v2.image.provider import (
    ProviderResult,
    apply_syntax_translation,
    canonical_provider_request,
)
from newsagent_v2.image.providers.synthetic import SyntheticImageProvider
from newsagent_v2.image.validate import (
    ImageValidationError,
    file_sha256,
    validate_raw_artwork,
    write_png_rgb,
)

REPO = Path(__file__).resolve().parents[1]
IMAGE_SRC = REPO / "src" / "newsagent_v2" / "image"
EDITORIAL_INPUT = REPO / "output" / "benchmarks" / "editorial_input.json"


def _measure_len(text: str) -> int:
    return len(text)


class VisualBriefTests(unittest.TestCase):
    def test_schema_and_artwork_only_restrictions(self) -> None:
        brief = load_canonical_brief()
        payload = brief.as_dict()
        self.assertEqual(payload["schema_version"], IMAGE_BRIEF_SCHEMA_VERSION)
        validate_brief_dict(payload)
        blob = " ".join(ARTWORK_ONLY_INSTRUCTIONS).lower()
        for needle in (
            "artwork only",
            "headline",
            "captions",
            "article text",
            "coinnetwork logo",
            "publication logo",
            "watermark",
            "random letters",
            "fake ui",
            "illegible pseudo-text",
            "source-publication branding",
        ):
            self.assertIn(needle, blob)
        prompt = brief.artwork_prompt().lower()
        self.assertIn("premium cinematic editorial artwork", prompt)
        self.assertIn("pure artwork only", prompt)
        self.assertNotIn("$449m", prompt)
        self.assertNotIn("$164", prompt)
        self.assertNotIn("do not render a headline", prompt)
        self.assertLess(len(prompt), 1200)
        self.assertIn("$449M", json.dumps(payload["facts"]))
        self.assertTrue(all(row["kind"] == "fact" for row in payload["facts"]))
        self.assertTrue(
            all(row["not_a_factual_claim"] is True for row in payload["visual_metaphors"])
        )

    def test_canonical_fixture_loads_v2_evidence_only(self) -> None:
        source = load_canonical_source()
        editorial = json.loads(EDITORIAL_INPUT.read_text(encoding="utf-8"))
        event = next(item for item in editorial["candidates"] if item["event_id"] == "event-027")
        self.assertEqual(source["event_id"], "event-027")
        self.assertEqual(source["evidence"][0]["summary"], event["evidence"][0]["summary"])
        self.assertEqual(source["evidence"][0]["url"], event["evidence"][0]["url"])
        self.assertFalse(source["evidence_notes"]["preferred_concept_present_in_v2_evidence"])
        brief = load_canonical_brief()
        self.assertNotIn("inflow", json.dumps(brief.financial_direction).lower())
        self.assertIn("outflow", json.dumps(brief.financial_direction).lower())

    def test_same_brief_reused_across_providers(self) -> None:
        brief = load_canonical_brief()
        request_a = canonical_provider_request(brief)
        request_b = canonical_provider_request(brief)
        self.assertEqual(request_a.as_dict(), request_b.as_dict())
        translated = apply_syntax_translation(
            request_a,
            extra={"engine": "dummy"},
            reason="backend requires an engine key; prompt text unchanged",
        )
        self.assertEqual(translated.prompt, request_a.prompt)
        self.assertTrue(translated.translation_applied)


class ProviderTelemetryTests(unittest.TestCase):
    def test_unknown_metrics_are_null(self) -> None:
        result = ProviderResult(provider_name="unset", success=False)
        payload = result.as_dict()
        for key in (
            "model_name",
            "load_time_ms",
            "generation_time_ms",
            "peak_vram_mb",
            "actual_cost_inr",
            "provider_reported_cost",
            "sha256",
            "license_name",
            "commercial_use_status",
        ):
            self.assertIsNone(payload[key])
        self.assertFalse(payload["success"])

    def test_cold_and_warm_distinguished(self) -> None:
        brief = load_canonical_brief()
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            runs = run_provider_benchmark(
                provider,
                brief,
                generations=2,
                persist_root=Path(tmp),
            )
        self.assertTrue(runs[0]["result"]["cold_start"])
        self.assertFalse(runs[0]["result"]["warm_generation"])
        self.assertIsNotNone(runs[0]["telemetry"]["load_time_ms"])
        self.assertTrue(runs[1]["result"]["warm_generation"])
        self.assertFalse(runs[1]["result"]["cold_start"])
        self.assertNotEqual(
            runs[0]["telemetry"]["cold_start"],
            runs[1]["telemetry"]["warm_generation"] and runs[0]["telemetry"]["cold_start"] is False,
        )


class ValidationAndArtifactTests(unittest.TestCase):
    def test_raw_hash_and_valid_16x9(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ok.png"
            write_png_rgb(path, 1280, 720, (12, 24, 36))
            report = validate_raw_artwork(path)
            self.assertEqual(report["width"], 1280)
            self.assertEqual(report["height"], 720)
            self.assertEqual(report["sha256"], file_sha256(path))
            self.assertIsNone(report["aesthetic_score"])

    def test_invalid_image_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.png"
            path.write_bytes(b"not an image" * 20)
            with self.assertRaises(ImageValidationError) as ctx:
                validate_raw_artwork(path)
            self.assertEqual(ctx.exception.code, "undecodable")

    def test_wrong_aspect_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "square.png"
            write_png_rgb(path, 800, 800, (0, 0, 0))
            with self.assertRaises(ImageValidationError) as ctx:
                validate_raw_artwork(path)
            self.assertEqual(ctx.exception.code, "aspect_ratio")

    def test_raw_never_overwritten_and_final_separate(self) -> None:
        brief = load_canonical_brief()
        provider = SyntheticImageProvider()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = run_provider_benchmark(
                provider,
                brief,
                persist_root=root,
                compose=True,
                composition_spec=CompositionSpec(
                    headline=brief.editorial_subject,
                    category_label="ETF",
                    test_mode=True,
                ),
            )
            raw = Path(runs[0]["result"]["raw_image_path"])
            final = Path(runs[0]["composition"]["final_path"])
            self.assertTrue(raw.exists())
            self.assertTrue(final.exists())
            self.assertNotEqual(raw, final)
            self.assertEqual(raw.parent.name, "raw")
            self.assertEqual(final.parent.name, "final")
            digest = file_sha256(raw)
            with self.assertRaises(ImageValidationError):
                raw_destination(Path(runs[0]["run_dir"]))
            self.assertEqual(file_sha256(raw), digest)
            self.assertNotEqual(digest, file_sha256(final))


class CompositorTests(unittest.TestCase):
    def test_test_mode_without_logo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            final = Path(tmp) / "final" / "card.png"
            write_png_rgb(raw, 1280, 720, (30, 30, 40))
            meta = compose_card(
                raw,
                final,
                CompositionSpec(headline="Bitcoin ETF outflows accelerate", test_mode=True),
            )
            self.assertTrue(final.exists())
            self.assertFalse(meta["logo"]["present"])
            self.assertTrue(meta["logo"]["required_asset"])
            self.assertFalse(meta["logo"]["fabricated"])
            self.assertEqual(file_sha256(raw), meta["raw_sha256"])

    def test_production_logo_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            final = Path(tmp) / "card.png"
            write_png_rgb(raw, 1280, 720, (30, 30, 40))
            with self.assertRaises(CompositorError) as ctx:
                compose_card(
                    raw,
                    final,
                    CompositionSpec(headline="Headline", test_mode=False, logo_path=None),
                )
            self.assertEqual(ctx.exception.code, "logo_required")
            self.assertFalse(final.exists())

    def test_production_logo_and_category_bounds(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            logo = Path(tmp) / "logo.png"
            final = Path(tmp) / "card.png"
            write_png_rgb(raw, 1280, 720, (24, 28, 36))
            Image.new("RGBA", (180, 48), (20, 90, 200, 255)).save(logo)
            digest = file_sha256(raw)
            logo_digest = file_sha256(logo)
            meta = compose_card(
                raw,
                final,
                CompositionSpec(
                    headline="Bitcoin ETF outflows accelerate as investors pull $449M in three days",
                    category_label="etf_product",
                    logo_path=logo,
                    test_mode=False,
                ),
            )
            self.assertEqual(meta["compositor_version"], COMPOSITOR_VERSION)
            self.assertEqual(meta["category_label"], "ETF PRODUCT")
            self.assertTrue(meta["logo"]["present"])
            self.assertEqual(meta["logo"]["sha256"], logo_digest)
            self.assertTrue(meta["logo"]["inside_safe_bounds"])
            self.assertTrue(meta["compositor_validation"]["passed"])
            self.assertEqual(meta["width"], 1280)
            self.assertEqual(meta["height"], 720)
            self.assertEqual(file_sha256(raw), digest)
            self.assertEqual(file_sha256(logo), logo_digest)
            self.assertLessEqual(len(meta["headline_lines"]), 3)

    def test_logo_only_skips_headline_and_category(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            logo = Path(tmp) / "logo.png"
            final = Path(tmp) / "card.png"
            write_png_rgb(raw, 1280, 720, (24, 28, 36))
            Image.new("RGBA", (180, 48), (255, 255, 255, 255)).save(logo)
            digest = file_sha256(raw)
            meta = compose_card(
                raw,
                final,
                CompositionSpec(
                    headline="Bitcoin ETF outflows accelerate as investors pull $449M in three days",
                    category_label="etf_product",
                    logo_path=logo,
                    test_mode=False,
                    logo_only=True,
                ),
            )
            self.assertEqual(meta["headline"], "")
            self.assertIsNone(meta["category_label"])
            self.assertEqual(meta["headline_lines"], [])
            self.assertTrue(meta["logo"]["present"])
            self.assertTrue(meta["compositor_validation"]["logo_only"])
            self.assertFalse(meta["compositor_validation"]["headline_drawn"])
            self.assertFalse(meta["compositor_validation"]["category_drawn"])
            self.assertTrue(meta["compositor_validation"]["passed"])
            self.assertEqual(file_sha256(raw), digest)

    def test_logo_blue_knockout_preserves_master_and_geometry(self) -> None:
        from PIL import Image, ImageDraw

        from newsagent_v2.image.logo_derive import derive_white_transparent_logo

        with tempfile.TemporaryDirectory() as tmp:
            master = Path(tmp) / "brand" / "coinnetwork_logo.png"
            master.parent.mkdir()
            image = Image.new("RGB", (80, 30), (6, 136, 211))
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 8, 28, 22), fill=(255, 255, 255))
            image.save(master)
            before = file_sha256(master)
            dest = Path(tmp) / "brand" / "derived" / "coinnetwork_logo_white_transparent.png"
            meta = derive_white_transparent_logo(master, dest)
            self.assertEqual(file_sha256(master), before)
            self.assertEqual(meta["width"], 80)
            self.assertEqual(meta["height"], 30)
            self.assertTrue(meta["has_alpha"])
            with Image.open(dest) as derived:
                self.assertEqual(derived.mode, "RGBA")
                self.assertEqual(derived.size, (80, 30))
                self.assertEqual(derived.getpixel((1, 1))[3], 0)
                self.assertGreaterEqual(derived.getpixel((20, 15))[3], 250)

    def test_category_label_and_logo_discovery(self) -> None:
        self.assertEqual(format_category_label("etf_product"), "ETF PRODUCT")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertIsNone(discover_approved_logo(root))
            dest = root / "brand"
            dest.mkdir()
            (dest / "coinnetwork_logo.png").write_bytes(b"not-an-image")
            found = discover_approved_logo(root)
            self.assertEqual(found, dest / "coinnetwork_logo.png")

    def test_compose_existing_run_from_source_artifact(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_id = "src-run"
            source = root / source_id
            (source / "raw").mkdir(parents=True)
            raw = source / "raw" / "artwork.jpg"
            Image.new("RGB", (1280, 720), (12, 16, 24)).save(raw, format="JPEG")
            expected = file_sha256(raw)
            (source / "brief.json").write_text(
                json.dumps(
                    {
                        "event_id": "event-027",
                        "story_category": "etf_product",
                        "editorial_subject": (
                            "Bitcoin ETF outflows accelerate as investors pull $449M in three days"
                        ),
                    }
                ),
                encoding="utf-8",
            )
            (source / "telemetry.json").write_text(
                json.dumps(
                    {
                        "event_id": "event-027",
                        "provider_name": "cloudflare_workers_ai",
                        "model_name": "@cf/black-forest-labs/flux-2-klein-4b",
                    }
                ),
                encoding="utf-8",
            )
            logo = Path(tmp) / "logo.png"
            Image.new("RGBA", (160, 40), (255, 255, 255, 255)).save(logo)
            result = compose_existing_run(
                source_id,
                headline="Bitcoin ETF outflows accelerate as investors pull $449M in three days",
                category_label="etf_product",
                event_id="event-027",
                logo_path=logo,
                expected_raw_sha256=expected,
                persist_root=root,
                source_root=root,
            )
            self.assertEqual(result["source_hash_before"], result["source_hash_after"])
            self.assertEqual(file_sha256(raw), expected)
            final = Path(result["composition"]["final_path"])
            self.assertTrue(final.exists())
            with Image.open(final) as image:
                self.assertEqual(image.size, (1280, 720))
            self.assertTrue(result["composition"]["compositor_validation"]["passed"])
            self.assertEqual(result["provenance"]["image_generation_requests"], 0)

    def test_deterministic_wrapping_and_impossible_headline(self) -> None:
        lines = wrap_text(
            "Bitcoin ETF outflows accelerate as investors pull funds",
            max_width=20,
            max_lines=4,
            measure=_measure_len,
        )
        self.assertGreaterEqual(len(lines), 2)
        self.assertTrue(all(len(line) <= 20 for line in lines))
        with self.assertRaises(CompositorError) as ctx:
            wrap_text(
                "supercalifragilisticexpialidocious",
                max_width=8,
                max_lines=3,
                measure=_measure_len,
            )
        self.assertEqual(ctx.exception.code, "unbreakable_word")
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            final = Path(tmp) / "card.png"
            write_png_rgb(raw, 1280, 720, (20, 20, 20))
            huge = "Bitcoin ETF outflows accelerate as investors pull funds " * 80
            with self.assertRaises(CompositorError) as overflow:
                compose_card(
                    raw,
                    final,
                    CompositionSpec(headline=huge, test_mode=True),
                )
            self.assertEqual(overflow.exception.code, "headline_overflow")

    def test_unicode_headline_sanitized(self) -> None:
        cleaned = sanitize_overlay_text("Bitcoin\x00 ETF â€” èµ„é‡‘æµå‡º\nstage")
        self.assertEqual(cleaned, "Bitcoin ETF â€” èµ„é‡‘æµå‡º stage")
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw.png"
            final = Path(tmp) / "card.png"
            write_png_rgb(raw, 1280, 720, (18, 22, 30))
            meta = compose_card(
                raw,
                final,
                CompositionSpec(headline="Bitcoin ETF â€” èµ„é‡‘æµå‡º", test_mode=True),
            )
            self.assertEqual(meta["headline"], "Bitcoin ETF â€” èµ„é‡‘æµå‡º")


class ScorecardAndFairnessTests(unittest.TestCase):
    def test_subjective_defaults_null_and_no_fake_quality_or_cost(self) -> None:
        card = build_scorecard(
            run_id="run-1",
            event_id="event-027",
            provider_name="synthetic_offline",
            model_name="synthetic-solid-png",
            objective={"generation_time_ms": 3, "success": True},
        )
        for name in SUBJECTIVE_SCORE_FIELDS:
            self.assertIsNone(card["subjective"][name])
        self.assertEqual(card["subjective"], empty_subjective_scores())
        self.assertFalse(card["auto_scored"])
        self.assertIsNone(card["objective"]["actual_cost_inr"])
        self.assertIsNone(card["objective"]["provider_reported_cost"])

    def test_objective_metrics_persisted(self) -> None:
        brief = load_canonical_brief()
        with tempfile.TemporaryDirectory() as tmp:
            runs = run_fair_benchmark(
                [
                    SyntheticImageProvider(color=(10, 10, 10)),
                    SyntheticImageProvider(color=(40, 40, 60)),
                ],
                brief,
                persist_root=Path(tmp),
            )
            self.assertEqual(runs[0][0]["request"]["prompt"], runs[1][0]["request"]["prompt"])
            path = Path(runs[0][0]["run_dir"]) / "scorecard.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsNotNone(saved["objective"]["generation_time_ms"])
            self.assertIsNotNone(saved["objective"]["sha256"])
            self.assertTrue(saved["objective"]["success"])
            telemetry = json.loads((Path(runs[0][0]["run_dir"]) / "telemetry.json").read_text())
            self.assertIsNone(telemetry["quality_score"])
            self.assertIsNone(telemetry["actual_cost_inr"])
            self.assertIn("same canonical story facts", " ".join(FAIRNESS_RULES).lower())
            self.assertIn("FLUX", CANDIDATE_FAMILIES_NOT_INSTALLED)


class IsolationTests(unittest.TestCase):
    def test_image_package_stays_offline_and_inside_v2(self) -> None:
        blocked = (
            "from_pretrained",
            "huggingface.co",
            "diffusers",
            "api.groq.com",
            "GROQ_API_KEY",
            "newsagent_v2.providers.groq",
            "sendPhoto",
            "sendMessage",
            "wordpress",
            "NewsAgent-Local",
            "Anime Faceless",
            "GenerateImage",
            "torch.hub",
        )
        for path in IMAGE_SRC.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json"}:
                continue
            text = path.read_text(encoding="utf-8")
            lowered = text.lower()
            self.assertNotIn("newsagent-local", lowered)
            self.assertNotIn("anime faceless", lowered)
            self.assertNotIn("c:\\hermes", lowered)
            for token in blocked:
                self.assertNotIn(token, text)
            if path.name not in {"cloudflare.py", "gemini.py"}:
                self.assertNotIn("import requests", text)
                self.assertNotIn("from requests", text)
            else:
                self.assertIn("import requests", text)
        self.assertTrue(str(DEFAULT_RUNS_ROOT).startswith(str(REPO)))
        self.assertIn("image_runs", str(DEFAULT_RUNS_ROOT))
        self.assertNotIn("main.py", (IMAGE_SRC / "__init__.py").read_text(encoding="utf-8"))


class PersistHelperTests(unittest.TestCase):
    def test_manifest_separates_raw_and_final(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = prepare_run_dir(Path(tmp) / "run")
            persist_image_run(
                base,
                brief={"schema_version": IMAGE_BRIEF_SCHEMA_VERSION},
                provider_request={},
                provider_result={"actual_cost_inr": None},
                telemetry={"quality_score": None},
                scorecard={"auto_scored": False},
                run_id="x",
            )
            manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["raw_and_final_separated"])


if __name__ == "__main__":
    unittest.main()


