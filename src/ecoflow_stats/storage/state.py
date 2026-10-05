"""The ``app_state`` key-value table: currently just the session/CSRF
signing secret, generated once on first use and reused for the life of the
database (design D11: changing the password revokes sessions; restarting
the process must not)."""

from __future__ import annotations

import secrets
import sqlite3

_SECRET_KEY = "secret"


def get_or_create_secret(conn: sqlite3.Connection) -> str:
    """Return the persistent signing secret, minting and storing one once
    if this database has never had one."""
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (_SECRET_KEY,)).fetchone()
    if row is not None:
        return str(row[0])
    value = secrets.token_hex(32)
    conn.execute("INSERT INTO app_state (key, value) VALUES (?, ?)", (_SECRET_KEY, value))
    conn.commit()
    return value


__all__ = ["get_or_create_secret"]
