"""Integration tests for the ``app_state`` secret (sessions/CSRF signing
key) and session-revocation generation (SEC-07)."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.state import (
    get_or_create_secret,
    get_session_generation,
    set_session_generation,
)


def test_the_secret_is_created_once_and_then_reused(tmp_path: Path) -> None:
    """A new session/CSRF signing key must not be minted on every call —
    that would invalidate every existing session on each request."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        first = get_or_create_secret(db.writer)
        second = get_or_create_secret(db.writer)
        assert first == second
    finally:
        db.close()


def test_the_secret_survives_a_restart(tmp_path: Path) -> None:
    """Restarting the process must not revoke every password session — the
    secret has to persist in the database, not live only in memory."""
    path = tmp_path / "ecoflow-stats.db"
    db = Database(path)
    secret = get_or_create_secret(db.writer)
    db.close()

    db2 = Database(path)
    try:
        assert get_or_create_secret(db2.writer) == secret
    finally:
        db2.close()


def test_session_generation_defaults_to_zero_when_never_set(tmp_path: Path) -> None:
    """A fresh database (never logged anything out) must read back the
    same `0` default `SecurityContext.session_generation` already
    starts from, or a brand-new install would reject its own
    just-issued cookies."""
    db = Database(tmp_path / "ecoflow-stats.db")
    try:
        assert get_session_generation(db.writer) == 0
    finally:
        db.close()


def test_session_generation_persists_and_overwrites_across_a_restart(tmp_path: Path) -> None:
    """SEC-07: `/logout` bumping the generation must survive a restart —
    otherwise a cookie it just revoked would start verifying again the
    moment the next process reads back the stale `0` default instead of
    the value actually set here."""
    path = tmp_path / "ecoflow-stats.db"
    db = Database(path)
    set_session_generation(db.writer, 1)
    db.close()

    db2 = Database(path)
    try:
        assert get_session_generation(db2.writer) == 1
        set_session_generation(db2.writer, 2)
        assert get_session_generation(db2.writer) == 2
    finally:
        db2.close()
