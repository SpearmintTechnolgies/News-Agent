from __future__ import annotations
import json
from pathlib import Path

def publish_dry_run(editorial: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "publish_payload.json"
    path.write_text(json.dumps(editorial, indent=2, ensure_ascii=False), encoding="utf-8")
    return path
