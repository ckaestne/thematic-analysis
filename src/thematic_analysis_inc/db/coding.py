"""Coding queue + Stage-A coder codes."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.connection import now


@dataclass
class CodingAssignment:
    segment_id: int
    coder_id: int
    codebook_version: int
    content: str


@dataclass
class CoderCode:
    code_id: int
    segment_id: int
    coder_id: int
    code: str
    description: str
    rationale: str


@dataclass
class CoderCodes:
    coder_id: int
    codes: list[CoderCode]


def sync_coding_queue(conn: sqlite3.Connection) -> int:
    """Ensure a `coding_queue` row exists for every (segment, real-coder) at
    the latest codebook version. Returns rows inserted."""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM codebook_versions"
    ).fetchone()
    if row is None or row["v"] is None:
        return 0
    version = int(row["v"])
    cur = conn.execute(
        "INSERT OR IGNORE INTO coding_queue "
        "(segment_id, coder_id, codebook_version) "
        "SELECT s.segment_id, c.coder_id, ? "
        "FROM segments s CROSS JOIN coders c "
        "WHERE c.coder_id >= 1",
        (version,),
    )
    return cur.rowcount or 0


def pending_count(conn: sqlite3.Connection, coder_id: int) -> int:
    """Count pending rows for a coder (claimed_at IS NULL)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue "
        "WHERE coder_id = ? AND claimed_at IS NULL AND finished_at IS NULL "
        "AND error IS NULL",
        (coder_id,),
    ).fetchone()
    return int(row["n"])


def claim_next_coding_assignment(
    conn: sqlite3.Connection, coder_id: int
) -> CodingAssignment | None:
    """Atomically claim the next pending row for `coder_id`. Returns None if
    nothing is pending."""
    while True:
        row = conn.execute(
            "SELECT segment_id, codebook_version FROM coding_queue "
            "WHERE coder_id = ? AND claimed_at IS NULL "
            "  AND finished_at IS NULL AND error IS NULL "
            "ORDER BY segment_id LIMIT 1",
            (coder_id,),
        ).fetchone()
        if row is None:
            return None
        seg_id = int(row["segment_id"])
        version = int(row["codebook_version"])
        # Conditional UPDATE for atomic claim.
        cur = conn.execute(
            "UPDATE coding_queue SET claimed_at = ? "
            "WHERE segment_id = ? AND coder_id = ? AND claimed_at IS NULL",
            (now(), seg_id, coder_id),
        )
        if cur.rowcount == 0:
            # Race: someone else grabbed it; try the next.
            continue
        seg = conn.execute(
            "SELECT content FROM segments WHERE segment_id = ?",
            (seg_id,),
        ).fetchone()
        if seg is None:
            # Segment vanished; skip.
            continue
        return CodingAssignment(
            segment_id=seg_id,
            coder_id=coder_id,
            codebook_version=version,
            content=seg["content"],
        )


def record_coding_result(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    coder_id: int,
    version: int,
    codes: list[str],
    rationales: list[str],
) -> list[int]:
    """Insert codes for a finished coding assignment and mark queue done.

    Stage-A codes carry empty description and no quote links (the coder
    agent doesn't emit those yet). Returns the new code_ids in order.
    """
    new_ids: list[int] = []
    with conn:
        for i, code in enumerate(codes):
            rat = rationales[i] if i < len(rationales) else ""
            cur = conn.execute(
                "INSERT INTO codes "
                "(segment_id, coder_id, version, code, description, rationale) "
                "VALUES (?, ?, ?, ?, '', ?)",
                (segment_id, coder_id, version, code, rat),
            )
            new_ids.append(int(cur.lastrowid))
        conn.execute(
            "UPDATE coding_queue SET finished_at = ? "
            "WHERE segment_id = ? AND coder_id = ?",
            (now(), segment_id, coder_id),
        )
    return new_ids


def record_coding_failure(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    coder_id: int,
    error: str,
) -> None:
    conn.execute(
        "UPDATE coding_queue SET error = ?, finished_at = ? "
        "WHERE segment_id = ? AND coder_id = ?",
        (error, now(), segment_id, coder_id),
    )


def load_segment_coder_codes(
    conn: sqlite3.Connection, segment_id: int
) -> list[CoderCodes]:
    """Load Stage-A codes for the segment, grouped by coder."""
    rows = conn.execute(
        "SELECT code_id, segment_id, coder_id, code, description, rationale "
        "FROM codes WHERE segment_id = ? AND coder_id >= 1 "
        "ORDER BY coder_id, code_id",
        (segment_id,),
    ).fetchall()
    out: dict[int, list[CoderCode]] = {}
    for r in rows:
        out.setdefault(r["coder_id"], []).append(
            CoderCode(
                code_id=r["code_id"],
                segment_id=r["segment_id"],
                coder_id=r["coder_id"],
                code=r["code"],
                description=r["description"] or "",
                rationale=r["rationale"] or "",
            )
        )
    return [
        CoderCodes(coder_id=cid, codes=codes)
        for cid, codes in sorted(out.items())
    ]


def queue_status_char(row: sqlite3.Row) -> str:
    if row["error"] is not None:
        return "failed"
    if row["finished_at"] is not None:
        return "done"
    if row["claimed_at"] is not None:
        return "running"
    return "pending"


def list_coder_progress(
    conn: sqlite3.Connection, coder_id: int
) -> dict[str, int]:
    rows = conn.execute(
        "SELECT claimed_at, finished_at, error FROM coding_queue "
        "WHERE coder_id = ?",
        (coder_id,),
    ).fetchall()
    out = {"done": 0, "running": 0, "failed": 0, "pending": 0}
    for r in rows:
        out[queue_status_char(r)] = out.get(queue_status_char(r), 0) + 1
    return out


def list_queue_rows(
    conn: sqlite3.Connection,
    *,
    coder_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    where: list[str] = []
    params: list = []
    if coder_id is not None:
        where.append("coder_id = ?")
        params.append(coder_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM coding_queue {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT segment_id, coder_id, codebook_version, claimed_at, "
        f"  finished_at, error, "
        f"  (SELECT COUNT(*) FROM codes "
        f"   WHERE codes.segment_id = coding_queue.segment_id "
        f"     AND codes.coder_id = coding_queue.coder_id) AS n_codes "
        f"FROM coding_queue {clause} "
        f"ORDER BY segment_id DESC, coder_id LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["status"] = queue_status_char(r)
        items.append(d)
    return int(total), items


def get_queue_row(
    conn: sqlite3.Connection, segment_id: int, coder_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT claimed_at, finished_at, error FROM coding_queue "
        "WHERE segment_id = ? AND coder_id = ?",
        (segment_id, coder_id),
    ).fetchone()


def get_review_edge_for_aggregator_code(
    conn: sqlite3.Connection, agg_code_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT new_code_id, decision, rationale FROM codes_derived "
        "WHERE source_code_id = ? AND derivation_type = 'R'",
        (agg_code_id,),
    ).fetchone()


def edit_code_text(
    conn: sqlite3.Connection, code_id: int, new_code: str
) -> bool:
    cur = conn.execute(
        "UPDATE codes SET code = ? WHERE code_id = ?", (new_code, code_id)
    )
    return cur.rowcount > 0
