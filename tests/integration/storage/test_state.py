"""Integration tests for the ``app_state`` secret (sessions/CSRF signing key)."""

from __future__ import annotations

from pathlib import Path

from ecoflow_stats.storage.database import Database
from ecoflow_stats.storage.state import get_or_create_secret


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
