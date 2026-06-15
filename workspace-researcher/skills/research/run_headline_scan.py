#!/usr/bin/env python3
"""
run_headline_scan.py — DEPRECATED wrapper. Delegates to scan_headlines.py.

Use scan_headlines.py directly. This file exists only so older agent sessions
that invoke run_headline_scan.py still work.
"""
from __future__ import annotations

import os
import runpy
import sys

SCAN = os.path.join(os.path.dirname(os.path.dirname(__file__)), "headline-scan", "scan_headlines.py")

if __name__ == "__main__":
    print("[DEPRECATED] use skills/headline-scan/scan_headlines.py", file=sys.stderr)
    sys.argv[0] = SCAN
    runpy.run_path(SCAN, run_name="__main__")
