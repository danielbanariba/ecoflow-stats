"""Daily energy accounting: cumulative Wh counters turned into daily kWh
by source and direction, timezone-aware and defended against a counter
reset (energy spec; design-data section 4.5)."""

from __future__ import annotations
