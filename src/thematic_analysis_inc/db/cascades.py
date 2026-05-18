"""Cascade-delete helpers that respect FK relationships."""

from __future__ import annotations

import sqlite3


def _delete_codes_and_dependents(
    conn: sqlite3.Connection, code_ids: list[int]
) -> None:
    if not code_ids:
        return
    ph = ",".join("?" * len(code_ids))
    # Delete provenance edges that touch any of these codes.
    conn.execute(
        f"DELETE FROM codes_derived "
        f"WHERE new_code_id IN ({ph}) OR source_code_id IN ({ph})",
        code_ids + code_ids,
    )
    conn.execute(
        f"DELETE FROM codes_supporting_quotes WHERE code_id IN ({ph})",
        code_ids,
    )
    conn.execute(
        f"DELETE FROM codebook WHERE code_id IN ({ph})", code_ids
    )
    conn.execute(f"DELETE FROM codes WHERE code_id IN ({ph})", code_ids)


def _delete_quotes_for_segment(
    conn: sqlite3.Connection, segment_id: int
) -> None:
    qids = [
        r["quote_id"]
        for r in conn.execute(
            "SELECT quote_id FROM quotes WHERE segment_id = ?",
            (segment_id,),
        ).fetchall()
    ]
    if qids:
        ph = ",".join("?" * len(qids))
        conn.execute(
            f"DELETE FROM codes_supporting_quotes WHERE quote_id IN ({ph})",
            qids,
        )
        conn.execute(
            f"DELETE FROM quotes WHERE quote_id IN ({ph})", qids
        )


def delete_segment_cascade(
    conn: sqlite3.Connection, segment_id: int
) -> bool:
    """Delete a segment and every row that points back at it."""
    with conn:
        code_ids = [
            r["code_id"]
            for r in conn.execute(
                "SELECT code_id FROM codes WHERE segment_id = ?",
                (segment_id,),
            ).fetchall()
        ]
        _delete_codes_and_dependents(conn, code_ids)
        _delete_quotes_for_segment(conn, segment_id)
        conn.execute(
            "DELETE FROM coding_queue WHERE segment_id = ?", (segment_id,)
        )
        cur = conn.execute(
            "DELETE FROM segments WHERE segment_id = ?", (segment_id,)
        )
        return cur.rowcount > 0


def delete_document_cascade(
    conn: sqlite3.Connection, document_id: int
) -> tuple[bool, int]:
    """Delete a document and every segment it owns (with cascades).

    Returns (removed_document, segments_removed)."""
    with conn:
        seg_ids = [
            int(r["segment_id"])
            for r in conn.execute(
                "SELECT segment_id FROM segments WHERE document_id = ?",
                (document_id,),
            ).fetchall()
        ]
        for sid in seg_ids:
            code_ids = [
                r["code_id"]
                for r in conn.execute(
                    "SELECT code_id FROM codes WHERE segment_id = ?",
                    (sid,),
                ).fetchall()
            ]
            _delete_codes_and_dependents(conn, code_ids)
            _delete_quotes_for_segment(conn, sid)
            conn.execute(
                "DELETE FROM coding_queue WHERE segment_id = ?", (sid,)
            )
            conn.execute(
                "DELETE FROM segments WHERE segment_id = ?", (sid,)
            )
        cur = conn.execute(
            "DELETE FROM documents WHERE document_id = ?", (document_id,)
        )
        return cur.rowcount > 0, len(seg_ids)


