"""Load a frozen writer-bake-off fixture. No network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_fixture(path: Path) -> dict[str, Any]:
    root = Path(path)
    required = (
        "manifest.json",
        "article_input.json",
        "compact_writer_input.json",
        "baseline_a_groq_article.json",
        "baseline_a_qa.json",
        "qa_config.json",
        "baseline_a_editorial_telemetry.json",
    )
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise FileNotFoundError(f"incomplete fixture {root}: missing {missing}")
    return {
        "root": root,
        "manifest": load_json(root / "manifest.json"),
        "article_input": load_json(root / "article_input.json"),
        "compact_writer_input": load_json(root / "compact_writer_input.json"),
        "baseline_article": load_json(root / "baseline_a_groq_article.json"),
        "baseline_qa": load_json(root / "baseline_a_qa.json"),
        "qa_config": load_json(root / "qa_config.json"),
        "baseline_editorial": load_json(root / "baseline_a_editorial_telemetry.json"),
    }
