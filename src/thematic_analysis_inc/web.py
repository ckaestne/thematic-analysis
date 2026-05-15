"""FastAPI web server for inspecting and editing the thematic-analysis pipeline.

Exposes a JSON REST API over the SQLite store plus serves a pre-built React
SPA from ``src/thematic_analysis_inc/web_static``. Run with::

    ta-web --db analysis.sqlite

The frontend is read-mostly with surgical mutations: edit research context,
edit code text, and delete runs/aggregations/decisions so the pipeline
re-computes them on the next worker run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from thematic_analysis.research_context import AGENT_ROLES, ResearchContext
from thematic_analysis_inc import store


# ---------------------------------------------------------------------------
# App factory + DB dependency
# ---------------------------------------------------------------------------


_DB_PATH: Path | None = None


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("DB path not configured")
    return store.connect(_DB_PATH)


# ---------------------------------------------------------------------------
# Pydantic models for request bodies (responses are plain dicts)
# ---------------------------------------------------------------------------


class ResearchContextIn(BaseModel):
    description: str = ""
    tailored_prompts: dict[str, str] = Field(default_factory=dict)


class CoderIn(BaseModel):
    coder_id: str
    identity: str


class CodeEdit(BaseModel):
    code: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM segments GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def _coder_run_progress(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Per-coder: how many segments coded / total."""
    coders = conn.execute(
        "SELECT coder_id, identity FROM coders ORDER BY coder_id"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) AS n FROM segments").fetchone()["n"]
    out = []
    for c in coders:
        by = conn.execute(
            "SELECT status, COUNT(*) AS n FROM coder_runs "
            "WHERE coder_id = ? GROUP BY status",
            (c["coder_id"],),
        ).fetchall()
        d = {r["status"]: r["n"] for r in by}
        out.append(
            {
                "coder_id": c["coder_id"],
                "identity": c["identity"],
                "segments_total": total,
                "runs_done": d.get("done", 0),
                "runs_running": d.get("running", 0),
                "runs_failed": d.get("failed", 0),
            }
        )
    return out


