"""FastAPI web server for inspecting and editing the thematic-analysis pipeline.

Stage-1 endpoints use the SQLModel-backed helpers in
:mod:`thematic_analysis_inc.db` directly (no ``conn`` threading).
Stage-2 endpoints still open a ``sqlite3.Connection`` for the
``theme_*`` tables.
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


def _conn() -> sqlite3.Connection:
    if _DB_PATH is None:
        raise RuntimeError("DB path not configured")
    return store.connect(_DB_PATH)


def _ensure_connected() -> None:
    """Open the engine + sqlite layer for the current request. The sqlite
    connection is closed; the engine stays cached."""
    if _DB_PATH is None:
        raise RuntimeError("DB path not configured")
    c = store.connect(_DB_PATH)
    c.close()


class ResearchContextIn(BaseModel):
    description: str = ""
    tailored_prompts: dict[str, str] = Field(default_factory=dict)


class CoderIn(BaseModel):
    identity: str


class CodeEdit(BaseModel):
    code: str


def _coder_payload(c) -> dict[str, Any]:
    return {
        "coder_id": c.coder_id,
        "identity": c.identity,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _segment_summary(seg) -> dict[str, Any]:
    return {
        "segment_id": seg.segment_id,
        "document_id": seg.document_id,
        "line_from": seg.line_from,
        "line_to": seg.line_to,
        "position": seg.position,
        "preview": seg.content[:240],
        "len": len(seg.content),
    }


def _coder_progress() -> list[dict[str, Any]]:
    coders = store.list_coders()
    total_segments = 0
    with store.session() as s:
        from sqlalchemy import func
        from sqlmodel import select
        total_segments = int(
            s.exec(select(func.count()).select_from(store.Segment)).one()
        )
    out = []
    for c in coders:
        prog = db_coding.coder_progress(c)
        out.append(
            {
                "coder_id": c.coder_id,
                "identity": c.identity,
                "segments_total": total_segments,
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


def _segment_payload(segment_id: int) -> dict[str, Any]:
    seg = store.get_segment(segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail="segment not found")
    coder_codes = db_coding.load_segment_coder_codes(seg)
    coder_blocks: list[dict[str, Any]] = []
    for cid in sorted(coder_codes):
        q = db_coding.get_queue_entry(segment_id, cid)
        status = q.status if q is not None else "pending"
        coder_blocks.append(
            {
                "coder_id": cid,
                "status": status,
                "codes": [
                    {
                        "code_id": c.code_id,
                        "code": c.code,
                        "rationale": c.rationale,
                        "description": c.description,
                    }
                    for c in coder_codes[cid]
                ],
            }
        )

    agg_codes = db_aggregation.list_aggregator_codes_for_segment(segment_id)
    agg_payload: list[dict[str, Any]] = []
    for ac in agg_codes:
        quotes = db_aggregation.load_aggregated_code_quotes(ac.code_id)
        # Find the review edge (if any).
        from sqlmodel import select
        from thematic_analysis_inc.db.models import CodesDerived
        with store.session() as s:
            edge = s.exec(
                select(CodesDerived).where(
                    CodesDerived.source_code_id == ac.code_id,
                    CodesDerived.derivation_type == "R",
                )
            ).first()
            edge_payload = (
                {
                    "new_code_id": edge.new_code_id,
                    "decision": edge.decision,
                    "rationale": edge.rationale,
                }
                if edge is not None
                else None
            )
        agg_payload.append(
            {
                "code_id": ac.code_id,
                "code": ac.code,
                "description": ac.description,
                "rationale": ac.rationale,
                "quotes": quotes,
                "review": edge_payload,
            }
        )
    return {
        "segment_id": seg.segment_id,
        "document_id": seg.document_id,
        "content": seg.content,
        "line_from": seg.line_from,
        "line_to": seg.line_to,
        "position": seg.position,
        "status": db_status.derive_segment_status(seg),
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
            s1 = store.status_counts()
            latest = store.latest_codebook()
            codebook_version = latest.version if latest else 0
            s2 = store.stage2_status_counts(conn, codebook_version)
            rc_row = store.get_research_context()
            ctx = (
                store.research_context_to_domain(rc_row)
                if rc_row is not None
                else None
            )
            latest_rc_version = (
                rc_row.research_context_version if rc_row is not None else None
            )
            db_size = _DB_PATH.stat().st_size if _DB_PATH and _DB_PATH.exists() else 0
            return {
                "db_path": str(_DB_PATH),
                "db_size_bytes": db_size,
                "research_context_set": ctx is not None,
                "latest_research_context_version": latest_rc_version,
                "stage1": {
                    "segments_total": s1.segments_total,
                    "segments_by_status": s1.segments_by_status,
                    "coders_total": s1.coders_total,
                    "coding_queue_total": s1.coding_queue_total,
                    "coding_queue_by_status": s1.coding_queue_by_status,
                    "aggregator_codes_total": s1.aggregator_codes_total,
                    "aggregator_segments_total": s1.aggregator_segments_total,
                    "reviewer_codes_total": s1.reviewer_codes_total,
                    "review_decisions_by_kind": s1.review_decisions_by_kind,
                    "codebook_version": s1.codebook_version,
                    "codebook_codes": s1.codebook_codes,
                    # Legacy aliases the bundled SPA references.
                    "coder_runs_total": s1.coding_queue_total,
                    "coder_runs_by_status": s1.coding_queue_by_status,
                    "aggregations_total": s1.aggregator_segments_total,
                    "aggregations_by_status": (
                        {"done": s1.aggregator_segments_total}
                        if s1.aggregator_segments_total
                        else {}
                    ),
                    "review_decisions_total": sum(
                        s1.review_decisions_by_kind.values()
                    ),
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
                "per_coder": _coder_progress(),
                "per_theme_coder": _theme_coder_progress(conn, codebook_version),
            }
        finally:
            conn.close()

    def _rc_payload(rc) -> dict[str, Any]:
        ctx = store.research_context_to_domain(rc)
        return {
            "research_context_version": rc.research_context_version,
            "description": ctx.description,
            "tailored_prompts": dict(ctx.tailored_prompts),
            "roles": list(AGENT_ROLES),
        }

    @app.get("/api/research-context")
    def get_research_context() -> dict[str, Any] | None:
        _ensure_connected()
        rc = store.get_research_context()
        return _rc_payload(rc) if rc is not None else None

    @app.get("/api/research-context/versions")
    def list_research_context_versions() -> list[dict[str, Any]]:
        _ensure_connected()
        return [
            {
                "research_context_version": r.research_context_version,
                "description": r.description or "",
                "created_at": (
                    r.created_at.isoformat() if r.created_at else None
                ),
            }
            for r in store.list_research_context_versions()
        ]

    @app.get("/api/research-context/versions/{v}")
    def get_research_context_version(v: int) -> dict[str, Any]:
        _ensure_connected()
        rc = store.get_research_context(version=v)
        if rc is None:
            raise HTTPException(
                status_code=404, detail="research context version not found"
            )
        return _rc_payload(rc)

    @app.put("/api/research-context")
    def put_research_context(body: ResearchContextIn) -> dict[str, Any]:
        _ensure_connected()
        ctx = ResearchContext(
            description=body.description,
            tailored_prompts={
                k: v
                for k, v in body.tailored_prompts.items()
                if k in AGENT_ROLES and v
            },
        )
        rc = store.set_research_context(ctx)
        return {
            "status": "ok",
            "research_context_version": rc.research_context_version,
            "description": ctx.description,
            "tailored_prompts": dict(ctx.tailored_prompts),
        }

    @app.delete("/api/research-context")
    def delete_research_context() -> dict[str, bool]:
        _ensure_connected()
        return {"removed": store.clear_research_context()}

    @app.post("/api/research-context/regenerate-prompts")
    def regenerate_tailored_prompts() -> dict[str, Any]:
        from thematic_analysis.research_context_tailor import (
            generate_all_tailored_prompts,
        )

        _ensure_connected()
        rc = store.get_research_context()
        ctx = (
            store.research_context_to_domain(rc) if rc is not None else None
        )
        if ctx is None or ctx.is_empty():
            raise HTTPException(
                status_code=400,
                detail="no research context description set",
            )
        prompts = generate_all_tailored_prompts(ctx.description)
        new_ctx = ResearchContext(
            description=ctx.description, tailored_prompts=prompts
        )
        new_rc = store.set_research_context(new_ctx)
        return {
            "research_context_version": new_rc.research_context_version,
            "description": new_ctx.description,
            "tailored_prompts": dict(new_ctx.tailored_prompts),
            "roles": list(AGENT_ROLES),
        }

    # ── segments ─────────────────────────────────────────────────────────
    @app.get("/api/segments")
    def list_segments(
        q: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        _ensure_connected()
        total, rows = db_documents.list_segments(q=q, limit=limit, offset=offset)
        items = []
        for seg in rows:
            d = _segment_summary(seg)
            d["status"] = db_status.derive_segment_status(seg)
            items.append(d)
        return {
            "total": total,
            "items": items,
            "status_counts": db_status.segments_by_derived_status(),
        }

    @app.get("/api/segments/{segment_id}")
    def get_segment(segment_id: int) -> dict[str, Any]:
        _ensure_connected()
        return _segment_payload(segment_id)

    @app.delete("/api/segments/{segment_id}")
    def delete_segment(segment_id: int) -> dict[str, Any]:
        _ensure_connected()
        seg = store.get_segment(segment_id)
        if seg is None:
            raise HTTPException(status_code=404, detail="segment not found")
        ok = db_cascades.delete_segment_cascade(seg)
        if not ok:
            raise HTTPException(status_code=404, detail="segment not found")
        return {"removed_segment": True}

    # ── documents ────────────────────────────────────────────────────────
    @app.get("/api/documents")
    def list_documents() -> dict[str, Any]:
        _ensure_connected()
        docs = store.list_documents()
        items = []
        for d in docs:
            items.append(
                {
                    "document_id": d.document_id,
                    "filename": d.filename,
                    "created_at": (
                        d.created_at.isoformat() if d.created_at else None
                    ),
                    "segments_total": len(d.segments),
                }
            )
        coder_ids = [c.coder_id for c in store.list_coders()]
        return {"items": items, "coder_ids": coder_ids}

    @app.get("/api/documents/{document_id}")
    def get_document(document_id: int) -> dict[str, Any]:
        _ensure_connected()
        with store.session() as s:
            doc = s.get(store.Document, document_id)
            if doc is None:
                raise HTTPException(
                    status_code=404, detail="document not found"
                )
            seg_ids = [seg.segment_id for seg in doc.segments]
            doc_payload = {
                "document_id": doc.document_id,
                "filename": doc.filename,
                "created_at": (
                    doc.created_at.isoformat() if doc.created_at else None
                ),
            }
        seg_list = [_segment_payload(sid) for sid in seg_ids]
        return {**doc_payload, "segments": seg_list}

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: int) -> dict[str, Any]:
        _ensure_connected()
        with store.session() as s:
            doc = s.get(store.Document, document_id)
            if doc is None:
                raise HTTPException(
                    status_code=404, detail="document not found"
                )
            s.expunge(doc)
        removed, n_segs = db_cascades.delete_document_cascade(doc)
        if not removed:
            raise HTTPException(status_code=404, detail="document not found")
        return {"removed_document": True, "removed_segments": n_segs}

    # ── coders ───────────────────────────────────────────────────────────
    @app.get("/api/coders")
    def get_coders() -> list[dict[str, Any]]:
        _ensure_connected()
        return [_coder_payload(c) for c in store.list_coders()]

    @app.put("/api/coders")
    def put_coder(body: CoderIn) -> dict[str, Any]:
        _ensure_connected()
        coder = store.add_coder(body.identity)
        return {
            "inserted": True,
            "coder_id": coder.coder_id,
            "identity": coder.identity,
        }

    # Legacy POST alias for clients that haven't migrated to PUT yet.
    @app.post("/api/coders")
    def post_coder(body: CoderIn) -> dict[str, Any]:
        return put_coder(body)

    @app.delete("/api/coders/{coder_id}")
    def delete_coder(coder_id: int, force: bool = False) -> dict[str, Any]:
        _ensure_connected()
        coder = store.get_coder(coder_id)
        if coder is None:
            raise HTTPException(status_code=404, detail="coder not found")
        try:
            removed, n = db_cascades.delete_coder_cascade(coder, force=force)
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e))
        if not removed:
            raise HTTPException(status_code=404, detail="coder not found")
        return {"removed": True, "queue_rows_deleted": n}

    # ── coding queue (replaces coder-runs) ───────────────────────────────
    @app.get("/api/coding-queue")
    def list_coding_queue(
        coder_id: int | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        _ensure_connected()
        coder = store.get_coder(coder_id) if coder_id is not None else None
        total, entries = db_coding.list_queue_entries(
            coder, limit=limit, offset=offset
        )
        items = []
        for q in entries:
            with store.session() as s:
                from sqlalchemy import func
                from sqlmodel import select

                n_codes = int(
                    s.exec(
                        select(func.count())
                        .select_from(store.Code)
                        .where(
                            store.Code.segment_id == q.segment_id,
                            store.Code.coder_id == q.coder_id,
                        )
                    ).one()
                )
            items.append(
                {
                    "segment_id": q.segment_id,
                    "coder_id": q.coder_id,
                    "codebook_version": q.codebook_used_id,
                    "research_context_version": q.research_context_used_id,
                    "claimed_at": (
                        q.claimed_at.isoformat() if q.claimed_at else None
                    ),
                    "finished_at": (
                        q.finished_at.isoformat() if q.finished_at else None
                    ),
                    "error": q.error,
                    "n_codes": n_codes,
                    "status": q.status,
                }
            )
        return {"total": total, "items": items}

    @app.delete("/api/coding-queue/{segment_id}/{coder_id}")
    def reset_coding_assignment(
        segment_id: int, coder_id: int
    ) -> dict[str, Any]:
        _ensure_connected()
        q = db_coding.get_queue_entry(segment_id, coder_id)
        if q is None:
            raise HTTPException(status_code=404, detail="queue row not found")
        ok = db_cascades.reset_coding_assignment_cascade(q)
        if not ok:
            raise HTTPException(status_code=404, detail="queue row not found")
        return {"removed": True}

    @app.patch("/api/codes/{code_id}")
    def edit_code(code_id: int, body: CodeEdit) -> dict[str, str]:
        _ensure_connected()
        with store.session() as s:
            c = s.get(store.Code, code_id)
            if c is None:
                raise HTTPException(status_code=404, detail="code not found")
            s.expunge(c)
        ok = db_coding.edit_code_text(c, body.code)
        if not ok:
            raise HTTPException(status_code=404, detail="code not found")
        return {"status": "ok"}

    # ── aggregations ─────────────────────────────────────────────────────
    @app.get("/api/aggregations")
    def list_aggregations(
        limit: int = Query(default=100, le=1000), offset: int = 0
    ) -> dict[str, Any]:
        _ensure_connected()
        total, items = db_aggregation.list_aggregations(
            limit=limit, offset=offset
        )
        return {"total": total, "items": items}

    @app.delete("/api/aggregations/segment/{segment_id}")
    def delete_aggregation(segment_id: int) -> dict[str, Any]:
        _ensure_connected()
        seg = store.get_segment(segment_id)
        if seg is None:
            return {"removed_aggregator_codes": 0}
        n = db_cascades.delete_aggregation_for_segment_cascade(seg)
        return {"removed_aggregator_codes": n}

    # ── review decisions ─────────────────────────────────────────────────
    @app.get("/api/review-decisions")
    def list_review_decisions(
        decision: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        _ensure_connected()
        total, rows = db_review.list_review_decisions(
            decision, limit=limit, offset=offset
        )
        items = []
        with store.session() as s:
            for d in rows:
                src = s.get(store.Code, d.source_code_id)
                new = s.get(store.Code, d.new_code_id)
                items.append(
                    {
                        "new_code_id": d.new_code_id,
                        "source_code_id": d.source_code_id,
                        "decision": d.decision,
                        "rationale": d.rationale,
                        "source_code": src.code if src else None,
                        "segment_id": src.segment_id if src else None,
                        "new_code": new.code if new else None,
                    }
                )
        return {"total": total, "items": items}

    # ── codebook versions ────────────────────────────────────────────────
    @app.get("/api/codebook/versions")
    def list_codebook_versions() -> list[dict[str, Any]]:
        _ensure_connected()
        out = []
        for cv in store.list_codebooks():
            snap = json.loads(store.codebook_to_json_for_version(cv.version))
            out.append(
                {
                    "version": cv.version,
                    "parent_version": cv.parent_version,
                    "created_at": (
                        cv.created_at.isoformat() if cv.created_at else None
                    ),
                    "n_codes": len(snap.get("codes", [])),
                }
            )
        return out

    @app.get("/api/codebook/versions/{version}")
    def get_codebook_version(version: int) -> dict[str, Any]:
        _ensure_connected()
        cv = store.get_codebook(version)
        if cv is None:
            raise HTTPException(status_code=404, detail="version not found")
        snapshot = store.codebook_to_json_for_version(version)
        return {
            "version": cv.version,
            "parent_version": cv.parent_version,
            "created_at": (
                cv.created_at.isoformat() if cv.created_at else None
            ),
            "codebook": json.loads(snapshot),
        }

    # ── stage 2: theme coders ────────────────────────────────────────────
    @app.get("/api/theme-coders")
    def get_theme_coders() -> list[dict[str, Any]]:
        conn = _conn()
        try:
            return [vars(c) for c in store.list_theme_coders(conn)]
        finally:
            conn.close()

    @app.post("/api/theme-coders")
    def post_theme_coder(body: dict) -> dict[str, Any]:
        conn = _conn()
        try:
            name = body.get("name") or body.get("coder_id") or ""
            identity = body.get("identity", "")
            if not name:
                raise HTTPException(status_code=422, detail="name required")
            inserted = store.add_theme_coder(conn, name, identity)
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
                latest = store.latest_codebook()
                codebook_version = latest.version if latest else 0
            rows = db_theme.list_theme_coder_runs_for_version(
                conn, codebook_version
            )
            return [dict(r) for r in rows]
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
                latest = store.latest_codebook()
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
