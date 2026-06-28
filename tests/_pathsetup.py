"""Put the repository root on sys.path so tests import the package uninstalled.

Imported for its side effect by each test module. With the flat layout the
``rpm_fetch`` package sits at the repo root, so adding that root lets the suite
run from anywhere without an editable install.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
