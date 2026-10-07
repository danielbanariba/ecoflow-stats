"""Unit tests for the pure health-status logic (amendment A1).

Deliberately separate from the HTTP route: `evaluate_status` decides the
`ok`/`degraded`/`starting` body field from per-device sample age alone.
The 503 trigger (collector dead or database unwritable) is a completely
different, independent check — see `tests/integration/web/test_health.py`
for that half of the contract.
"""

from __future__ import annotations

from ecoflow_stats.web.routes.health import evaluate_status

_THRESHOLD = 180


def test_starting_when_no_device_has_sampled_yet() -> None:
    assert evaluate_status([], stale_threshold_s=_THRESHOLD) == "starting"
    assert evaluate_status([None], stale_threshold_s=_THRESHOLD) == "starting"


def test_starting_when_any_device_among_several_has_not_sampled_yet() -> None:
    """A brand-new device added alongside an already-healthy one must not
    be silently skipped — the overall status still reflects it."""
    assert evaluate_status([10, None, 20], stale_threshold_s=_THRESHOLD) == "starting"


def test_ok_when_every_device_is_within_the_staleness_threshold() -> None:
    assert evaluate_status([0, 179], stale_threshold_s=_THRESHOLD) == "ok"


def test_degraded_when_one_middle_device_exceeds_the_threshold_but_others_are_fresh() -> None:
    """Must check every device, not just the first or the last: only the
    middle entry here is stale. An EcoFlow cloud outage ages every sample
    but must only ever reach `degraded` here — never force a 503 by itself
    (amendment A1: 503 is reserved for a dead collector or an unwritable
    database, not for stale data a restart cannot fix)."""
    assert evaluate_status([0, 500, 0], stale_threshold_s=_THRESHOLD) == "degraded"
