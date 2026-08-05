"""Shared pytest configuration for bridge bugfix tests.

Adds repo root to sys.path so `import hermes_vtuber_bridge` works whether
pytest is invoked from the repo root or the tests/ directory.
"""

import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
