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

from thematic_analysis.research_context import ResearchContext
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
# Research context
# ---------------------------------------------------------------------------


def _research_context_to_json(ctx: ResearchContext) -> str:
    return json.dumps(
        {
            "description": ctx.description,
            "tailored_prompts": dict(ctx.tailored_prompts),
        },
        indent=2,
    )


_LEGACY_FIELD_LABELS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("aim", "Aim"),
    ("research_questions", "Research questions"),
    ("theoretical_framework", "Theoretical framework"),
    ("paradigm", "Paradigm"),
    ("domain", "Domain"),
    ("background", "Background"),
    ("keywords", "Keywords"),
]


def _legacy_to_description(data: dict) -> str:
    """Fold a pre-refactor 9-field research_context JSON into a freeform
    description so existing data is preserved across the schema change."""
    parts: list[str] = []
    for key, label in _LEGACY_FIELD_LABELS:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            cleaned = [str(v).strip() for v in val if str(v).strip()]
            if not cleaned:
                continue
            if key == "research_questions":
                rendered = "\n".join(f"{i + 1}. {v}" for i, v in enumerate(cleaned))
            else:
                rendered = ", ".join(cleaned)
        else:
            rendered = str(val).strip()
            if not rendered:
                continue
        parts.append(f"**{label}:** {rendered}")
    return "\n\n".join(parts)


def _research_context_from_json(raw: str) -> ResearchContext:
    data = json.loads(raw)
    if "description" in data or "tailored_prompts" in data:
        prompts = data.get("tailored_prompts") or {}
        if not isinstance(prompts, dict):
            prompts = {}
        return ResearchContext(
            description=str(data.get("description", "")),
            tailored_prompts={
                str(k): str(v) for k, v in prompts.items() if v
            },
        )
    # Legacy 9-field shape — fold into description so data isn't lost.
    return ResearchContext(description=_legacy_to_description(data))


def set_research_context(
    conn: sqlite3.Connection, context: ResearchContext
) -> None:
    """Upsert the singleton research context row."""
    conn.execute(
        "INSERT INTO research_context (id, context_json, updated_at) "
        "VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  context_json = excluded.context_json, "
        "  updated_at = excluded.updated_at",
        (_research_context_to_json(context), _now()),
    )


