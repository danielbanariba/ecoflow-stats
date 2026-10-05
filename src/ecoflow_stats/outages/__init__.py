"""Outage detection: a pure, versioned detector that serves both live
alerts and recomputed statistics, so the two never disagree about whether
a given period was an outage (design D6)."""

from __future__ import annotations
