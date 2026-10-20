#!/usr/bin/env python3
"""Run unit tests (no network, no provider dispatch)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

suite = unittest.defaultTestLoader.discover(str(TESTS_DIR), pattern="test_*.py")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
