"""Aggregator (reserved coder_id = 0) helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.coders import SYSTEM_AGGREGATOR_ID
from thematic_analysis_inc.db.documents import add_quote, link_code_quote
from thematic_analysis_inc.db.research_context import (
    latest_research_context_version,
)


@dataclass
class AggregatorMergeInput:
    """One merged/retained code from the aggregator agent."""

    code: str
    description: str = ""
    rationale: str = ""
    quote_texts: list[str] = None  # type: ignore[assignment]
    source_code_ids: list[int] = None  # type: ignore[assignment]


def next_segment_to_aggregate(
    conn: sqlite3.Connection,
) -> sqlite3.Row | None:
    """Return the next segment whose every coding_queue row is `done`
    (no errors, finished_at set) and which has no aggregator code yet."""
    n_coders = conn.execute(
        "SELECT COUNT(*) AS n FROM coders WHERE coder_id >= 1"
    ).fetchone()["n"]
    if n_coders == 0:
        return None
    return conn.execute(
        "SELECT s.segment_id, s.content FROM segments s "
        "WHERE EXISTS ("
        "  SELECT 1 FROM coding_queue cq WHERE cq.segment_id = s.segment_id"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM coding_queue cq WHERE cq.segment_id = s.segment_id "
        "    AND (cq.finished_at IS NULL OR cq.error IS NOT NULL)"
        ") "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM codes c WHERE c.segment_id = s.segment_id "
        "    AND c.coder_id = 0"
        ") "
        "ORDER BY s.segment_id LIMIT 1"
    ).fetchone()


def segment_has_aggregator_code(
    conn: sqlite3.Connection, segment_id: int
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM codes WHERE segment_id = ? AND coder_id = 0 LIMIT 1",
        (segment_id,),
    ).fetchone()
    return row is not None


def record_aggregation_result(
    conn: sqlite3.Connection,
    *,
    segment_id: int,
    version: int,
    inputs: list[AggregatorMergeInput],
    research_context_version: int | None = None,
) -> list[int]:
    """For each merged/retained code: insert quotes, an aggregator code
    row, link the quotes, and add one codes_derived('A') edge per source
    code. Returns the new aggregator code_ids.

    The Python kwarg ``version`` writes to the renamed
    ``codes.codebook_version`` column. ``research_context_version`` is
    optional; if ``None`` the latest known RC version is used.
    """
    if research_context_version is None:
        research_context_version = latest_research_context_version(conn)
    new_ids: list[int] = []
    with conn:
        for inp in inputs:
            cur = conn.execute(
                "INSERT INTO codes "
                "(segment_id, coder_id, codebook_version, "
                " research_context_version, code, description, rationale) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    segment_id,
                    SYSTEM_AGGREGATOR_ID,
                    version,
                    research_context_version,
                    inp.code,
                    inp.description or "",
                    inp.rationale or "",
                ),
            )
            agg_code_id = int(cur.lastrowid)
            new_ids.append(agg_code_id)
            for text in (inp.quote_texts or []):
                qid = add_quote(conn, segment_id, text)
                link_code_quote(conn, agg_code_id, qid)
            for src in (inp.source_code_ids or []):
                conn.execute(
                    "INSERT OR IGNORE INTO codes_derived "
                    "(new_code_id, source_code_id, derivation_type, "
                    " decision, rationale) "
                    "VALUES (?, ?, 'A', NULL, NULL)",
                    (agg_code_id, src),
                )
    return new_ids


def load_aggregated_code(
    conn: sqlite3.Connection, code_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT code_id, segment_id, coder_id, codebook_version, code, "
        "       description, rationale "
        "FROM codes WHERE code_id = ? AND coder_id = 0",
        (code_id,),
    ).fetchone()


def load_aggregated_code_quotes(
    conn: sqlite3.Connection, code_id: int
) -> list[dict]:
    rows = conn.execute(
        "SELECT q.quote_id, q.text "
        "FROM codes_supporting_quotes csq "
        "JOIN quotes q ON q.quote_id = csq.quote_id "
        "WHERE csq.code_id = ? ORDER BY q.quote_id",
        (code_id,),
    ).fetchall()
    return [{"quote_id": r["quote_id"], "text": r["text"]} for r in rows]


def list_aggregator_codes_for_segment(
    conn: sqlite3.Connection, segment_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT code_id, code, description, rationale "
        "FROM codes WHERE segment_id = ? AND coder_id = 0 "
        "ORDER BY code_id",
        (segment_id,),
    ).fetchall()


def list_aggregations(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    """List per-segment aggregations as a synthetic view over codes(coder_id=0)."""
    total = conn.execute(
        "SELECT COUNT(DISTINCT segment_id) AS n FROM codes WHERE coder_id = 0"
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT segment_id, COUNT(*) AS n_codes, MIN(code_id) AS first_id "
        "FROM codes WHERE coder_id = 0 GROUP BY segment_id "
        "ORDER BY segment_id DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return int(total), [dict(r) for r in rows]
