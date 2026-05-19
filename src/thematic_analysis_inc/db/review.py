"""Reviewer (reserved coder_id = -1) helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.codebook import (
    copy_codebook_membership,
    insert_codebook_version,
)
from thematic_analysis_inc.db.coders import SYSTEM_REVIEWER_ID
from thematic_analysis_inc.db.research_context import (
    latest_research_context_version,
)


# Single-char decisions stored in codes_derived.decision when
# derivation_type='R'. SKIP is never persisted.
DECISION_ADD = "A"
DECISION_MERGE = "M"
DECISION_UPDATE = "U"


@dataclass
class ReviewableAggregatorCode:
    code_id: int
    segment_id: int
    code: str


def next_aggregated_code_to_review(
    conn: sqlite3.Connection,
) -> ReviewableAggregatorCode | None:
    row = conn.execute(
        "SELECT c.code_id, c.segment_id, c.code "
        "FROM codes c "
        "WHERE c.coder_id = 0 "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM codes_derived d "
        "    WHERE d.source_code_id = c.code_id AND d.derivation_type = 'R'"
        "  ) "
        "ORDER BY c.code_id LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return ReviewableAggregatorCode(
        code_id=int(row["code_id"]),
        segment_id=int(row["segment_id"]),
        code=row["code"],
    )


def resolve_target_code_id(
    conn: sqlite3.Connection, *, version: int, code_text: str
) -> int | None:
    """Find the reviewer code_id with given text that belongs to codebook
    `version`. Returns None if no match exists."""
    row = conn.execute(
        "SELECT c.code_id FROM codes c "
        "JOIN codebook cb ON cb.code_id = c.code_id AND cb.version = ? "
        "WHERE c.code = ? "
        "ORDER BY c.code_id DESC LIMIT 1",
        (version, code_text),
    ).fetchone()
    return None if row is None else int(row["code_id"])


def record_review(
    conn: sqlite3.Connection,
    *,
    source_agg_code_id: int,
    decision: str,
    new_code_text: str,
    new_description: str,
    rationale: str,
    parent_version: int,
    target_code_id: int | None = None,
) -> int:
    """Persist a non-SKIP review decision.

    For ADD: inserts a new reviewer code and adds it to a new codebook
    version copied from `parent_version`.
    For MERGE: inserts a new reviewer code (same text as target), adds
    a `codes_derived('R','M')` edge, and copies the codebook unchanged.
    For UPDATE: inserts a new reviewer code, copies codebook with the
    target replaced by the new code.

    Returns the new codebook version.
    """
    if decision not in {DECISION_ADD, DECISION_MERGE, DECISION_UPDATE}:
        raise ValueError(f"invalid decision: {decision!r}")
    if decision in (DECISION_MERGE, DECISION_UPDATE) and target_code_id is None:
        raise ValueError(
            f"decision {decision!r} requires target_code_id"
        )

    rc_version = latest_research_context_version()
    with conn:
        cur = conn.execute(
            "INSERT INTO codes "
            "(segment_id, coder_id, codebook_version, "
            " research_context_version, code, description, rationale) "
            "VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (
                SYSTEM_REVIEWER_ID,
                parent_version,
                rc_version,
                new_code_text,
                new_description or "",
                rationale or "",
            ),
        )
        new_code_id = int(cur.lastrowid)

        conn.execute(
            "INSERT INTO codes_derived "
            "(new_code_id, source_code_id, derivation_type, decision, rationale) "
            "VALUES (?, ?, 'R', ?, ?)",
            (new_code_id, source_agg_code_id, decision, rationale),
        )

        new_version = insert_codebook_version(
            conn, parent=parent_version, created_by="reviewer"
        )

        if decision == DECISION_ADD:
            copy_codebook_membership(
                conn,
                from_version=parent_version,
                to_version=new_version,
                add_code_id=new_code_id,
            )
        elif decision == DECISION_MERGE:
            # Keep the target code; the new reviewer code represents the
            # merge event but isn't added to the codebook.
            copy_codebook_membership(
                conn,
                from_version=parent_version,
                to_version=new_version,
            )
        else:  # UPDATE
            copy_codebook_membership(
                conn,
                from_version=parent_version,
                to_version=new_version,
                drop_code_id=target_code_id,
                add_code_id=new_code_id,
            )

    return new_version


def list_review_decisions(
    conn: sqlite3.Connection,
    *,
    decision: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    where: list[str] = ["d.derivation_type = 'R'"]
    params: list = []
    if decision is not None:
        where.append("d.decision = ?")
        params.append(decision)
    clause = "WHERE " + " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM codes_derived d {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT d.new_code_id, d.source_code_id, d.decision, d.rationale, "
        f"  src.code AS source_code, src.segment_id, "
        f"  new_c.code AS new_code "
        f"FROM codes_derived d "
        f"JOIN codes src ON src.code_id = d.source_code_id "
        f"JOIN codes new_c ON new_c.code_id = d.new_code_id "
        f"{clause} "
        f"ORDER BY d.new_code_id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    ).fetchall()
    return int(total), [dict(r) for r in rows]


def find_reviewer_code_by_text(
    conn: sqlite3.Connection, code_text: str
) -> int | None:
    """Latest reviewer code with the given text (used to link quotes to a
    newly-inserted reviewer code)."""
    row = conn.execute(
        "SELECT code_id FROM codes WHERE code = ? AND coder_id = -1 "
        "ORDER BY code_id DESC LIMIT 1",
        (code_text,),
    ).fetchone()
    return None if row is None else int(row["code_id"])


def segment_review_remaining(
    conn: sqlite3.Connection, segment_id: int
) -> int:
    """Count aggregator codes on this segment without a review edge."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM codes c "
        "WHERE c.segment_id = ? AND c.coder_id = 0 "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM codes_derived d "
        "    WHERE d.source_code_id = c.code_id AND d.derivation_type = 'R'"
        "  )",
        (segment_id,),
    ).fetchone()
    return int(row["n"])
