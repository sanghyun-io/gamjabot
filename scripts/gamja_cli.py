#!/usr/bin/env python3
"""Development wrapper for the bundled Owner CLI.

Release builds expose the same CLI through `backend --cli`, so this wrapper
never requires a repository or virtual environment after installation.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from app.owner_cli import *  # re-export testable CLI helpers

if __name__ == "__main__":
    main()
