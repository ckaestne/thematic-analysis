"""Document, segment, quote tables."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from thematic_analysis_inc.db.connection import now


@dataclass
class Document:
    document_id: int
    filename: str
    created_at: str
    size_bytes: int


@dataclass
class Segment:
    segment_id: int
    document_id: int | None
    content: str
    line_from: int | None
    line_to: int | None
    position: int | None


@dataclass
class EnqueueResult:
    inserted_segments: int


def add_document(
    conn: sqlite3.Connection, filename: str, content: bytes
) -> int:
    cur = conn.execute(
        "INSERT INTO documents (filename, content, created_at) "
        "VALUES (?, ?, ?)",
        (filename, content, now()),
    )
    assert cur.lastrowid is not None
    return int(cur.lastrowid)


def find_document_id_by_filename(
    conn: sqlite3.Connection, filename: str
) -> int | None:
    row = conn.execute(
        "SELECT document_id FROM documents WHERE filename = ? "
        "ORDER BY document_id ASC LIMIT 1",
        (filename,),
    ).fetchone()
    return None if row is None else int(row["document_id"])


def list_documents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT d.document_id, d.filename, d.created_at, "
        "  length(d.content) AS size_bytes, "
        "  COUNT(s.segment_id) AS segments_total "
        "FROM documents d "
        "LEFT JOIN segments s ON s.document_id = d.document_id "
        "GROUP BY d.document_id "
        "ORDER BY d.document_id DESC"
    ).fetchall()


def get_document_meta(
    conn: sqlite3.Connection, document_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT document_id, filename, created_at, length(content) AS size_bytes "
        "FROM documents WHERE document_id = ?",
        (document_id,),
    ).fetchone()


def get_document_segments(
    conn: sqlite3.Connection, document_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT segment_id, content, line_from, line_to, position, "
        "  length(content) AS len "
        "FROM segments WHERE document_id = ? "
        "ORDER BY position IS NULL, position, segment_id",
        (document_id,),
    ).fetchall()


def enqueue_segments(
    conn: sqlite3.Connection,
    segments: Iterable[tuple],
) -> EnqueueResult:
    """Insert segments. Each entry is one of:
        (document_id, content)
        (document_id, content, line_from, line_to)
        (document_id, content, line_from, line_to, position)

    Returns count inserted. Segment ids are auto-assigned INTEGERs.
    """
    inserted = 0
    with conn:
        for entry in segments:
            if len(entry) == 2:
                document_id, content = entry
                line_from = line_to = position = None
            elif len(entry) == 4:
                document_id, content, line_from, line_to = entry
                position = None
            else:
                document_id, content, line_from, line_to, position = entry
            conn.execute(
                "INSERT INTO segments "
                "(document_id, content, line_from, line_to, position) "
                "VALUES (?, ?, ?, ?, ?)",
                (document_id, content, line_from, line_to, position),
            )
            inserted += 1
    return EnqueueResult(inserted_segments=inserted)


def get_segment(
    conn: sqlite3.Connection, segment_id: int
) -> Segment | None:
    row = conn.execute(
        "SELECT segment_id, document_id, content, line_from, line_to, position "
        "FROM segments WHERE segment_id = ?",
        (segment_id,),
    ).fetchone()
    if row is None:
        return None
    return Segment(**dict(row))


def list_segments(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[sqlite3.Row]]:
    where: list[str] = []
    params: list = []
    if q:
        where.append("(CAST(segment_id AS TEXT) LIKE ? OR content LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM segments {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT segment_id, document_id, line_from, line_to, position, "
        f"  substr(content, 1, 240) AS preview, length(content) AS len "
        f"FROM segments {clause} ORDER BY segment_id "
        f"LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return int(total), rows


def add_quote(
    conn: sqlite3.Connection, segment_id: int, text: str
) -> int:
    cur = conn.execute(
        "INSERT INTO quotes (segment_id, text) VALUES (?, ?)",
        (segment_id, text),
    )
    return int(cur.lastrowid)


def link_code_quote(
    conn: sqlite3.Connection, code_id: int, quote_id: int
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO codes_supporting_quotes "
        "(code_id, quote_id) VALUES (?, ?)",
        (code_id, quote_id),
    )
