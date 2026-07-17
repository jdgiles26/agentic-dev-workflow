"""conftest.py — pytest configuration for the agentic-dev-workflow test suite."""
import sys
from pathlib import Path

# Ensure the repo root is on sys.path so `from tools.xxx import` works.
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))