def delete_coder_cascade(
    conn: sqlite3.Connection, coder_id: int, *, force: bool = False
) -> tuple[bool, int]:
    """Delete a real coder (id ≥ 1) and their codes/queue rows.

    If `force=False` and any queue rows exist, raises RuntimeError.
    Returns (removed, queue_rows_deleted)."""
    if coder_id <= 0:
        raise ValueError(
            f"refusing to delete system coder (coder_id={coder_id})"
        )
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM coding_queue WHERE coder_id = ?",
        (coder_id,),
    ).fetchone()["n"]
    if n > 0 and not force:
        raise RuntimeError(
            f"coder {coder_id} has {n} coding_queue rows; pass force=True "
            "to cascade"
        )
    with conn:
        code_ids = [
            r["code_id"]
            for r in conn.execute(
                "SELECT code_id FROM codes WHERE coder_id = ?", (coder_id,)
            ).fetchall()
        ]
        _delete_codes_and_dependents(conn, code_ids)
        cur_q = conn.execute(
            "DELETE FROM coding_queue WHERE coder_id = ?", (coder_id,)
        )
        cur = conn.execute(
            "DELETE FROM coders WHERE coder_id = ?", (coder_id,)
        )
        return cur.rowcount == 1, int(cur_q.rowcount)


def delete_aggregation_for_segment_cascade(
    conn: sqlite3.Connection, segment_id: int
) -> int:
    """Delete every aggregator code (and its downstream reviewer codes)
    for a segment. Returns count of aggregator codes deleted."""
    with conn:
        agg_ids = [
            r["code_id"]
            for r in conn.execute(
                "SELECT code_id FROM codes WHERE segment_id = ? AND coder_id = 0",
                (segment_id,),
            ).fetchall()
        ]
        # Find reviewer codes that came from these aggregator codes.
        rev_ids: list[int] = []
        if agg_ids:
            ph = ",".join("?" * len(agg_ids))
            rev_ids = [
                int(r["new_code_id"])
                for r in conn.execute(
                    f"SELECT new_code_id FROM codes_derived "
                    f"WHERE source_code_id IN ({ph}) "
                    f"  AND derivation_type = 'R'",
                    agg_ids,
                ).fetchall()
            ]
        _delete_codes_and_dependents(conn, rev_ids)
        _delete_codes_and_dependents(conn, agg_ids)
        # Clear any quotes that were created exclusively for these aggregator
        # codes and are no longer referenced.
        conn.execute(
            "DELETE FROM quotes WHERE segment_id = ? "
            "AND quote_id NOT IN (SELECT quote_id FROM codes_supporting_quotes)",
            (segment_id,),
        )
        return len(agg_ids)


def reset_coding_assignment(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    coder_id: int,
) -> bool:
    """Drop this coder's codes for the segment, clear queue claim, and
    cascade-delete any aggregator/reviewer codes for the segment."""
    with conn:
        code_ids = [
            r["code_id"]
            for r in conn.execute(
                "SELECT code_id FROM codes WHERE segment_id = ? AND coder_id = ?",
                (segment_id, coder_id),
            ).fetchall()
        ]
        _delete_codes_and_dependents(conn, code_ids)
        # Reset the queue row.
        cur = conn.execute(
            "UPDATE coding_queue "
            "SET claimed_at = NULL, finished_at = NULL, error = NULL "
            "WHERE segment_id = ? AND coder_id = ?",
            (segment_id, coder_id),
        )
        # Aggregator output is now stale; nuke it (and downstream review).
        delete_aggregation_for_segment_cascade(conn, segment_id)
        return cur.rowcount > 0


def reset_failed_assignments(
    conn: sqlite3.Connection, coder_id: int
) -> int:
    """Clear `error` (and reset claim) for failed queue rows for this coder."""
    cur = conn.execute(
        "UPDATE coding_queue "
        "SET claimed_at = NULL, finished_at = NULL, error = NULL "
        "WHERE coder_id = ? AND error IS NOT NULL",
        (coder_id,),
    )
    return int(cur.rowcount)


def reset_all_assignments(
    conn: sqlite3.Connection, coder_id: int
) -> int:
    """For every queue row for this coder, drop their codes and reset claim."""
    with conn:
        code_ids = [
            r["code_id"]
            for r in conn.execute(
                "SELECT code_id FROM codes WHERE coder_id = ?", (coder_id,)
            ).fetchall()
        ]
        _delete_codes_and_dependents(conn, code_ids)
        cur = conn.execute(
            "UPDATE coding_queue "
            "SET claimed_at = NULL, finished_at = NULL, error = NULL "
            "WHERE coder_id = ?",
            (coder_id,),
        )
        return int(cur.rowcount)
