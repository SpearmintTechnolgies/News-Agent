#!/usr/bin/env python3
"""Tests for HTML parser tool."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from tools.parser.parser import parse  # noqa: E402

SAMPLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Write For Us - Crypto Blog</title></head>
<body>
  <h1>Guest Post Guidelines</h1>
  <p>We welcome guest post submissions from cryptography experts.</p>
  <form action="/submit" method="post">
    <input name="author" type="text" required>
    <textarea name="message"></textarea>
  </form>
  <a href="https://example.com/about">About</a>
  <a href="/contact">Contact</a>
</body>
</html>
"""


class ParserTests(unittest.TestCase):
    def test_parse_extracts_title_forms_links_and_signals(self) -> None:
        result = parse(SAMPLE_HTML, base_url="https://example.com/write-for-us")
        self.assertIn("Write For Us", result.title)
        self.assertIn("guest post", result.text.lower())
        self.assertEqual(len(result.forms), 1)
        self.assertEqual(result.forms[0].method, "post")
        self.assertTrue(result.signals["guest_post_language"])
        self.assertTrue(result.signals["has_form"])
        self.assertTrue(result.signals["has_textarea_form"])
        self.assertIn("https://example.com/about", result.links)
        self.assertIn("https://example.com/contact", result.links)


if __name__ == "__main__":
    unittest.main()
