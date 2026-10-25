# -*- coding: utf-8 -*-
"""
Tests for icontract decorators on data_provider.realtime_types helpers.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from icontract import ViolationError

from data_provider.realtime_types import safe_float, safe_int


class TestSafeFloatContract(unittest.TestCase):
    def test_returns_float_for_numeric_string(self):
        self.assertEqual(safe_float("123.45"), 123.45)

    def test_returns_default_for_invalid_input(self):
        self.assertIsNone(safe_float("not-a-number"))

    def test_returns_default_for_none(self):
        self.assertIsNone(safe_float(None))

    def test_violates_when_default_is_not_numeric(self):
        with self.assertRaises(ViolationError):
            safe_float("123", default="invalid")  # type: ignore


class TestSafeIntContract(unittest.TestCase):
    def test_returns_int_for_numeric_string(self):
        self.assertEqual(safe_int("123.0"), 123)

    def test_returns_default_for_invalid_input(self):
        self.assertIsNone(safe_int("not-a-number"))

    def test_violates_when_default_is_not_int(self):
        with self.assertRaises(ViolationError):
            safe_int("123", default=1.5)  # type: ignore




if __name__ == "__main__":
    unittest.main()
