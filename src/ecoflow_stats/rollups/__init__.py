"""Daily rollup derivation: composes each capability's own per-day
aggregation (battery today; energy and grid in later phases) into one
`daily_rollups` refresh (design-data section 4.8)."""

from __future__ import annotations
