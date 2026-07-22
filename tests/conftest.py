"""Pytest config for the news bot tests."""

import sys
from pathlib import Path

# Ensure the repo root is importable so `from newsbot...` / `from core...` work.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))