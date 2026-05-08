"""Data access layer for the incremental Stage 1 pipeline.

These functions are thin SQL wrappers — no business logic, no LLM calls.
Workers compose these primitives.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from thematic_analysis_inc.schema import create_schema


EMPTY_CODEBOOK_JSON = json.dumps({"codes": []}, indent=2)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with WAL + foreign keys + row factory."""
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    create_schema(conn)
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    """Open the DB, apply schema, ensure codebook v1 (empty) exists."""
    conn = connect(path)
    create_schema(conn)
    if latest_codebook_version(conn) is None:
        insert_codebook_version(
            conn, EMPTY_CODEBOOK_JSON, parent=None, created_by="init"
        )
    return conn


# ---------------------------------------------------------------------------
# Codebook versions
# ---------------------------------------------------------------------------


@dataclass
class CodebookVersion:
    version: int
    parent_version: int | None
    snapshot_json: str
    created_by: str
    created_at: str


def latest_codebook_version(conn: sqlite3.Connection) -> CodebookVersion | None:
    row = conn.execute(
        "SELECT version, parent_version, snapshot_json, created_by, created_at "
        "FROM codebook_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return CodebookVersion(**dict(row))


def get_codebook_version(
    conn: sqlite3.Connection, version: int
) -> CodebookVersion | None:
    row = conn.execute(
        "SELECT version, parent_version, snapshot_json, created_by, created_at "
        "FROM codebook_versions WHERE version = ?",
        (version,),
    ).fetchone()
    if row is None:
        return None
    return CodebookVersion(**dict(row))


def insert_codebook_version(
    conn: sqlite3.Connection,
    snapshot_json: str,
    parent: int | None,
    created_by: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO codebook_versions "
        "(parent_version, snapshot_json, created_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (parent, snapshot_json, created_by, _now()),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Coders
# ---------------------------------------------------------------------------


@dataclass
class Coder:
    coder_id: str
    identity: str
    created_at: str


def add_coder(conn: sqlite3.Connection, coder_id: str, identity: str) -> bool:
    """Insert a coder. Returns True if newly inserted, False if id existed."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO coders (coder_id, identity, created_at) "
        "VALUES (?, ?, ?)",
        (coder_id, identity, _now()),
    )
    return cur.rowcount == 1


def remove_coder(conn: sqlite3.Connection, coder_id: str) -> bool:
    """Delete a coder. Returns True if a row was removed.

    Refuses if the coder has any coder_runs (caller must clear those first).
    """
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM coder_runs WHERE coder_id = ?", (coder_id,)
    ).fetchone()["n"]
    if n > 0:
        raise RuntimeError(
            f"coder '{coder_id}' has {n} coder_runs; refuse to remove"
        )
    cur = conn.execute("DELETE FROM coders WHERE coder_id = ?", (coder_id,))
    return cur.rowcount == 1


def get_coder(conn: sqlite3.Connection, coder_id: str) -> Coder | None:
    row = conn.execute(
        "SELECT coder_id, identity, created_at FROM coders WHERE coder_id = ?",
        (coder_id,),
    ).fetchone()
    if row is None:
        return None
    return Coder(**dict(row))


def list_coders(conn: sqlite3.Connection) -> list[Coder]:
    rows = conn.execute(
        "SELECT coder_id, identity, created_at FROM coders ORDER BY coder_id"
    ).fetchall()
    return [Coder(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


@dataclass
class EnqueueResult:
    inserted_segments: int
    skipped_segments: int


def enqueue_segments(
    conn: sqlite3.Connection,
    segments: Iterable[tuple[str, str]],
    batch: int | None = None,
) -> EnqueueResult:
    """Insert segments. Idempotent on segment_id. No coder_runs created."""
    inserted = 0
    skipped = 0
    try:
        conn.execute("BEGIN")
        for segment_id, text in segments:
            cur = conn.execute(
                "INSERT OR IGNORE INTO segments (segment_id, text, batch, status) "
                "VALUES (?, ?, ?, 'pending')",
                (segment_id, text, batch),
            )
            if cur.rowcount == 0:
                skipped += 1
            else:
                inserted += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return EnqueueResult(inserted_segments=inserted, skipped_segments=skipped)


# ---------------------------------------------------------------------------
# Coder runs (created on-the-fly when a coder starts work on a segment)
# ---------------------------------------------------------------------------


def segments_to_code(
    conn: sqlite3.Connection, coder_id: str
) -> list[sqlite3.Row]:
    """Segments with no coder_run row yet for this coder."""
    return conn.execute(
        "SELECT s.segment_id, s.text FROM segments s "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM coder_runs cr "
        "  WHERE cr.segment_id = s.segment_id AND cr.coder_id = ?"
        ") ORDER BY s.segment_id",
        (coder_id,),
    ).fetchall()


def start_coder_run(
    conn: sqlite3.Connection,
    segment_id: str,
    coder_id: str,
    codebook_version: int,
) -> int | None:
    """Insert a 'running' coder_run row. Returns the new id, or None if a
    row already exists for (segment_id, coder_id) (race / retry case)."""
    try:
        cur = conn.execute(
            "INSERT INTO coder_runs "
            "(segment_id, coder_id, codebook_version, status, claimed_at) "
            "VALUES (?, ?, ?, 'running', ?)",
            (segment_id, coder_id, codebook_version, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    if cur.lastrowid is None:
        return None
    conn.execute(
        "UPDATE segments SET status='coding' "
        "WHERE segment_id = ? AND status = 'pending'",
        (segment_id,),
    )
    return int(cur.lastrowid)


def record_coder_result(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    codes: list[str],
    rationales: list[str],
    is_new: list[bool],
    raw_response: str | None,
) -> None:
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE coder_runs SET status='done', finished_at=?, raw_response=? "
            "WHERE id = ?",
            (_now(), raw_response, run_id),
        )
        for i, code in enumerate(codes):
            rat = rationales[i] if i < len(rationales) else None
            isn = is_new[i] if i < len(is_new) else None
            conn.execute(
                "INSERT INTO coder_codes "
                "(coder_run_id, position, code, rationale, is_new) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    i,
                    code,
                    rat,
                    None if isn is None else (1 if isn else 0),
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_coder_failure(
    conn: sqlite3.Connection, run_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE coder_runs SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (_now(), error, run_id),
    )


def reset_unfinished_coder_runs(
    conn: sqlite3.Connection, coder_id: str
) -> int:
    """Delete failed/running rows for this coder so they will be re-coded
    next time `code <coder_id>` runs. Returns rows deleted."""
    cur = conn.execute(
        "DELETE FROM coder_runs "
        "WHERE coder_id = ? AND status IN ('failed', 'running')",
        (coder_id,),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


@dataclass
class CoderRunResult:
    coder_id: str
    codes: list[str]
    rationales: list[str]
    is_new: list[bool]


def next_segment_to_aggregate(
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    """Return the next segment for which every registered coder has a 'done'
    coder_run, and which has no aggregation row yet. Returns None if there
    are no coders or no qualifying segment."""
    n_coders = conn.execute(
        "SELECT COUNT(*) AS n FROM coders"
    ).fetchone()["n"]
    if n_coders == 0:
        return None
    return conn.execute(
        "SELECT s.segment_id, s.text FROM segments s "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM coders c "
        "  WHERE NOT EXISTS ("
        "    SELECT 1 FROM coder_runs cr "
        "    WHERE cr.segment_id = s.segment_id "
        "      AND cr.coder_id = c.coder_id "
        "      AND cr.status = 'done'"
        "  )"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM aggregations a WHERE a.segment_id = s.segment_id"
        ") "
        "ORDER BY s.segment_id LIMIT 1"
    ).fetchone()


def load_segment_coder_results(
    conn: sqlite3.Connection, segment_id: str
) -> list[CoderRunResult]:
    """Load each completed coder_run for the segment, with its codes."""
    runs = conn.execute(
        "SELECT id, coder_id FROM coder_runs "
        "WHERE segment_id = ? AND status = 'done' "
        "ORDER BY coder_id",
        (segment_id,),
    ).fetchall()
    out: list[CoderRunResult] = []
    for r in runs:
        rows = conn.execute(
            "SELECT code, rationale, is_new FROM coder_codes "
            "WHERE coder_run_id = ? ORDER BY position",
            (r["id"],),
        ).fetchall()
        out.append(
            CoderRunResult(
                coder_id=r["coder_id"],
                codes=[x["code"] for x in rows],
                rationales=[x["rationale"] or "" for x in rows],
                is_new=[bool(x["is_new"]) for x in rows],
            )
        )
    return out


def start_aggregation(
    conn: sqlite3.Connection, segment_id: str
) -> int | None:
    """Insert a 'pending' aggregations row for a segment. Returns the new id,
    or None if a row already exists (UNIQUE constraint)."""
    try:
        cur = conn.execute(
            "INSERT INTO aggregations (segment_id, status, created_at) "
            "VALUES (?, 'pending', ?)",
            (segment_id, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    if cur.lastrowid is None:
        return None
    conn.execute(
        "UPDATE segments SET status='aggregating' "
        "WHERE segment_id = ? AND status IN ('pending', 'coding')",
        (segment_id,),
    )
    return int(cur.lastrowid)


@dataclass
class AggregatedCodeRow:
    code: str
    quotes_json: str
    source_coders_json: str


def record_aggregation_result(
    conn: sqlite3.Connection,
    *,
    aggregation_id: int,
    segment_id: str,
    rows: list[AggregatedCodeRow],
) -> None:
    """Persist aggregated_codes and mark the aggregation 'done'. If any
    aggregated_codes were produced, transition the segment to 'reviewing';
    otherwise mark it 'done' (nothing to review)."""
    try:
        conn.execute("BEGIN")
        for r in rows:
            conn.execute(
                "INSERT INTO aggregated_codes "
                "(aggregation_id, code, quotes_json, source_coders_json) "
                "VALUES (?, ?, ?, ?)",
                (aggregation_id, r.code, r.quotes_json, r.source_coders_json),
            )
        conn.execute(
            "UPDATE aggregations SET status='done', finished_at=? WHERE id = ?",
            (_now(), aggregation_id),
        )
        next_status = "reviewing" if rows else "done"
        conn.execute(
            "UPDATE segments SET status=? WHERE segment_id = ?",
            (next_status, segment_id),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_aggregation_failure(
    conn: sqlite3.Connection, aggregation_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE aggregations SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (_now(), error, aggregation_id),
    )


def reset_unfinished_aggregations(conn: sqlite3.Connection) -> int:
    """Delete failed/pending aggregations so they will be re-attempted next
    time `aggregate` runs. Also nukes their aggregated_codes (cascade-by-hand)."""
    rows = conn.execute(
        "SELECT id FROM aggregations WHERE status IN ('failed', 'pending')"
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM aggregated_codes WHERE aggregation_id IN ({placeholders})",
        ids,
    )
    cur = conn.execute(
        f"DELETE FROM aggregations WHERE id IN ({placeholders})", ids
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Review decisions
# ---------------------------------------------------------------------------


def next_aggregated_code_to_review(
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    """Return the next aggregated_code row (with its segment_id) that has no
    review_decision yet. Returns None if all codes are reviewed."""
    return conn.execute(
        "SELECT ac.id, ac.aggregation_id, ac.code, ac.quotes_json, "
        "       ac.source_coders_json, a.segment_id "
        "FROM aggregated_codes ac "
        "JOIN aggregations a ON a.id = ac.aggregation_id "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM review_decisions rd "
        "  WHERE rd.aggregated_code_id = ac.id"
        ") "
        "ORDER BY ac.id LIMIT 1"
    ).fetchone()


def record_review_decision(
    conn: sqlite3.Connection,
    *,
    aggregated_code_id: int,
    decision: str,
    target_code: str | None,
    rationale: str,
    applied: int,
    resulting_version: int | None,
) -> int:
    """Insert a review_decision row. Returns the new id."""
    cur = conn.execute(
        "INSERT INTO review_decisions "
        "(aggregated_code_id, decision, target_code, rationale, applied, "
        " resulting_version, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            aggregated_code_id,
            decision,
            target_code,
            rationale,
            applied,
            resulting_version,
            _now(),
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@dataclass
class StatusCounts:
    segments_total: int
    segments_by_status: dict[str, int]
    coders_total: int
    coder_runs_total: int
    coder_runs_by_status: dict[str, int]
    aggregations_total: int
    aggregations_by_status: dict[str, int]
    review_decisions_total: int
    review_decisions_applied: int
    codebook_version: int
    codebook_codes: int

    def format(self) -> str:
        def by_status(d: dict[str, int]) -> str:
            if not d:
                return "(none)"
            return " | ".join(f"{k}={v}" for k, v in sorted(d.items()))

        lines = [
            f"segments:         {self.segments_total} total | "
            f"{by_status(self.segments_by_status)}",
            f"coders:           {self.coders_total}",
            f"coder_runs:       {self.coder_runs_total} total | "
            f"{by_status(self.coder_runs_by_status)}",
            f"aggregations:     {self.aggregations_total} total | "
            f"{by_status(self.aggregations_by_status)}",
            f"review_decisions: {self.review_decisions_total} total | "
            f"applied={self.review_decisions_applied}",
            f"codebook:         v={self.codebook_version} "
            f"codes={self.codebook_codes}",
        ]
        return "\n".join(lines)


def _group_count(
    conn: sqlite3.Connection, table: str, column: str = "status"
) -> tuple[int, dict[str, int]]:
    rows = conn.execute(
        f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
    ).fetchall()
    by = {r["k"]: r["n"] for r in rows}
    total = sum(by.values())
    return total, by


def status_counts(conn: sqlite3.Connection) -> StatusCounts:
    seg_total, seg_by = _group_count(conn, "segments")
    cr_total, cr_by = _group_count(conn, "coder_runs")
    agg_total, agg_by = _group_count(conn, "aggregations")

    coders_total = conn.execute(
        "SELECT COUNT(*) AS n FROM coders"
    ).fetchone()["n"]
    rd_total = conn.execute(
        "SELECT COUNT(*) AS n FROM review_decisions"
    ).fetchone()["n"]
    rd_applied = conn.execute(
        "SELECT COUNT(*) AS n FROM review_decisions WHERE applied = 1"
    ).fetchone()["n"]

    latest = latest_codebook_version(conn)
    if latest is None:
        codebook_version = 0
        codebook_codes = 0
    else:
        codebook_version = latest.version
        codebook_codes = len(json.loads(latest.snapshot_json).get("codes", []))

    return StatusCounts(
        segments_total=seg_total,
        segments_by_status=seg_by,
        coders_total=coders_total,
        coder_runs_total=cr_total,
        coder_runs_by_status=cr_by,
        aggregations_total=agg_total,
        aggregations_by_status=agg_by,
        review_decisions_total=rd_total,
        review_decisions_applied=rd_applied,
        codebook_version=codebook_version,
        codebook_codes=codebook_codes,
    )
