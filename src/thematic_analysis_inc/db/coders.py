"""Coder CRUD with INTEGER ids. System rows (0, -1) are seeded by schema."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.connection import now


SYSTEM_AGGREGATOR_ID = 0
SYSTEM_REVIEWER_ID = -1


@dataclass
class Coder:
    coder_id: int
    name: str
    identity: str
    created_at: str


def _next_coder_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(coder_id), 0) AS m FROM coders WHERE coder_id >= 1"
    ).fetchone()
    return int(row["m"]) + 1


def add_coder(
    conn: sqlite3.Connection, name: str, identity: str
) -> Coder:
    """Insert a new real coder (id ≥ 1). Returns the new Coder row."""
    with conn:
        coder_id = _next_coder_id(conn)
        conn.execute(
            "INSERT INTO coders (coder_id, name, identity, created_at) "
            "VALUES (?, ?, ?, ?)",
            (coder_id, name, identity, now()),
        )
    return Coder(
        coder_id=coder_id, name=name, identity=identity, created_at=now()
    )


def get_coder(conn: sqlite3.Connection, coder_id: int) -> Coder | None:
    row = conn.execute(
        "SELECT coder_id, name, identity, created_at "
        "FROM coders WHERE coder_id = ?",
        (coder_id,),
    ).fetchone()
    if row is None:
        return None
    return Coder(**dict(row))


def get_coder_by_name(
    conn: sqlite3.Connection, name: str
) -> Coder | None:
    row = conn.execute(
        "SELECT coder_id, name, identity, created_at "
        "FROM coders WHERE name = ? AND coder_id >= 1 "
        "ORDER BY coder_id LIMIT 1",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return Coder(**dict(row))


def list_coders(conn: sqlite3.Connection) -> list[Coder]:
    """List real coders (id ≥ 1) ordered by id."""
    rows = conn.execute(
        "SELECT coder_id, name, identity, created_at FROM coders "
        "WHERE coder_id >= 1 ORDER BY coder_id"
    ).fetchall()
    return [Coder(**dict(r)) for r in rows]


def assert_real_coder(coder_id: int) -> None:
    if coder_id <= 0:
        raise ValueError(
            f"coder_id {coder_id} is a system id; only real coders (id ≥ 1) "
            "are allowed here"
        )