def _theme_coder_progress(
    conn: sqlite3.Connection, codebook_version: int
) -> list[dict[str, Any]]:
    coders = conn.execute(
        "SELECT theme_coder_id, identity FROM theme_coders ORDER BY theme_coder_id"
    ).fetchall()
    out = []
    for c in coders:
        run = conn.execute(
            "SELECT id, status, claimed_at, finished_at, error "
            "FROM theme_coder_runs "
            "WHERE theme_coder_id = ? AND codebook_version = ? "
            "ORDER BY id DESC LIMIT 1",
            (c["theme_coder_id"], codebook_version),
        ).fetchone()
        out.append(
            {
                "theme_coder_id": c["theme_coder_id"],
                "identity": c["identity"],
                "run": dict(run) if run else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


def create_app(db_path: str | Path) -> FastAPI:
    global _DB_PATH
    _DB_PATH = Path(db_path)
    # Pre-create / migrate schema.
    store.init_db(_DB_PATH).close()

    app = FastAPI(title="Thematic Analysis Inspector", version="0.1.0")

    # ── status / dashboard ────────────────────────────────────────────────

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        conn = _conn()
        try:
            s1 = store.status_counts(conn)
            latest = store.latest_codebook_version(conn)
            codebook_version = latest.version if latest else 0
            s2 = store.stage2_status_counts(conn, codebook_version)
            ctx = store.get_research_context(conn)
            db_size = _DB_PATH.stat().st_size if _DB_PATH and _DB_PATH.exists() else 0
            return {
                "db_path": str(_DB_PATH),
                "db_size_bytes": db_size,
                "research_context_set": ctx is not None,
                "stage1": {
                    "segments_total": s1.segments_total,
                    "segments_by_status": s1.segments_by_status,
                    "coders_total": s1.coders_total,
                    "coder_runs_total": s1.coder_runs_total,
                    "coder_runs_by_status": s1.coder_runs_by_status,
                    "aggregations_total": s1.aggregations_total,
                    "aggregations_by_status": s1.aggregations_by_status,
                    "review_decisions_total": s1.review_decisions_total,
                    "review_decisions_applied": s1.review_decisions_applied,
                    "codebook_version": s1.codebook_version,
                    "codebook_codes": s1.codebook_codes,
                },
                "stage2": {
                    "codebook_version": s2.codebook_version,
                    "theme_coders_total": s2.theme_coders_total,
                    "theme_coder_runs_total": s2.theme_coder_runs_total,
                    "theme_coder_runs_by_status": s2.theme_coder_runs_by_status,
                    "theme_aggregations_total": s2.theme_aggregations_total,
                    "theme_aggregations_by_status": s2.theme_aggregations_by_status,
                    "themes_in_result": s2.themes_in_result,
                },
                "per_coder": _coder_run_progress(conn),
                "per_theme_coder": _theme_coder_progress(conn, codebook_version),
            }
        finally:
            conn.close()

    # ── research context ──────────────────────────────────────────────────

    @app.get("/api/research-context")
    def get_research_context() -> dict[str, Any] | None:
        conn = _conn()
        try:
            ctx = store.get_research_context(conn)
            if ctx is None:
                return None
            return {
                "description": ctx.description,
                "tailored_prompts": dict(ctx.tailored_prompts),
                "roles": list(AGENT_ROLES),
            }
        finally:
            conn.close()

    @app.put("/api/research-context")
    def put_research_context(body: ResearchContextIn) -> dict[str, Any]:
        conn = _conn()
        try:
            existing = store.get_research_context(conn)
            tailored = dict(body.tailored_prompts)
            # If the description changed, drop stored tailored prompts so
            # they cannot drift out of sync. The user (or the regenerate
            # endpoint) supplies new ones.
            if existing is not None and existing.description != body.description:
                tailored = {
                    k: v
                    for k, v in tailored.items()
                    if k in existing.tailored_prompts
                    and v == existing.tailored_prompts[k]
                }
            ctx = ResearchContext(
                description=body.description,
                tailored_prompts={
                    k: v for k, v in tailored.items() if k in AGENT_ROLES and v
                },
            )
            store.set_research_context(conn, ctx)
            return {
                "status": "ok",
                "description": ctx.description,
                "tailored_prompts": dict(ctx.tailored_prompts),
            }
        finally:
            conn.close()

    @app.delete("/api/research-context")
    def delete_research_context() -> dict[str, bool]:
        conn = _conn()
        try:
            return {"removed": store.clear_research_context(conn)}
        finally:
            conn.close()

    @app.post("/api/research-context/regenerate-prompts")
    def regenerate_tailored_prompts() -> dict[str, Any]:
        """Generate a tailored prompt section for every agent role from the
        currently stored research-context description, persist them, and
        return the new map."""
        from thematic_analysis.research_context_tailor import (
            generate_all_tailored_prompts,
        )

        conn = _conn()
        try:
            ctx = store.get_research_context(conn)
            if ctx is None or ctx.is_empty():
                raise HTTPException(
                    status_code=400,
                    detail="no research context description set",
                )
            prompts = generate_all_tailored_prompts(ctx.description)
            new_ctx = ResearchContext(
                description=ctx.description,
                tailored_prompts=prompts,
            )
            store.set_research_context(conn, new_ctx)
            return {
                "description": new_ctx.description,
                "tailored_prompts": dict(new_ctx.tailored_prompts),
                "roles": list(AGENT_ROLES),
            }
        finally:
            conn.close()

    # ── segments ──────────────────────────────────────────────────────────

    @app.get("/api/segments")
    def list_segments(
        status: str | None = None,
        batch: int | None = None,
        q: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            where = []
            params: list[Any] = []
            if status:
                where.append("status = ?")
                params.append(status)
            if batch is not None:
                where.append("batch = ?")
                params.append(batch)
            if q:
                where.append("(segment_id LIKE ? OR text LIKE ?)")
                params.extend([f"%{q}%", f"%{q}%"])
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM segments {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT segment_id, batch, status, title, document_id, "
                f"  substr(text, 1, 240) AS preview, length(text) AS len "
                f"FROM segments {clause} ORDER BY segment_id "
                f"LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {
                "total": total,
                "items": _rows_to_dicts(rows),
                "status_counts": _seg_status_counts(conn),
            }
        finally:
            conn.close()

    @app.get("/api/segments/{segment_id}")
    def get_segment(segment_id: str) -> dict[str, Any]:
        conn = _conn()
        try:
            seg = conn.execute(
                "SELECT segment_id, text, title, document_id, batch, status "
                "FROM segments WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            if seg is None:
                raise HTTPException(status_code=404, detail="segment not found")
            runs = conn.execute(
                "SELECT id, coder_id, codebook_version, status, "
                "  claimed_at, finished_at, error, raw_response "
                "FROM coder_runs WHERE segment_id = ? ORDER BY coder_id",
                (segment_id,),
            ).fetchall()
            run_list: list[dict[str, Any]] = []
            for r in runs:
                codes = conn.execute(
                    "SELECT position, code, rationale, is_new "
                    "FROM coder_codes WHERE coder_run_id = ? ORDER BY position",
                    (r["id"],),
                ).fetchall()
                run_list.append({**dict(r), "codes": _rows_to_dicts(codes)})

            agg = conn.execute(
                "SELECT id, status, created_at, finished_at, error "
                "FROM aggregations WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
            agg_codes: list[dict[str, Any]] = []
            if agg is not None:
                acs = conn.execute(
                    "SELECT id, code, quotes_json, source_coders_json "
                    "FROM aggregated_codes WHERE aggregation_id = ? ORDER BY id",
                    (agg["id"],),
                ).fetchall()
                for ac in acs:
                    rd = conn.execute(
                        "SELECT id, decision, target_code, rationale, applied, "
                        "  resulting_version, created_at "
                        "FROM review_decisions WHERE aggregated_code_id = ?",
                        (ac["id"],),
                    ).fetchone()
                    agg_codes.append(
                        {
                            "id": ac["id"],
                            "code": ac["code"],
                            "quotes": json.loads(ac["quotes_json"] or "[]"),
                            "source_coders": json.loads(
                                ac["source_coders_json"] or "[]"
                            ),
                            "review": dict(rd) if rd else None,
                        }
                    )
            return {
                **dict(seg),
                "coder_runs": run_list,
                "aggregation": dict(agg) if agg else None,
                "aggregated_codes": agg_codes,
            }
        finally:
            conn.close()

    @app.delete("/api/segments/{segment_id}")
    def delete_segment(segment_id: str) -> dict[str, Any]:
        """Delete a segment and every derived row that points at it.

        Useful when a segment was loaded by mistake. Cascades:
        review_decisions → aggregated_codes → aggregations,
        coder_codes → coder_runs, then segments.
        """
        conn = _conn()
        try:
            conn.execute("BEGIN")
            agg_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM aggregations WHERE segment_id = ?",
                    (segment_id,),
                ).fetchall()
            ]
            run_ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM coder_runs WHERE segment_id = ?",
                    (segment_id,),
                ).fetchall()
            ]
            if agg_ids:
                ph = ",".join("?" * len(agg_ids))
                conn.execute(
                    f"DELETE FROM review_decisions WHERE aggregated_code_id IN "
                    f"(SELECT id FROM aggregated_codes WHERE aggregation_id IN ({ph}))",
                    agg_ids,
                )
                conn.execute(
                    f"DELETE FROM aggregated_codes WHERE aggregation_id IN ({ph})",
                    agg_ids,
                )
                conn.execute(
                    f"DELETE FROM aggregations WHERE id IN ({ph})", agg_ids
                )
            if run_ids:
                ph = ",".join("?" * len(run_ids))
                conn.execute(
                    f"DELETE FROM coder_codes WHERE coder_run_id IN ({ph})",
                    run_ids,
                )
                conn.execute(
                    f"DELETE FROM coder_runs WHERE id IN ({ph})", run_ids
                )
            cur = conn.execute(
                "DELETE FROM segments WHERE segment_id = ?", (segment_id,)
            )
            removed = cur.rowcount
            conn.execute("COMMIT")
            if removed == 0:
                raise HTTPException(status_code=404, detail="segment not found")
            return {
                "removed_segment": True,
                "removed_aggregations": len(agg_ids),
                "removed_coder_runs": len(run_ids),
            }
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ── documents ─────────────────────────────────────────────────────────

    @app.get("/api/documents")
    def list_documents() -> dict[str, Any]:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT d.document_id, d.filename, d.created_at, "
                "  length(d.content) AS size_bytes, "
                "  COUNT(s.segment_id) AS segments_total "
                "FROM documents d "
                "LEFT JOIN segments s ON s.document_id = d.document_id "
                "GROUP BY d.document_id "
                "ORDER BY d.document_id DESC"
            ).fetchall()
            return {"items": _rows_to_dicts(rows)}
        finally:
            conn.close()

    @app.get("/api/documents/{document_id}")
    def get_document(document_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            doc = conn.execute(
                "SELECT document_id, filename, created_at, length(content) AS size_bytes "
                "FROM documents WHERE document_id = ?",
                (document_id,),
            ).fetchone()
            if doc is None:
                raise HTTPException(status_code=404, detail="document not found")
            segs = conn.execute(
                "SELECT segment_id, title, status, text, length(text) AS len "
                "FROM segments WHERE document_id = ? "
                "ORDER BY position IS NULL, position, segment_id",
                (document_id,),
            ).fetchall()
            return {**dict(doc), "segments": _rows_to_dicts(segs)}
        finally:
            conn.close()

    # ── coders (stage 1) ──────────────────────────────────────────────────

    @app.get("/api/coders")
    def get_coders() -> list[dict[str, Any]]:
        conn = _conn()
        try:
            return [vars(c) for c in store.list_coders(conn)]
        finally:
            conn.close()

    @app.post("/api/coders")
    def post_coder(body: CoderIn) -> dict[str, Any]:
        conn = _conn()
        try:
            inserted = store.add_coder(conn, body.coder_id, body.identity)
            return {"inserted": inserted, "coder_id": body.coder_id}
        finally:
            conn.close()

    @app.delete("/api/coders/{coder_id}")
    def delete_coder(coder_id: str, force: bool = False) -> dict[str, Any]:
        conn = _conn()
        try:
            try:
                removed, runs_deleted = store.remove_coder(
                    conn, coder_id, force=force
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not removed:
                raise HTTPException(status_code=404, detail="coder not found")
            return {"removed": True, "runs_deleted": runs_deleted}
        finally:
            conn.close()

    # ── coder runs ────────────────────────────────────────────────────────

    @app.get("/api/coder-runs")
    def list_coder_runs(
        coder_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            where: list[str] = []
            params: list[Any] = []
            if coder_id:
                where.append("cr.coder_id = ?")
                params.append(coder_id)
            if status:
                where.append("cr.status = ?")
                params.append(status)
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM coder_runs cr {clause}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT cr.id, cr.segment_id, cr.coder_id, cr.codebook_version, "
                f"  cr.status, cr.claimed_at, cr.finished_at, cr.error, "
                f"  (SELECT COUNT(*) FROM coder_codes WHERE coder_run_id = cr.id) "
                f"    AS n_codes "
                f"FROM coder_runs cr {clause} "
                f"ORDER BY cr.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {"total": total, "items": _rows_to_dicts(rows)}
        finally:
            conn.close()

    @app.delete("/api/coder-runs/{run_id}")
    def delete_coder_run(run_id: int) -> dict[str, Any]:
        """Delete a single coder_run row (and its codes). The pipeline will
        recreate it next time `ta-stage1 code <coder_id>` runs."""
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT segment_id FROM coder_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            segment_id = row["segment_id"]
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM coder_codes WHERE coder_run_id = ?", (run_id,)
            )
            conn.execute("DELETE FROM coder_runs WHERE id = ?", (run_id,))
            # Also drop any downstream aggregation for this segment if present,
            # because the aggregator key is (segment_id) and the previous
            # aggregation no longer corresponds to current coder output.
            agg = conn.execute(
                "SELECT id FROM aggregations WHERE segment_id = ?", (segment_id,)
            ).fetchone()
            removed_agg = False
            if agg is not None:
                _cascade_delete_aggregation(conn, agg["id"])
                removed_agg = True
            # Reset segment status so it gets picked up again.
            conn.execute(
                "UPDATE segments SET status='pending' WHERE segment_id = ?",
                (segment_id,),
            )
            conn.execute("COMMIT")
            return {"removed": True, "removed_aggregation": removed_agg}
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @app.patch("/api/coder-codes/{run_id}/{position}")
    def edit_coder_code(
        run_id: int, position: int, body: CodeEdit
    ) -> dict[str, str]:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE coder_codes SET code = ? "
                "WHERE coder_run_id = ? AND position = ?",
                (body.code, run_id, position),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="code not found")
            return {"status": "ok"}
        finally:
            conn.close()

    # ── aggregations ──────────────────────────────────────────────────────

    @app.get("/api/aggregations")
    def list_aggregations(
        status: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            where = "WHERE status = ?" if status else ""
            params: list[Any] = [status] if status else []
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM aggregations {where}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT a.id, a.segment_id, a.status, a.created_at, "
                f"  a.finished_at, a.error, "
                f"  (SELECT COUNT(*) FROM aggregated_codes WHERE aggregation_id = a.id) "
                f"    AS n_codes "
                f"FROM aggregations a {where} "
                f"ORDER BY a.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {"total": total, "items": _rows_to_dicts(rows)}
        finally:
            conn.close()

    @app.delete("/api/aggregations/{agg_id}")
    def delete_aggregation(agg_id: int) -> dict[str, Any]:
        """Delete an aggregation row + its aggregated_codes + review_decisions.
        The segment goes back to status='coding' so `aggregate` picks it up."""
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT segment_id FROM aggregations WHERE id = ?", (agg_id,)
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="aggregation not found")
            segment_id = row["segment_id"]
            conn.execute("BEGIN")
            _cascade_delete_aggregation(conn, agg_id)
            conn.execute(
                "UPDATE segments SET status='coding' WHERE segment_id = ?",
                (segment_id,),
            )
            conn.execute("COMMIT")
            return {"removed": True}
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @app.patch("/api/aggregated-codes/{ac_id}")
    def edit_aggregated_code(ac_id: int, body: CodeEdit) -> dict[str, str]:
        conn = _conn()
        try:
            cur = conn.execute(
                "UPDATE aggregated_codes SET code = ? WHERE id = ?",
                (body.code, ac_id),
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="code not found")
            return {"status": "ok"}
        finally:
            conn.close()

    @app.delete("/api/aggregated-codes/{ac_id}")
    def delete_aggregated_code(ac_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM review_decisions WHERE aggregated_code_id = ?",
                (ac_id,),
            )
            cur = conn.execute(
                "DELETE FROM aggregated_codes WHERE id = ?", (ac_id,)
            )
            conn.execute("COMMIT")
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="code not found")
            return {"status": "ok"}
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ── review decisions ──────────────────────────────────────────────────

    @app.get("/api/review-decisions")
    def list_review_decisions(
        decision: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            where = "WHERE rd.decision = ?" if decision else ""
            params: list[Any] = [decision] if decision else []
            total = conn.execute(
                f"SELECT COUNT(*) AS n FROM review_decisions rd {where}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT rd.id, rd.aggregated_code_id, rd.decision, "
                f"  rd.target_code, rd.rationale, rd.applied, "
                f"  rd.resulting_version, rd.created_at, "
                f"  ac.code AS aggregated_code, a.segment_id "
                f"FROM review_decisions rd "
                f"JOIN aggregated_codes ac ON ac.id = rd.aggregated_code_id "
                f"JOIN aggregations a ON a.id = ac.aggregation_id "
                f"{where} ORDER BY rd.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
            return {"total": total, "items": _rows_to_dicts(rows)}
        finally:
            conn.close()

    @app.delete("/api/review-decisions/{rd_id}")
    def delete_review_decision(rd_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            cur = conn.execute(
                "DELETE FROM review_decisions WHERE id = ?", (rd_id,)
            )
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="decision not found")
            return {"status": "ok"}
        finally:
            conn.close()

    # ── codebook versions ─────────────────────────────────────────────────

    @app.get("/api/codebook/versions")
    def list_codebook_versions() -> list[dict[str, Any]]:
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT version, parent_version, created_by, created_at, "
                "  length(snapshot_json) AS bytes "
                "FROM codebook_versions ORDER BY version DESC"
            ).fetchall()
            out = []
            for r in rows:
                # Cheap: parse snapshot once to count codes.
                snap = conn.execute(
                    "SELECT snapshot_json FROM codebook_versions WHERE version = ?",
                    (r["version"],),
                ).fetchone()
                n = len(json.loads(snap["snapshot_json"]).get("codes", []))
                out.append({**dict(r), "n_codes": n})
            return out
        finally:
            conn.close()

    @app.get("/api/codebook/versions/{version}")
    def get_codebook_version(version: int) -> dict[str, Any]:
        conn = _conn()
        try:
            cv = store.get_codebook_version(conn, version)
            if cv is None:
                raise HTTPException(status_code=404, detail="version not found")
            return {
                "version": cv.version,
                "parent_version": cv.parent_version,
                "created_by": cv.created_by,
                "created_at": cv.created_at,
                "codebook": json.loads(cv.snapshot_json),
            }
        finally:
            conn.close()

    # ── stage 2: theme coders ─────────────────────────────────────────────

    @app.get("/api/theme-coders")
    def get_theme_coders() -> list[dict[str, Any]]:
        conn = _conn()
        try:
            return [vars(c) for c in store.list_theme_coders(conn)]
        finally:
            conn.close()

    @app.post("/api/theme-coders")
    def post_theme_coder(body: CoderIn) -> dict[str, Any]:
        conn = _conn()
        try:
            inserted = store.add_theme_coder(conn, body.coder_id, body.identity)
            return {"inserted": inserted, "theme_coder_id": body.coder_id}
        finally:
            conn.close()

    @app.delete("/api/theme-coders/{theme_coder_id}")
    def delete_theme_coder(
        theme_coder_id: str, force: bool = False
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            try:
                removed, n = store.remove_theme_coder(
                    conn, theme_coder_id, force=force
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not removed:
                raise HTTPException(
                    status_code=404, detail="theme coder not found"
                )
            return {"removed": True, "runs_deleted": n}
        finally:
            conn.close()

    @app.get("/api/theme-coder-runs")
    def list_theme_coder_runs(
        codebook_version: int | None = None,
    ) -> list[dict[str, Any]]:
        conn = _conn()
        try:
            if codebook_version is None:
                latest = store.latest_codebook_version(conn)
                codebook_version = latest.version if latest else 0
            rows = conn.execute(
                "SELECT id, theme_coder_id, codebook_version, status, "
                "  claimed_at, finished_at, error, "
                "  CASE WHEN result_json IS NULL THEN 0 "
                "       ELSE length(result_json) END AS result_bytes "
                "FROM theme_coder_runs "
                "WHERE codebook_version = ? "
                "ORDER BY theme_coder_id",
                (codebook_version,),
            ).fetchall()
            return _rows_to_dicts(rows)
        finally:
            conn.close()

    @app.get("/api/theme-coder-runs/{run_id}")
    def get_theme_coder_run(run_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT id, theme_coder_id, codebook_version, status, "
                "  claimed_at, finished_at, error, result_json, raw_response "
                "FROM theme_coder_runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            d = dict(row)
            d["result"] = (
                json.loads(d.pop("result_json")) if d.get("result_json") else None
            )
            return d
        finally:
            conn.close()

    @app.delete("/api/theme-coder-runs/{run_id}")
    def delete_theme_coder_run(run_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM theme_aggregation_inputs WHERE theme_coder_run_id = ?",
                (run_id,),
            )
            cur = conn.execute(
                "DELETE FROM theme_coder_runs WHERE id = ?", (run_id,)
            )
            conn.execute("COMMIT")
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="run not found")
            return {"status": "ok"}
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ── stage 2: theme aggregation / final themes ─────────────────────────

    @app.get("/api/theme-aggregation")
    def get_theme_aggregation(
        codebook_version: int | None = None,
    ) -> dict[str, Any] | None:
        conn = _conn()
        try:
            if codebook_version is None:
                latest = store.latest_codebook_version(conn)
                codebook_version = latest.version if latest else 0
            row = store.latest_theme_aggregation(conn, codebook_version)
            if row is None:
                return None
            d = dict(row)
            d["result"] = (
                json.loads(d.pop("result_json")) if d.get("result_json") else None
            )
            return d
        finally:
            conn.close()

    @app.delete("/api/theme-aggregations/{agg_id}")
    def delete_theme_aggregation(agg_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM theme_aggregation_inputs WHERE theme_aggregation_id = ?",
                (agg_id,),
            )
            cur = conn.execute(
                "DELETE FROM theme_aggregations WHERE id = ?", (agg_id,)
            )
            conn.execute("COMMIT")
            if cur.rowcount == 0:
                raise HTTPException(
                    status_code=404, detail="aggregation not found"
                )
            return {"status": "ok"}
        except HTTPException:
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    # ── static SPA ────────────────────────────────────────────────────────

    static_dir = Path(__file__).parent / "web_static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        # SPA: mount built assets, fall back to index.html for client routes.
        assets_dir = static_dir / "assets"
        if assets_dir.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=str(assets_dir)),
                name="assets",
            )

        @app.get("/")
        def _index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/{full_path:path}")
        def _spa_fallback(full_path: str) -> Any:
            # Don't swallow unknown /api/* — surface a 404.
            if full_path.startswith("api/") or full_path == "api":
                raise HTTPException(status_code=404, detail="not found")
            candidate = static_dir / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(static_dir / "index.html")
    else:

        @app.get("/")
        def _no_ui() -> JSONResponse:
            return JSONResponse(
                {
                    "error": "frontend not built",
                    "hint": (
                        "Run `npm install && npm run build` in the frontend/ "
                        "directory; the built files must be in "
                        "src/thematic_analysis_inc/web_static/."
                    ),
                }
            )

    return app


def _cascade_delete_aggregation(conn: sqlite3.Connection, agg_id: int) -> None:
    conn.execute(
        "DELETE FROM review_decisions WHERE aggregated_code_id IN "
        "(SELECT id FROM aggregated_codes WHERE aggregation_id = ?)",
        (agg_id,),
    )
    conn.execute(
        "DELETE FROM aggregated_codes WHERE aggregation_id = ?", (agg_id,)
    )
    conn.execute("DELETE FROM aggregations WHERE id = ?", (agg_id,))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ta-web",
        description="Inspect and edit a thematic-analysis SQLite database via "
        "a web UI.",
    )
    parser.add_argument(
        "--db", required=True, help="Path to the SQLite database file."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (dev only).",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        # Allow pointing at a brand-new path — the schema is created on first
        # connect, so an empty DB is still useful (init + status pages work).
        print(f"note: {db_path} does not exist; it will be created", flush=True)

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is not installed. Install with:\n"
            "  pip install 'thematic-analysis[web]'",
            flush=True,
        )
        return 1

    app = create_app(db_path)
    print(f"serving {db_path} at http://{args.host}:{args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
