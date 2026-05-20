"""Stage 2 (theme_*) DAL — behaviour-preserving relocation from store.py."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from thematic_analysis_inc.db.connection import now


@dataclass
class ThemeCoder:
    theme_coder_id: str
    identity: str
    created_at: str


def add_theme_coder(
    conn: sqlite3.Connection, theme_coder_id: str, identity: str
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO theme_coders "
        "(theme_coder_id, identity, created_at) VALUES (?, ?, ?)",
        (theme_coder_id, identity, now()),
    )
    return cur.rowcount == 1


def remove_theme_coder(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    *,
    force: bool = False,
) -> tuple[bool, int]:
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM theme_coder_runs WHERE theme_coder_id = ?",
        (theme_coder_id,),
    ).fetchone()["n"]
    if n > 0 and not force:
        raise RuntimeError(
            f"theme_coder '{theme_coder_id}' has {n} run(s); pass "
            "force=True to cascade"
        )
    runs_deleted = 0
    with conn:
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
            "DELETE FROM theme_coders WHERE theme_coder_id = ?",
            (theme_coder_id,),
        )
        return cur.rowcount == 1, int(runs_deleted)


def get_theme_coder(
    conn: sqlite3.Connection, theme_coder_id: str
) -> ThemeCoder | None:
    row = conn.execute(
        "SELECT theme_coder_id, identity, created_at "
        "FROM theme_coders WHERE theme_coder_id = ?",
        (theme_coder_id,),
    ).fetchone()
    return None if row is None else ThemeCoder(**dict(row))


def list_theme_coders(conn: sqlite3.Connection) -> list[ThemeCoder]:
    rows = conn.execute(
        "SELECT theme_coder_id, identity, created_at FROM theme_coders "
        "ORDER BY theme_coder_id"
    ).fetchall()
    return [ThemeCoder(**dict(r)) for r in rows]


def theme_coders_to_run(
    conn: sqlite3.Connection, codebook_version: int
) -> list[sqlite3.Row]:
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
    try:
        cur = conn.execute(
            "INSERT INTO theme_coder_runs "
            "(theme_coder_id, codebook_version, status, claimed_at) "
            "VALUES (?, ?, 'running', ?)",
            (theme_coder_id, codebook_version, now()),
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
        (now(), result_json, raw_response, run_id),
    )


def record_theme_coder_failure(
    conn: sqlite3.Connection, run_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE theme_coder_runs SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (now(), error, run_id),
    )


def reset_unfinished_theme_coder_runs(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    codebook_version: int | None = None,
) -> int:
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
    return int(cur.rowcount)


@dataclass
class ThemeCoderRunResult:
    run_id: int
    theme_coder_id: str
    result_json: str


def all_theme_coders_done(
    conn: sqlite3.Connection, codebook_version: int
) -> bool:
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
        "    AND tcr.codebook_version = ? AND tcr.status = 'done'"
        ")",
        (codebook_version,),
    ).fetchone()["n"]
    return n_pending == 0


def load_done_theme_coder_runs(
    conn: sqlite3.Connection, codebook_version: int
) -> list[ThemeCoderRunResult]:
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
    try:
        cur = conn.execute(
            "INSERT INTO theme_aggregations "
            "(codebook_version, status, created_at) "
            "VALUES (?, 'running', ?)",
            (codebook_version, now()),
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
    with conn:
        conn.execute(
            "UPDATE theme_aggregations "
            "SET status='done', finished_at=?, result_json=? WHERE id = ?",
            (now(), result_json, agg_id),
        )
        for run_id in run_ids:
            conn.execute(
                "INSERT INTO theme_aggregation_inputs "
                "(theme_aggregation_id, theme_coder_run_id) VALUES (?, ?)",
                (agg_id, run_id),
            )


def record_theme_aggregation_failure(
    conn: sqlite3.Connection, agg_id: int, error: str
) -> None:
    conn.execute(
        "UPDATE theme_aggregations SET status='failed', finished_at=?, error=? "
        "WHERE id = ?",
        (now(), error, agg_id),
    )


def reset_unfinished_theme_aggregations(
    conn: sqlite3.Connection, codebook_version: int
) -> int:
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
    return int(cur.rowcount)


def list_theme_coders_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Raw theme_coders rows (for status payloads that don't need dataclasses)."""
    return conn.execute(
        "SELECT theme_coder_id, identity FROM theme_coders ORDER BY theme_coder_id"
    ).fetchall()


def latest_theme_coder_run(
    conn: sqlite3.Connection, theme_coder_id: str, codebook_version: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, status, claimed_at, finished_at, error "
        "FROM theme_coder_runs "
        "WHERE theme_coder_id = ? AND codebook_version = ? "
        "ORDER BY id DESC LIMIT 1",
        (theme_coder_id, codebook_version),
    ).fetchone()


def list_theme_coder_runs_for_version(
    conn: sqlite3.Connection, codebook_version: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, theme_coder_id, codebook_version, status, "
        "  claimed_at, finished_at, error, "
        "  CASE WHEN result_json IS NULL THEN 0 "
        "       ELSE length(result_json) END AS result_bytes "
        "FROM theme_coder_runs WHERE codebook_version = ? "
        "ORDER BY theme_coder_id",
        (codebook_version,),
    ).fetchall()


def get_theme_coder_run(
    conn: sqlite3.Connection, run_id: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, theme_coder_id, codebook_version, status, "
        "  claimed_at, finished_at, error, result_json, raw_response "
        "FROM theme_coder_runs WHERE id = ?",
        (run_id,),
    ).fetchone()


def delete_theme_coder_run(conn: sqlite3.Connection, run_id: int) -> bool:
    with conn:
        conn.execute(
            "DELETE FROM theme_aggregation_inputs WHERE theme_coder_run_id = ?",
            (run_id,),
        )
        cur = conn.execute(
            "DELETE FROM theme_coder_runs WHERE id = ?", (run_id,)
        )
        return cur.rowcount > 0


def delete_theme_aggregation(conn: sqlite3.Connection, agg_id: int) -> bool:
    with conn:
        conn.execute(
            "DELETE FROM theme_aggregation_inputs WHERE theme_aggregation_id = ?",
            (agg_id,),
        )
        cur = conn.execute(
            "DELETE FROM theme_aggregations WHERE id = ?", (agg_id,)
        )
        return cur.rowcount > 0


def get_existing_theme_coder_run_status(
    conn: sqlite3.Connection, theme_coder_id: str, codebook_version: int
) -> str | None:
    row = conn.execute(
        "SELECT status FROM theme_coder_runs "
        "WHERE theme_coder_id = ? AND codebook_version = ?",
        (theme_coder_id, codebook_version),
    ).fetchone()
    return None if row is None else row["status"]


def total_segments_count(conn: sqlite3.Connection) -> int:
    """Web helper: count of segments. (Lives here to avoid raw SQL in web.py.)"""
    row = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()
    return int(row["n"])


def latest_theme_aggregation(
    conn: sqlite3.Connection, codebook_version: int
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, codebook_version, status, created_at, finished_at, "
        "       result_json, error "
        "FROM theme_aggregations "
        "WHERE codebook_version = ? ORDER BY id DESC LIMIT 1",
        (codebook_version,),
    ).fetchone()
