"""Derived status counts for dashboards / status payloads."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass


@dataclass
class StatusCounts:
    segments_total: int
    segments_by_status: dict[str, int]
    coders_total: int
    coding_queue_total: int
    coding_queue_by_status: dict[str, int]
    aggregator_codes_total: int
    aggregator_segments_total: int
    reviewer_codes_total: int
    review_decisions_by_kind: dict[str, int]
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
            f"coding_queue:     {self.coding_queue_total} total | "
            f"{by_status(self.coding_queue_by_status)}",
            f"aggregator codes: {self.aggregator_codes_total} "
            f"(segments={self.aggregator_segments_total})",
            f"reviewer codes:   {self.reviewer_codes_total} | "
            f"{by_status(self.review_decisions_by_kind)}",
            f"codebook:         v={self.codebook_version} "
            f"codes={self.codebook_codes}",
        ]
        return "\n".join(lines)


def coding_queue_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT CASE "
        "  WHEN error IS NOT NULL THEN 'failed' "
        "  WHEN finished_at IS NOT NULL THEN 'done' "
        "  WHEN claimed_at IS NOT NULL THEN 'running' "
        "  ELSE 'pending' END AS status, COUNT(*) AS n "
        "FROM coding_queue GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def segments_by_derived_status(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Per-segment derived status; reduces all coding_queue rows + codes."""
    rows = conn.execute("SELECT segment_id FROM segments").fetchall()
    out: dict[str, int] = {}
    for r in rows:
        s = derive_segment_status(conn, int(r["segment_id"]))
        out[s] = out.get(s, 0) + 1
    return out


def derive_segment_status(
    conn: sqlite3.Connection, segment_id: int
) -> str:
    qrows = conn.execute(
        "SELECT claimed_at, finished_at, error FROM coding_queue "
        "WHERE segment_id = ?",
        (segment_id,),
    ).fetchall()
    if not qrows:
        return "pending"
    if any(r["error"] is not None for r in qrows):
        return "failed"
    if any(
        r["finished_at"] is None and r["claimed_at"] is not None
        for r in qrows
    ):
        return "coding"
    if any(r["claimed_at"] is None for r in qrows):
        return "pending"
    # all queue rows done.
    has_agg = conn.execute(
        "SELECT 1 FROM codes WHERE segment_id = ? AND coder_id = 0 LIMIT 1",
        (segment_id,),
    ).fetchone()
    if has_agg is None:
        return "aggregating"
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM codes c "
        "WHERE c.segment_id = ? AND c.coder_id = 0 "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM codes_derived d "
        "    WHERE d.source_code_id = c.code_id AND d.derivation_type = 'R'"
        "  )",
        (segment_id,),
    ).fetchone()["n"]
    if remaining > 0:
        return "reviewing"
    return "done"


def status_counts(conn: sqlite3.Connection) -> StatusCounts:
    seg_total = conn.execute(
        "SELECT COUNT(*) AS n FROM segments"
    ).fetchone()["n"]
    seg_by = segments_by_derived_status(conn)

    coders_total = conn.execute(
        "SELECT COUNT(*) AS n FROM coders WHERE coder_id >= 1"
    ).fetchone()["n"]

    cq_by = coding_queue_by_status(conn)
    cq_total = sum(cq_by.values())

    agg_codes_total = conn.execute(
        "SELECT COUNT(*) AS n FROM codes WHERE coder_id = 0"
    ).fetchone()["n"]
    agg_segs_total = conn.execute(
        "SELECT COUNT(DISTINCT segment_id) AS n FROM codes WHERE coder_id = 0"
    ).fetchone()["n"]

    rev_total = conn.execute(
        "SELECT COUNT(*) AS n FROM codes WHERE coder_id = -1"
    ).fetchone()["n"]
    rev_rows = conn.execute(
        "SELECT decision, COUNT(*) AS n FROM codes_derived "
        "WHERE derivation_type = 'R' GROUP BY decision"
    ).fetchall()
    rev_by = {r["decision"]: r["n"] for r in rev_rows}

    cb_row = conn.execute(
        "SELECT version FROM codebook_versions ORDER BY version DESC LIMIT 1"
    ).fetchone()
    cb_version = 0 if cb_row is None else int(cb_row["version"])
    cb_codes = conn.execute(
        "SELECT COUNT(*) AS n FROM codebook WHERE version = ?",
        (cb_version,),
    ).fetchone()["n"]

    return StatusCounts(
        segments_total=int(seg_total),
        segments_by_status=seg_by,
        coders_total=int(coders_total),
        coding_queue_total=int(cq_total),
        coding_queue_by_status=cq_by,
        aggregator_codes_total=int(agg_codes_total),
        aggregator_segments_total=int(agg_segs_total),
        reviewer_codes_total=int(rev_total),
        review_decisions_by_kind=rev_by,
        codebook_version=cb_version,
        codebook_codes=int(cb_codes),
    )


# Stage 2 status (unchanged; reuses old theme_* tables) -----------------------


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
    agg = conn.execute(
        "SELECT result_json FROM theme_aggregations "
        "WHERE codebook_version = ? ORDER BY id DESC LIMIT 1",
        (codebook_version,),
    ).fetchone()
    if agg is not None and agg["result_json"]:
        themes_in_result = len(
            json.loads(agg["result_json"]).get("themes", [])
        )

    return Stage2StatusCounts(
        codebook_version=codebook_version,
        theme_coders_total=int(tc_total),
        theme_coder_runs_total=int(tcr_total),
        theme_coder_runs_by_status=tcr_by,
        theme_aggregations_total=int(ta_total),
        theme_aggregations_by_status=ta_by,
        themes_in_result=int(themes_in_result),
    )
