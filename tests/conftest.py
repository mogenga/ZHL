"""Repo-root on sys.path for all test packages.

Mirrors ``tests/e2e/conftest.py`` so that ``tests/api`` (and the other
top-level test packages) can import ``app.*`` / ``tests.fixtures.*``
regardless of which pytest rootdir the run starts from. This keeps the
``.venv`` Python from relying on the implicit pytest rootdir trick.
"""

from __future__ import annotations

import sys
from pathlib import Path

# tests/conftest.py lives one level below the repo root; the root is .parent.parent.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
