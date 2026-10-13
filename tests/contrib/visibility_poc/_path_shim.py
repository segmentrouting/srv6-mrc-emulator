"""Shared sys.path shim for visibility-poc scraper tests.

`visibility/scraper/` is not a Python package (it ships as a container).
We inject the scraper directory directly so tests can `import scraper`.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRAPER_DIR = _REPO_ROOT / "visibility" / "scraper"
if str(_SCRAPER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRAPER_DIR))
