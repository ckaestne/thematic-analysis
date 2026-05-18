"""FastAPI web server for inspecting and editing the thematic-analysis pipeline.

All SQL lives in `thematic_analysis_inc.db`; this module is just glue.
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
from thematic_analysis_inc import db as store
from thematic_analysis_inc.db import (
    aggregation as db_aggregation,
    cascades as db_cascades,
    coding as db_coding,
    documents as db_documents,
    review as db_review,
    status as db_status,
    theme as db_theme,
)


_DB_PATH: Path | None = None


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("DB path not configured")
    return store.connect(_DB_PATH)


class ResearchContextIn(BaseModel):
    description: str = ""
    tailored_prompts: dict[str, str] = Field(default_factory=dict)


class CoderIn(BaseModel):
    name: str | None = None
    coder_id: str | None = None  # backwards compatibility — treated as name
    identity: str


class CodeEdit(BaseModel):
    code: str


def _coder_progress(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    coders = store.list_coders(conn)
    total = db_theme.total_segments_count(conn)
    out = []
    for c in coders:
        prog = db_coding.list_coder_progress(conn, c.coder_id)
        out.append(
            {
                "coder_id": c.coder_id,
                "name": c.name,
                "identity": c.identity,
                "segments_total": total,
                "runs_done": prog.get("done", 0),
                "runs_running": prog.get("running", 0),
                "runs_failed": prog.get("failed", 0),
            }
        )
    return out


def _theme_coder_progress(
    conn: sqlite3.Connection, codebook_version: int
) -> list[dict[str, Any]]:
    coders = db_theme.list_theme_coders_rows(conn)
    out = []
    for c in coders:
        run = db_theme.latest_theme_coder_run(
            conn, c["theme_coder_id"], codebook_version
        )
        out.append(
            {
                "theme_coder_id": c["theme_coder_id"],
                "identity": c["identity"],
                "run": dict(run) if run else None,
            }
        )
    return out


def _segment_payload(
    conn: sqlite3.Connection, segment_id: int
) -> dict[str, Any]:
    seg = store.get_segment(conn, segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail="segment not found")
    coder_codes = db_coding.load_segment_coder_codes(conn, segment_id)
    coder_blocks: list[dict[str, Any]] = []
    for cc in coder_codes:
        q = db_coding.get_queue_row(conn, segment_id, cc.coder_id)
        status = "pending"
        if q is not None:
            status = db_coding.queue_status_char(q)
        coder_blocks.append(
            {
                "coder_id": cc.coder_id,
                "status": status,
                "codes": [
                    {
                        "code_id": c.code_id,
                        "code": c.code,
                        "rationale": c.rationale,
                        "description": c.description,
                    }
                    for c in cc.codes
                ],
            }
        )

    agg_codes = db_aggregation.list_aggregator_codes_for_segment(conn, segment_id)
    agg_payload: list[dict[str, Any]] = []
    for ac in agg_codes:
        quotes = db_aggregation.load_aggregated_code_quotes(conn, ac["code_id"])
        review_row = db_coding.get_review_edge_for_aggregator_code(
            conn, ac["code_id"]
        )
        agg_payload.append(
            {
                "code_id": ac["code_id"],
                "code": ac["code"],
                "description": ac["description"],
                "rationale": ac["rationale"],
                "quotes": quotes,
                "review": dict(review_row) if review_row else None,
            }
        )
    return {
        "segment_id": seg.segment_id,
        "document_id": seg.document_id,
        "content": seg.content,
        "line_from": seg.line_from,
        "line_to": seg.line_to,
        "position": seg.position,
        "status": db_status.derive_segment_status(conn, segment_id),
        "coder_codes": coder_blocks,
        "aggregator_codes": agg_payload,
    }


def create_app(db_path: str | Path) -> FastAPI:
    global _DB_PATH
    _DB_PATH = Path(db_path)
    store.init_db(_DB_PATH).close()

    app = FastAPI(title="Thematic Analysis Inspector", version="0.2.0")

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
                    "coding_queue_total": s1.coding_queue_total,
                    "coding_queue_by_status": s1.coding_queue_by_status,
                    "aggregator_codes_total": s1.aggregator_codes_total,
                    "reviewer_codes_total": s1.reviewer_codes_total,
                    "review_decisions_by_kind": s1.review_decisions_by_kind,
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
                "per_coder": _coder_progress(conn),
                "per_theme_coder": _theme_coder_progress(conn, codebook_version),
            }
        finally:
            conn.close()

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
                description=ctx.description, tailored_prompts=prompts
            )
            store.set_research_context(conn, new_ctx)
            return {
                "description": new_ctx.description,
                "tailored_prompts": dict(new_ctx.tailored_prompts),
                "roles": list(AGENT_ROLES),
            }
        finally:
            conn.close()

    # ── segments ─────────────────────────────────────────────────────────
    @app.get("/api/segments")
    def list_segments(
        q: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            total, rows = db_documents.list_segments(
                conn, q=q, limit=limit, offset=offset
            )
            items = []
            for r in rows:
                d = dict(r)
                d["status"] = db_status.derive_segment_status(
                    conn, int(d["segment_id"])
                )
                items.append(d)
            return {
                "total": total,
                "items": items,
                "status_counts": db_status.segments_by_derived_status(conn),
            }
        finally:
            conn.close()

    @app.get("/api/segments/{segment_id}")
    def get_segment(segment_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            return _segment_payload(conn, segment_id)
        finally:
            conn.close()

    @app.delete("/api/segments/{segment_id}")
    def delete_segment(segment_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            ok = db_cascades.delete_segment_cascade(conn, segment_id)
            if not ok:
                raise HTTPException(status_code=404, detail="segment not found")
            return {"removed_segment": True}
        finally:
            conn.close()

    # ── documents ────────────────────────────────────────────────────────
    @app.get("/api/documents")
    def list_documents() -> dict[str, Any]:
        conn = _conn()
        try:
            rows = db_documents.list_documents(conn)
            coder_ids = [c.coder_id for c in store.list_coders(conn)]
            return {
                "items": _rows_to_dicts(rows),
                "coder_ids": coder_ids,
            }
        finally:
            conn.close()

    @app.get("/api/documents/{document_id}")
    def get_document(document_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            doc = db_documents.get_document_meta(conn, document_id)
            if doc is None:
                raise HTTPException(status_code=404, detail="document not found")
            segs = db_documents.get_document_segments(conn, document_id)
            seg_list = [
                _segment_payload(conn, int(s["segment_id"])) for s in segs
            ]
            return {**dict(doc), "segments": seg_list}
        finally:
            conn.close()

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            removed, n_segs = db_cascades.delete_document_cascade(
                conn, document_id
            )
            if not removed:
                raise HTTPException(status_code=404, detail="document not found")
            return {"removed_document": True, "removed_segments": n_segs}
        finally:
            conn.close()

    # ── coders ───────────────────────────────────────────────────────────
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
            name = body.name or body.coder_id or ""
            if not name:
                raise HTTPException(status_code=422, detail="name required")
            existing = store.get_coder_by_name(conn, name)
            if existing is not None:
                return {
                    "inserted": False,
                    "coder_id": existing.coder_id,
                    "name": existing.name,
                }
            coder = store.add_coder(conn, name=name, identity=body.identity)
            return {
                "inserted": True,
                "coder_id": coder.coder_id,
                "name": coder.name,
            }
        finally:
            conn.close()

    @app.delete("/api/coders/{coder_id}")
    def delete_coder(coder_id: int, force: bool = False) -> dict[str, Any]:
        conn = _conn()
        try:
            try:
                removed, n = db_cascades.delete_coder_cascade(
                    conn, coder_id, force=force
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not removed:
                raise HTTPException(status_code=404, detail="coder not found")
            return {"removed": True, "queue_rows_deleted": n}
        finally:
            conn.close()

    # ── coding queue (replaces coder-runs) ───────────────────────────────
    @app.get("/api/coding-queue")
    def list_coding_queue(
        coder_id: int | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            total, items = db_coding.list_queue_rows(
                conn, coder_id=coder_id, limit=limit, offset=offset
            )
            return {"total": total, "items": items}
        finally:
            conn.close()

    @app.delete("/api/coding-queue/{segment_id}/{coder_id}")
    def reset_coding_assignment(
        segment_id: int, coder_id: int
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            ok = db_cascades.reset_coding_assignment(
                conn, segment_id=segment_id, coder_id=coder_id
            )
            if not ok:
                raise HTTPException(status_code=404, detail="queue row not found")
            return {"removed": True}
        finally:
            conn.close()

    @app.patch("/api/codes/{code_id}")
    def edit_code(code_id: int, body: CodeEdit) -> dict[str, str]:
        conn = _conn()
        try:
            ok = db_coding.edit_code_text(conn, code_id, body.code)
            if not ok:
                raise HTTPException(status_code=404, detail="code not found")
            return {"status": "ok"}
        finally:
            conn.close()

    # ── aggregations ─────────────────────────────────────────────────────
    @app.get("/api/aggregations")
    def list_aggregations(
        limit: int = Query(default=100, le=1000), offset: int = 0
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            total, items = db_aggregation.list_aggregations(
                conn, limit=limit, offset=offset
            )
            return {"total": total, "items": items}
        finally:
            conn.close()

    @app.delete("/api/aggregations/segment/{segment_id}")
    def delete_aggregation(segment_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            n = db_cascades.delete_aggregation_for_segment_cascade(
                conn, segment_id
            )
            return {"removed_aggregator_codes": n}
        finally:
            conn.close()

    # ── review decisions ─────────────────────────────────────────────────
    @app.get("/api/review-decisions")
    def list_review_decisions(
        decision: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        conn = _conn()
        try:
            total, items = db_review.list_review_decisions(
                conn, decision=decision, limit=limit, offset=offset
            )
            return {"total": total, "items": items}
        finally:
            conn.close()

    # ── codebook versions ────────────────────────────────────────────────
    @app.get("/api/codebook/versions")
    def list_codebook_versions() -> list[dict[str, Any]]:
        conn = _conn()
        try:
            versions = store.list_codebook_versions(conn)
            out = []
            for cv in versions:
                n = len(store.get_codebook_codes(conn, cv.version))
                out.append(
                    {
                        "version": cv.version,
                        "parent_version": cv.parent_version,
                        "created_by": cv.created_by,
                        "created_at": cv.created_at,
                        "n_codes": n,
                    }
                )
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
            snapshot = store.codebook_to_json_for_version(conn, version)
            return {
                "version": cv.version,
                "parent_version": cv.parent_version,
                "created_by": cv.created_by,
                "created_at": cv.created_at,
                "codebook": json.loads(snapshot),
            }
        finally:
            conn.close()

    # ── stage 2: theme coders ────────────────────────────────────────────
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
            name = body.name or body.coder_id or ""
            if not name:
                raise HTTPException(status_code=422, detail="name required")
            inserted = store.add_theme_coder(conn, name, body.identity)
            return {"inserted": inserted, "theme_coder_id": name}
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
            rows = db_theme.list_theme_coder_runs_for_version(
                conn, codebook_version
            )
            return _rows_to_dicts(rows)
        finally:
            conn.close()

    @app.get("/api/theme-coder-runs/{run_id}")
    def get_theme_coder_run(run_id: int) -> dict[str, Any]:
        conn = _conn()
        try:
            row = db_theme.get_theme_coder_run(conn, run_id)
            if row is None:
                raise HTTPException(status_code=404, detail="run not found")
            d = dict(row)
            d["result"] = (
                json.loads(d.pop("result_json"))
                if d.get("result_json")
                else None
            )
            return d
        finally:
            conn.close()

    @app.delete("/api/theme-coder-runs/{run_id}")
    def delete_theme_coder_run(run_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            ok = db_theme.delete_theme_coder_run(conn, run_id)
            if not ok:
                raise HTTPException(status_code=404, detail="run not found")
            return {"status": "ok"}
        finally:
            conn.close()

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
                json.loads(d.pop("result_json"))
                if d.get("result_json")
                else None
            )
            return d
        finally:
            conn.close()

    @app.delete("/api/theme-aggregations/{agg_id}")
    def delete_theme_aggregation(agg_id: int) -> dict[str, str]:
        conn = _conn()
        try:
            ok = db_theme.delete_theme_aggregation(conn, agg_id)
            if not ok:
                raise HTTPException(
                    status_code=404, detail="aggregation not found"
                )
            return {"status": "ok"}
        finally:
            conn.close()

    # ── static SPA ───────────────────────────────────────────────────────
    static_dir = Path(__file__).parent / "web_static"
    if static_dir.exists() and (static_dir / "index.html").exists():
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ta-web",
        description=(
            "Inspect and edit a thematic-analysis SQLite database via a web UI."
        ),
    )
    parser.add_argument("--db", required=True, help="Path to the SQLite DB.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
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
