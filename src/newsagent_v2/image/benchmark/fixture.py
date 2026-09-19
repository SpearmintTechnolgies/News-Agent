"""Load the canonical image-benchmark fixture from packaged V2 evidence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from newsagent_v2.image.brief import VisualBrief, build_visual_brief

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "canonical_etf_fund_flow.json"
)


def load_canonical_source(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_canonical_brief(path: Path = FIXTURE_PATH) -> VisualBrief:
    return build_visual_brief(load_canonical_source(path))
