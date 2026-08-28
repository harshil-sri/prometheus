"""Pytest bootstrap — resolves the dual import-root layout.

Tests import via `src.twin...` (repo root on sys.path) while modules inside
src/ import via `twin.core...` (src/ on sys.path). Adding both roots here makes
`pytest tests/` work from the repo root on any machine.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")

for _path in (ROOT, SRC):
    if _path not in sys.path:
        sys.path.insert(0, _path)
