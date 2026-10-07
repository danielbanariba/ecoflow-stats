"""Unit tests for the pure `next=` redirect-target validation rule
(UI-08, qa-report-ui-01.md: "the `next` parameter accepts an absolute
URL / open redirect").

`safe_next_path` is the single chokepoint every caller (the login page,
the login submit handler, and `AccessControlMiddleware`'s own
redirect) routes an attacker-influenced `next` value through before
ever using it as a `Location` header -- these tests exercise it in
isolation, independent of any running app.
"""

from __future__ import annotations

from ecoflow_stats.web.security import safe_next_path


def test_none_collapses_to_root() -> None:
    assert safe_next_path(None) == "/"


def test_empty_string_collapses_to_root() -> None:
    assert safe_next_path("") == "/"


def test_a_path_not_starting_with_a_slash_collapses_to_root() -> None:
    """Pass-2 target: dropping the `raw.startswith("/")` check would let
    a bare `evil.com/phish` (no scheme, so some browsers still resolve
    it relative to nothing -- or a careless caller might string-concat
    it onto a host) through unchanged."""
    assert safe_next_path("evil.com/phish") == "/"


def test_a_protocol_relative_double_slash_host_collapses_to_root() -> None:
    """The pre-existing open-redirect guard: `//evil.com` is parsed by
    every browser as `https://evil.com` (scheme-relative), not a local
    path, even though it passes a naive `startswith("/")` check."""
    assert safe_next_path("//evil.com") == "/"
    assert safe_next_path("//evil.com/steal") == "/"


def test_a_leading_backslash_host_collapses_to_root() -> None:
    """UI-08 hardening: a leading `/\\evil.com` is normalized by several
    browsers (notably Chrome and Firefox) exactly like `//evil.com` --
    treated as scheme-relative to `evil.com` -- so it must be rejected
    too, even though it starts with a single forward slash and contains
    no literal `//`.

    Pass-2 target: removing the `"\\\\" in raw` clause from
    `safe_next_path` (keeping only the `//` and leading-slash checks)
    turns this red, letting `/\\evil.com` through as if it were the
    harmless local path `/evil.com`."""
    assert safe_next_path("/\\evil.com") == "/"


def test_a_slash_backslash_slash_host_collapses_to_root() -> None:
    """The `/\\/evil.com` variant -- a second real slash follows the
    backslash -- is the same browser-normalization bypass, attempted
    with an extra slash some sanitizers' `//`-only check might be
    tricked by if it strips the backslash first."""
    assert safe_next_path("/\\/evil.com") == "/"


def test_a_backslash_anywhere_in_an_otherwise_local_path_collapses_to_root() -> None:
    """No real route in this app ever legitimately contains a
    backslash, so treating any occurrence as suspicious -- not only a
    leading one -- costs nothing real and closes off a wider class of
    normalization quirks."""
    assert safe_next_path("/outages/evil\\host") == "/"


def test_a_safe_local_path_is_returned_unchanged() -> None:
    assert safe_next_path("/outages") == "/outages"


def test_a_safe_local_path_keeps_its_own_query_string() -> None:
    """UI-08's actual fix target: before this fix, every caller
    discarded the query string entirely (only `request.url.path` was
    ever carried as `next`), so returning to a filtered view after
    logging in silently reset the user's date-range filter. The
    validator itself must be transparent to a local path's own query
    string, not just its bare path, for any caller to be able to
    preserve it."""
    assert safe_next_path("/outages?from=2026-01-01&to=2026-01-02") == (
        "/outages?from=2026-01-01&to=2026-01-02"
    )


def test_a_target_pointing_back_at_login_collapses_to_root() -> None:
    """UI2-02 (qa-report-ui-02.md): `_referer_next_path` could produce
    `next=/login?next=...` when a non-`GET` request (the language-
    switch form) was submitted from the login page itself while
    logged out -- accepted unchanged before this fix since it starts
    with a single `/` and contains no `//`/backslash, it chained into
    a self-referential redirect a completed login never escaped.

    Pass-2 target: dropping the `_NEVER_A_NEXT_TARGET` check turns
    this red -- `/login?next=/battery` would pass through unchanged."""
    assert safe_next_path("/login") == "/"
    assert safe_next_path("/login?next=/battery") == "/"


def test_a_target_pointing_at_preferences_collapses_to_root() -> None:
    """UI-09 (qa-report-ui-01.md/-02.md): `/preferences` only ever
    accepts `POST` -- `next=/preferences` survived unchanged before
    this fix, so completing a login with that `next` landed on a raw
    `405` instead of a real page."""
    assert safe_next_path("/preferences") == "/"
    assert safe_next_path("/preferences?lang=es") == "/"
