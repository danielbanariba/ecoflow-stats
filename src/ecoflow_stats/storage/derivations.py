"""The ``derivations`` table: per-device, per-named-derivation bookkeeping
(algorithm version, parameter hash, incremental checkpoint, and dirty
marker) that lets a recompute resume from the right point instead of
starting over every time (design-data section 1 and 4.3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Derivation:
    """One ``derivations`` row: where a named derivation (``outages``,
    ``rollups``) for one device currently stands."""

    device_id: int
    name: str
    version: int
    params_hash: str
    checkpoint_ts: int | None
    dirty_from_ts: int | None
    computed_at: int


def _row_to_derivation(row: sqlite3.Row) -> Derivation:
    return Derivation(
        device_id=row["device_id"],
        name=row["name"],
        version=row["version"],
        params_hash=row["params_hash"],
        checkpoint_ts=row["checkpoint_ts"],
        dirty_from_ts=row["dirty_from_ts"],
        computed_at=row["computed_at"],
    )


class DerivationStore:
    """Synchronous ``derivations`` table access.

    ``mark_computed`` never commits on its own: it is always called from
    inside the same ``BEGIN IMMEDIATE`` transaction that also replaces the
    derivation's own output rows (design D4), and the caller commits once,
    after both writes succeed. ``mark_dirty`` is a standalone write (a
    collector tick or an import marking new work) and commits itself.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, device_id: int, name: str) -> Derivation | None:
        row = self._conn.execute(
            "SELECT device_id, name, version, params_hash, checkpoint_ts,"
            " dirty_from_ts, computed_at FROM derivations"
            " WHERE device_id = ? AND name = ?",
            (device_id, name),
        ).fetchone()
        return _row_to_derivation(row) if row is not None else None

    def mark_computed(
        self,
        device_id: int,
        name: str,
        *,
        version: int,
        params_hash: str,
        checkpoint_ts: int | None,
        computed_at: int,
    ) -> None:
        """Record the result of a completed (full or incremental) run:
        clears ``dirty_from_ts`` and advances the checkpoint and version.
        Does not commit -- see the class docstring."""
        self._conn.execute(
            "INSERT INTO derivations"
            " (device_id, name, version, params_hash, checkpoint_ts, dirty_from_ts, computed_at)"
            " VALUES (?, ?, ?, ?, ?, NULL, ?)"
            " ON CONFLICT (device_id, name) DO UPDATE SET"
            " version = excluded.version,"
            " params_hash = excluded.params_hash,"
            " checkpoint_ts = excluded.checkpoint_ts,"
            " dirty_from_ts = NULL,"
            " computed_at = excluded.computed_at",
            (device_id, name, version, params_hash, checkpoint_ts, computed_at),
        )

    def mark_dirty(self, device_id: int, name: str, from_ts: int) -> None:
        """Mark a derivation dirty from ``from_ts`` onward -- the earliest
        point that now needs recomputation (a new sample, an import, or a
        parameter change). Moves ``dirty_from_ts`` earlier when it is
        already dirty from a later point, and leaves it alone when it is
        already dirty from an earlier or equal point: a later, narrower
        dirty mark must never hide an earlier one still waiting."""
        existing = self.get(device_id, name)
        if existing is None:
            self._conn.execute(
                "INSERT INTO derivations"
                " (device_id, name, version, params_hash, checkpoint_ts,"
                " dirty_from_ts, computed_at)"
                " VALUES (?, ?, 0, '', NULL, ?, 0)",
                (device_id, name, from_ts),
            )
            self._conn.commit()
            return
        if existing.dirty_from_ts is None or from_ts < existing.dirty_from_ts:
            self._conn.execute(
                "UPDATE derivations SET dirty_from_ts = ? WHERE device_id = ? AND name = ?",
                (from_ts, device_id, name),
            )
            self._conn.commit()


__all__ = ["Derivation", "DerivationStore"]
