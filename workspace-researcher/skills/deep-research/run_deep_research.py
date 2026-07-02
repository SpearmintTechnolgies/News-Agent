#!/usr/bin/env python3
"""
run_deep_research.py — DEPRECATED wrapper. Delegates to run_research.py.

Use run_research.py directly. This file exists only so older agent sessions
that invoke run_deep_research.py still work.
"""
from __future__ import annotations

import os
import runpy
import sys

_RESEARCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_research.py")

if __name__ == "__main__":
    print("[DEPRECATED] use skills/deep-research/run_research.py", file=sys.stderr)
    # Map legacy --discover-aggressive to --extra-search
    argv = list(sys.argv)
    if "--discover-aggressive" in argv:
        argv[argv.index("--discover-aggressive")] = "--extra-search"
    sys.argv = argv
    sys.argv[0] = _RESEARCH
    runpy.run_path(_RESEARCH, run_name="__main__")
