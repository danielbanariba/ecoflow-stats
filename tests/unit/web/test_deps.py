"""Unit tests for the pure device-selection rule shared by every page
route (web-ui "One Device at a Time, With a Selector"; the device
choice persisting via cookie is this function's whole job).
"""

from __future__ import annotations

from ecoflow_stats.web.deps import select_device_id

_IDS = (1, 2, 3)


def test_a_valid_requested_device_wins_over_the_cookie() -> None:
    assert select_device_id(_IDS, requested=2, cookie_value="3") == 2


def test_an_invalid_requested_device_falls_back_to_the_cookie() -> None:
    assert select_device_id(_IDS, requested=99, cookie_value="3") == 3


def test_no_requested_device_uses_the_cookie() -> None:
    """Scenario basis for "the device choice persists via cookie across
    a second request": no `?device=` on the second request must still
    resolve to whatever the cookie remembers."""
    assert select_device_id(_IDS, requested=None, cookie_value="2") == 2


def test_an_invalid_cookie_value_falls_back_to_the_first_device() -> None:
    """Pass-2 target: a non-numeric or stale cookie must not raise or
    silently select nothing — the first configured device is the safe
    default, same as a brand-new visitor with no cookie at all."""
    assert select_device_id(_IDS, requested=None, cookie_value="not-a-number") == 1


def test_no_requested_device_and_no_cookie_uses_the_first_device() -> None:
    assert select_device_id(_IDS, requested=None, cookie_value=None) == 1
