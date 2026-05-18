"""Codebook versions, membership, and JSON hydration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.connection import now


@dataclass
class CodebookVersion:
    version: int
    parent_version: int | None
    created_by: str
    created_at: str


@dataclass
class CodebookEntry:
    code_id: int
    code: str
    description: str
    rationale: str
    quotes: list[dict]  # [{quote_id, text}, ...]


def insert_codebook_version(
    conn: sqlite3.Connection,
    *,
    parent: int | None,
    created_by: str,
) -> int:
    """Insert a new codebook_versions row. Returns the new version id."""
    cur = conn.execute(
        "INSERT INTO codebook_versions "
        "(parent_version, created_by, created_at) "
        "VALUES (?, ?, ?)",
        (parent, created_by, now()),
    )
    return int(cur.lastrowid)


def latest_codebook_version(
    conn: sqlite3.Connection,
) -> CodebookVersion | None:
    row = conn.execute(
        "SELECT version, parent_version, created_by, created_at "
        "FROM codebook_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return CodebookVersion(**dict(row))


def get_codebook_version(
    conn: sqlite3.Connection, version: int
) -> CodebookVersion | None:
    row = conn.execute(
        "SELECT version, parent_version, created_by, created_at "
        "FROM codebook_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        return None
    return CodebookVersion(**dict(row))


def list_codebook_versions(
    conn: sqlite3.Connection,
) -> list[CodebookVersion]:
    rows = conn.execute(
        "SELECT version, parent_version, created_by, created_at "
        "FROM codebook_versions ORDER BY version ASC"
    ).fetchall()
    return [CodebookVersion(**dict(r)) for r in rows]


def get_codebook_codes(
    conn: sqlite3.Connection, version: int
) -> list[CodebookEntry]:
    """Return the codes belonging to a codebook version, with their quotes."""
    rows = conn.execute(
        "SELECT c.code_id, c.code, c.description, c.rationale "
        "FROM codebook cb "
        "JOIN codes c ON c.code_id = cb.code_id "
        "WHERE cb.version = ? "
        "ORDER BY c.code_id",
        (version,),
    ).fetchall()
    out: list[CodebookEntry] = []
    for r in rows:
        qrows = conn.execute(
            "SELECT q.quote_id, q.text "
            "FROM codes_supporting_quotes csq "
            "JOIN quotes q ON q.quote_id = csq.quote_id "
            "WHERE csq.code_id = ? "
            "ORDER BY q.quote_id",
            (r["code_id"],),
        ).fetchall()
        out.append(
            CodebookEntry(
                code_id=r["code_id"],
                code=r["code"],
                description=r["description"] or "",
                rationale=r["rationale"] or "",
                quotes=[
                    {"quote_id": str(q["quote_id"]), "text": q["text"]}
                    for q in qrows
                ],
            )
        )
    return out


def codebook_to_json_for_version(
    conn: sqlite3.Connection, version: int
) -> str:
    """Serialize the codebook as the legacy `{"codes": [...]}` JSON shape."""
    entries = get_codebook_codes(conn, version)
    return json.dumps(
        {
            "codes": [
                {"code": e.code, "quotes": e.quotes}
                for e in entries
            ]
        },
        indent=2,
    )


def codebook_add_code(
    conn: sqlite3.Connection, version: int, code_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO codebook (version, code_id) VALUES (?, ?)",
        (version, code_id),
    )


def copy_codebook_membership(
    conn: sqlite3.Connection,
    *,
    from_version: int,
    to_version: int,
    drop_code_id: int | None = None,
    add_code_id: int | None = None,
) -> None:
    """Copy `codebook` rows from one version to another.

    `drop_code_id`: skip this code (used for UPDATE which replaces).
    `add_code_id`: also append this code (used for ADD / UPDATE).
    """
    rows = conn.execute(
        "SELECT code_id FROM codebook WHERE version = ?", (from_version,)
    ).fetchall()
    for r in rows:
        cid = r["code_id"]
        if drop_code_id is not None and cid == drop_code_id:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO codebook (version, code_id) VALUES (?, ?)",
            (to_version, cid),
        )
    if add_code_id is not None:
        conn.execute(
            "INSERT OR IGNORE INTO codebook (version, code_id) VALUES (?, ?)",
            (to_version, add_code_id),
        )
