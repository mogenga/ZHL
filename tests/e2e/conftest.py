"""E2E package conftest: make the repo root importable so test modules can
import ``tests.fixtures.loader`` regardless of the pytest rootdir layout."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
