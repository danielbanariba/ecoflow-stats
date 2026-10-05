"""The ``devices`` table store: identity, resolved adapter, and the last
``device/list`` online flag for every configured device."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DeviceRecord:
    """One row of the ``devices`` table."""

    id: int
    sn: str
    name: str | None
    product_name: str | None
    adapter_id: str
    online: bool | None
    online_checked_at: int | None
    created_at: int


_COLUMNS = "id, sn, name, product_name, adapter_id, online, online_checked_at, created_at"


def _row_to_record(row: sqlite3.Row) -> DeviceRecord:
    return DeviceRecord(
        id=row["id"],
        sn=row["sn"],
        name=row["name"],
        product_name=row["product_name"],
        adapter_id=row["adapter_id"],
        online=None if row["online"] is None else bool(row["online"]),
        online_checked_at=row["online_checked_at"],
        created_at=row["created_at"],
    )


class DeviceStore:
    """Synchronous ``devices`` table access. Callers from async code offload
    through ``anyio.to_thread.run_sync``; this store has no I/O of its own
    beyond the sqlite3 connection it is given."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        *,
        sn: str,
        adapter_id: str,
        created_at: int,
        name: str | None = None,
        product_name: str | None = None,
    ) -> DeviceRecord:
        """Insert a new device, or update an already-known one's adapter and
        any newly-supplied name/product — re-running startup for the same
        configured serial must never create a second row."""
        row = self._conn.execute(
            f"""
            INSERT INTO devices (sn, name, product_name, adapter_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sn) DO UPDATE SET
                adapter_id = excluded.adapter_id,
                name = COALESCE(excluded.name, devices.name),
                product_name = COALESCE(excluded.product_name, devices.product_name)
            RETURNING {_COLUMNS}
            """,
            (sn, name, product_name, adapter_id, created_at),
        ).fetchone()
        self._conn.commit()
        return _row_to_record(row)

    def get_by_sn(self, sn: str) -> DeviceRecord | None:
        row = self._conn.execute(f"SELECT {_COLUMNS} FROM devices WHERE sn = ?", (sn,)).fetchone()
        return _row_to_record(row) if row is not None else None

    def list_all(self) -> list[DeviceRecord]:
        rows = self._conn.execute(f"SELECT {_COLUMNS} FROM devices ORDER BY id").fetchall()
        return [_row_to_record(row) for row in rows]

    def set_online(self, device_id: int, online: bool | None, checked_at: int) -> None:
        self._conn.execute(
            "UPDATE devices SET online = ?, online_checked_at = ? WHERE id = ?",
            (None if online is None else int(online), checked_at, device_id),
        )
        self._conn.commit()


__all__ = ["DeviceRecord", "DeviceStore"]