def get_research_context(conn: sqlite3.Connection) -> ResearchContext | None:
    """Return the stored research context, or None if not set."""
    row = conn.execute(
        "SELECT context_json FROM research_context WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return _research_context_from_json(row["context_json"])


def clear_research_context(conn: sqlite3.Connection) -> bool:
    """Delete the stored research context. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM research_context WHERE id = 1")
    return cur.rowcount > 0


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


def list_codebook_versions(conn: sqlite3.Connection) -> list[CodebookVersion]:
    rows = conn.execute(
        "SELECT version, parent_version, snapshot_json, created_by, created_at "
        "FROM codebook_versions ORDER BY version ASC"
    ).fetchall()
    return [CodebookVersion(**dict(r)) for r in rows]


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


def remove_coder(
    conn: sqlite3.Connection, coder_id: str, *, force: bool = False
) -> tuple[bool, int]:
    """Delete a coder. Returns (removed, runs_deleted).

    By default refuses if the coder has any coder_runs. With force=True,
    cascades and deletes their coder_codes + coder_runs first.
    """
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM coder_runs WHERE coder_id = ?", (coder_id,)
    ).fetchone()["n"]
    if n > 0 and not force:
        raise RuntimeError(
            f"coder '{coder_id}' has {n} coder_runs; pass force=True to cascade"
        )
    runs_deleted = 0
    try:
        conn.execute("BEGIN")
        if n > 0:
            conn.execute(
                "DELETE FROM coder_codes WHERE coder_run_id IN ("
                "  SELECT id FROM coder_runs WHERE coder_id = ?"
                ")",
                (coder_id,),
            )
            cur = conn.execute(
                "DELETE FROM coder_runs WHERE coder_id = ?", (coder_id,)
            )
            runs_deleted = cur.rowcount
        cur = conn.execute(
            "DELETE FROM coders WHERE coder_id = ?", (coder_id,)
        )
        removed = cur.rowcount == 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return removed, runs_deleted


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


def add_document(
    conn: sqlite3.Connection,
    filename: str,
    content: bytes,
) -> int:
    """Insert a source document, returning its rowid."""
    cur = conn.execute(
        "INSERT INTO documents (filename, content, created_at) VALUES (?, ?, ?)",
        (filename, content, _now()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def enqueue_segments(
    conn: sqlite3.Connection,
    segments: Iterable[
        tuple[str, str]
        | tuple[str, str, str | None, int | None]
        | tuple[str, str, str | None, int | None, int | None]
    ],
    batch: int | None = None,
) -> EnqueueResult:
    """Insert segments. Idempotent on segment_id. No coder_runs created.

    Each segment is one of:
        (segment_id, text)
        (segment_id, text, title, document_id)
        (segment_id, text, title, document_id, position)

    Missing trailing fields default to NULL.
    """
    inserted = 0
    skipped = 0
    try:
        conn.execute("BEGIN")
        for entry in segments:
            if len(entry) == 2:
                segment_id, text = entry
                title, document_id, position = None, None, None
            elif len(entry) == 4:
                segment_id, text, title, document_id = entry
                position = None
            else:
                segment_id, text, title, document_id, position = entry
            cur = conn.execute(
                "INSERT OR IGNORE INTO segments "
                "(segment_id, text, title, document_id, position, batch, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending')",
                (segment_id, text, title, document_id, position, batch),
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


def reset_all_coder_runs(
    conn: sqlite3.Connection, coder_id: str
) -> int:
    """Delete all coder_runs (and their coder_codes) for this coder so every
    segment is re-coded next time `code <coder_id>` runs. Returns rows
    deleted."""
    try:
        conn.execute("BEGIN")
        conn.execute(
            "DELETE FROM coder_codes WHERE coder_run_id IN ("
            "  SELECT id FROM coder_runs WHERE coder_id = ?"
            ")",
            (coder_id,),
        )
        cur = conn.execute(
            "DELETE FROM coder_runs WHERE coder_id = ?", (coder_id,)
        )
        deleted = cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return deleted


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


# ---------------------------------------------------------------------------
# Stage 2 — theme coders
# ---------------------------------------------------------------------------


@dataclass
class ThemeCoder:
    theme_coder_id: str
    identity: str
    created_at: str


def add_theme_coder(conn: sqlite3.Connection, theme_coder_id: str, identity: str) -> bool:
    """Insert a theme coder. Returns True if newly inserted, False if id existed."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO theme_coders (theme_coder_id, identity, created_at) "
        "VALUES (?, ?, ?)",
        (theme_coder_id, identity, _now()),
    )
    return cur.rowcount == 1


def remove_theme_coder(
    conn: sqlite3.Connection, theme_coder_id: str, *, force: bool = False
) -> tuple[bool, int]:
    """Delete a theme coder. Returns (removed, runs_deleted).

    By default refuses if the coder has any theme_coder_runs. With force=True,
    cascades and deletes their runs first.
    """
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coder_runs WHERE theme_coder_id = ?",
        (theme_coder_id,),
    ).fetchone()["n"]
    if n > 0 and not force:
        raise RuntimeError(
            f"theme_coder '{theme_coder_id}' has {n} run(s); pass force=True to cascade"
        )
    runs_deleted = 0
    try:
        conn.execute("BEGIN")
        if n > 0:
            conn.execute(
                "DELETE FROM theme_aggregation_inputs WHERE theme_coder_run_id IN ("
                "  SELECT id FROM theme_coder_runs WHERE theme_coder_id = ?"
                ")",
                (theme_coder_id,),
            )
            cur = conn.execute(
                "DELETE FROM theme_coder_runs WHERE theme_coder_id = ?",
                (theme_coder_id,),
            )
            runs_deleted = cur.rowcount
        cur = conn.execute(
            "DELETE FROM theme_coders WHERE theme_coder_id = ?", (theme_coder_id,)
        )
        removed = cur.rowcount == 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return removed, runs_deleted


def get_theme_coder(conn: sqlite3.Connection, theme_coder_id: str) -> ThemeCoder | None:
    row = conn.execute(
        "SELECT theme_coder_id, identity, created_at "
        "FROM theme_coders WHERE theme_coder_id = ?",
        (theme_coder_id,),
    ).fetchone()
    if row is None:
        return None
    return ThemeCoder(**dict(row))


