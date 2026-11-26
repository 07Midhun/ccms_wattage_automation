"""Pytest configuration and path setup."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = TESTS_DIR.parent

for path_str in (str(TESTS_DIR), str(PROJECT_DIR)):
    if path_str not in sys.path:
        sys.path.insert(0, path_str)
