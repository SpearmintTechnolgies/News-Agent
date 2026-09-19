"""Dormant: V4 copyright recovery policy tests (module not on publish path)."""

from __future__ import annotations

import unittest


class TestV4CopyrightRecoveryPolicyDormant(unittest.TestCase):
    @unittest.skip("copyright recovery removed from V4 publish path")
    def test_placeholder(self) -> None:
        self.fail("unreachable")


if __name__ == "__main__":
    unittest.main()


