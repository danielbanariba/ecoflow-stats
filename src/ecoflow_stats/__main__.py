"""Enable ``python -m ecoflow_stats``."""

from __future__ import annotations

import sys

from ecoflow_stats.cli import main

if __name__ == "__main__":
    sys.exit(main())
