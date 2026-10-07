"""The ``app_state`` key-value table: the session/CSRF signing secret
(generated once on first use and reused for the life of the database),
plus `/logout`'s session-revocation generation counter -- both must
survive a restart (design D11: changing the password revokes sessions;
restarting the process must not), so both live here rather than only
in memory."""

from __future__ import annotations

import secrets
import sqlite3

_SECRET_KEY = "secret"
_SESSION_GENERATION_KEY = "session_generation"


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


def get_session_generation(conn: sqlite3.Connection) -> int:
    """Return the persisted session-revocation generation (SEC-07), or
    `0` if this database has never recorded one -- the same default
    `web.security.SecurityContext.session_generation` already starts
    from."""
    row = conn.execute(
        "SELECT value FROM app_state WHERE key = ?", (_SESSION_GENERATION_KEY,)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def set_session_generation(conn: sqlite3.Connection, generation: int) -> None:
    """Persist `generation` (SEC-07), so a later restart's first read
    via `get_session_generation` continues from the value `/logout`
    last bumped it to, instead of silently resetting to `0` and
    un-revoking every session that bump had just revoked."""
    conn.execute(
        "INSERT INTO app_state (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (_SESSION_GENERATION_KEY, str(generation)),
    )
    conn.commit()


__all__ = ["get_or_create_secret", "get_session_generation", "set_session_generation"]