def list_theme_coders(conn: sqlite3.Connection) -> list[ThemeCoder]:
    rows = conn.execute(
        "SELECT theme_coder_id, identity, created_at "
        "FROM theme_coders ORDER BY theme_coder_id"
    ).fetchall()
    return [ThemeCoder(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Stage 2 — theme coder runs
# ---------------------------------------------------------------------------


def theme_coders_to_run(
    conn: sqlite3.Connection, codebook_version: int
) -> list[sqlite3.Row]:
    """Theme coders that don't yet have a 'done' run for this codebook version."""
    return conn.execute(
        "SELECT tc.theme_coder_id, tc.identity "
        "FROM theme_coders tc "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM theme_coder_runs tcr "
        "  WHERE tcr.theme_coder_id = tc.theme_coder_id "
        "    AND tcr.codebook_version = ? "
        "    AND tcr.status = 'done'"
        ") "
        "ORDER BY tc.theme_coder_id",
        (codebook_version,),
    ).fetchall()


def start_theme_coder_run(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    codebook_version: int,
) -> int | None:
    """Insert a 'running' theme_coder_run. Returns the new id, or None on race."""
    try:
        cur = conn.execute(
            "INSERT INTO theme_coder_runs "
            "(theme_coder_id, codebook_version, status, claimed_at) "
            "VALUES (?, ?, 'running', ?)",
            (theme_coder_id, codebook_version, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid) if cur.lastrowid else None


def record_theme_coder_result(
    conn: sqlite3.Connection,
    run_id: int,
    result_json: str,
    raw_response: str | None = None,
) -> None:
    conn.execute(
        "UPDATE theme_coder_runs "
        "SET status='done', finished_at=?, result_json=?, raw_response=? "
        "WHERE id = ?",
        (_now(), result_json, raw_response, run_id),
    )


def record_theme_coder_failure(
    conn: sqlite3.Connection, run_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE theme_coder_runs SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (_now(), error, run_id),
    )


def reset_unfinished_theme_coder_runs(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    codebook_version: int | None = None,
) -> int:
    """Delete failed/running rows so they will be re-attempted. Returns rows deleted."""
    if codebook_version is not None:
        cur = conn.execute(
            "DELETE FROM theme_coder_runs "
            "WHERE theme_coder_id = ? AND codebook_version = ? "
            "  AND status IN ('failed', 'running')",
            (theme_coder_id, codebook_version),
        )
    else:
        cur = conn.execute(
            "DELETE FROM theme_coder_runs "
            "WHERE theme_coder_id = ? AND status IN ('failed', 'running')",
            (theme_coder_id,),
        )
    return cur.rowcount


# ---------------------------------------------------------------------------
# Stage 2 — theme aggregation
# ---------------------------------------------------------------------------


@dataclass
class ThemeCoderRunResult:
    run_id: int
    theme_coder_id: str
    result_json: str


def all_theme_coders_done(conn: sqlite3.Connection, codebook_version: int) -> bool:
    """True if every registered theme coder has a 'done' run for this version."""
    n_coders = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coders"
    ).fetchone()["n"]
    if n_coders == 0:
        return False
    n_pending = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coders tc "
        "WHERE NOT EXISTS ("
        "  SELECT 1 FROM theme_coder_runs tcr "
        "  WHERE tcr.theme_coder_id = tc.theme_coder_id "
        "    AND tcr.codebook_version = ? "
        "    AND tcr.status = 'done'"
        ")",
        (codebook_version,),
    ).fetchone()["n"]
    return n_pending == 0


def load_done_theme_coder_runs(
    conn: sqlite3.Connection, codebook_version: int
) -> list[ThemeCoderRunResult]:
    """Load all 'done' theme_coder_runs for the given codebook version."""
    rows = conn.execute(
        "SELECT id, theme_coder_id, result_json "
        "FROM theme_coder_runs "
        "WHERE codebook_version = ? AND status = 'done' "
        "ORDER BY theme_coder_id",
        (codebook_version,),
    ).fetchall()
    return [
        ThemeCoderRunResult(
            run_id=r["id"],
            theme_coder_id=r["theme_coder_id"],
            result_json=r["result_json"],
        )
        for r in rows
    ]


def start_theme_aggregation(
    conn: sqlite3.Connection, codebook_version: int
) -> int | None:
    """Insert a 'running' theme_aggregation row. Returns new id, or None on race."""
    try:
        cur = conn.execute(
            "INSERT INTO theme_aggregations (codebook_version, status, created_at) "
            "VALUES (?, 'running', ?)",
            (codebook_version, _now()),
        )
    except sqlite3.IntegrityError:
        return None
    return int(cur.lastrowid) if cur.lastrowid else None


def record_theme_aggregation_result(
    conn: sqlite3.Connection,
    agg_id: int,
    result_json: str,
    run_ids: list[int],
) -> None:
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE theme_aggregations "
            "SET status='done', finished_at=?, result_json=? WHERE id = ?",
            (_now(), result_json, agg_id),
        )
        for run_id in run_ids:
            conn.execute(
                "INSERT INTO theme_aggregation_inputs "
                "(theme_aggregation_id, theme_coder_run_id) VALUES (?, ?)",
                (agg_id, run_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_theme_aggregation_failure(
    conn: sqlite3.Connection, agg_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE theme_aggregations SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (_now(), error, agg_id),
    )


def reset_unfinished_theme_aggregations(
    conn: sqlite3.Connection, codebook_version: int
) -> int:
    """Delete failed/running aggregation rows so they can be re-attempted."""
    rows = conn.execute(
        "SELECT id FROM theme_aggregations "
        "WHERE codebook_version = ? AND status IN ('failed', 'running')",
        (codebook_version,),
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM theme_aggregation_inputs "
        f"WHERE theme_aggregation_id IN ({placeholders})",
        ids,
    )
    cur = conn.execute(
        f"DELETE FROM theme_aggregations WHERE id IN ({placeholders})", ids
    )
    return cur.rowcount


def latest_theme_aggregation(
    conn: sqlite3.Connection, codebook_version: int
) -> sqlite3.Row | None:
    """Return the most recent theme_aggregation row for this codebook version."""
    return conn.execute(
        "SELECT id, codebook_version, status, created_at, finished_at, "
        "       result_json, error "
        "FROM theme_aggregations "
        "WHERE codebook_version = ? ORDER BY id DESC LIMIT 1",
        (codebook_version,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Stage 2 — status
# ---------------------------------------------------------------------------


@dataclass
class Stage2StatusCounts:
    codebook_version: int
    theme_coders_total: int
    theme_coder_runs_total: int
    theme_coder_runs_by_status: dict[str, int]
    theme_aggregations_total: int
    theme_aggregations_by_status: dict[str, int]
    themes_in_result: int

    def format(self) -> str:
        def by_status(d: dict[str, int]) -> str:
            if not d:
                return "(none)"
            return " | ".join(f"{k}={v}" for k, v in sorted(d.items()))

        lines = [
            f"codebook version:     v{self.codebook_version}",
            f"theme_coders:         {self.theme_coders_total}",
            f"theme_coder_runs:     {self.theme_coder_runs_total} total | "
            f"{by_status(self.theme_coder_runs_by_status)}",
            f"theme_aggregations:   {self.theme_aggregations_total} total | "
            f"{by_status(self.theme_aggregations_by_status)}",
            f"themes in result:     {self.themes_in_result}",
        ]
        return "\n".join(lines)


def stage2_status_counts(
    conn: sqlite3.Connection, codebook_version: int
) -> Stage2StatusCounts:
    tc_total = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coders"
    ).fetchone()["n"]

    tcr_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM theme_coder_runs "
        "WHERE codebook_version = ? GROUP BY status",
        (codebook_version,),
    ).fetchall()
    tcr_by = {r["status"]: r["n"] for r in tcr_rows}
    tcr_total = sum(tcr_by.values())

    ta_rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM theme_aggregations "
        "WHERE codebook_version = ? GROUP BY status",
        (codebook_version,),
    ).fetchall()
    ta_by = {r["status"]: r["n"] for r in ta_rows}
    ta_total = sum(ta_by.values())

    themes_in_result = 0
    agg = latest_theme_aggregation(conn, codebook_version)
    if agg is not None and agg["result_json"]:
        themes_in_result = len(json.loads(agg["result_json"]).get("themes", []))

    return Stage2StatusCounts(
        codebook_version=codebook_version,
        theme_coders_total=tc_total,
        theme_coder_runs_total=tcr_total,
        theme_coder_runs_by_status=tcr_by,
        theme_aggregations_total=ta_total,
        theme_aggregations_by_status=ta_by,
        themes_in_result=themes_in_result,
    )
