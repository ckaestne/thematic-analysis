"""FastAPI web server for inspecting and editing the thematic-analysis pipeline.

Endpoints use the SQLModel-backed helpers in
:mod:`thematic_analysis_inc.db` directly (no ``conn`` threading).
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from thematic_analysis.llm_config import llm_configured
from thematic_analysis.research_context import AGENT_ROLES, ResearchContext
from thematic_analysis_inc import db as store, workers
from thematic_analysis_inc.db import (
    aggregation as db_aggregation,
    cascades as db_cascades,
    coding as db_coding,
    documents as db_documents,
    review as db_review,
    status as db_status,
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


class EnqueueIn(BaseModel):
    coder_ids: list[int] | None = None


class ThemeCodingJobIn(BaseModel):
    codebook_version: int
    prompt: str


class ManualThemeIn(BaseModel):
    title: str
    description: str = ""
    rationale: str = ""


class BackgroundRunnerStartIn(BaseModel):
    workers: int = 1
    use_mock_embeddings: bool = False


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
        "title": seg.title,
        "line_from": seg.line_from,
        "line_to": seg.line_to,
        "position": seg.position,
        "preview": seg.content[:240],
        "len": len(seg.content),
    }


def _coder_progress() -> list[dict[str, Any]]:
    coders = store.list_coders()
    total_segments = store.count_segments()
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


def _queue_state(
    max_cb: int | None, latest_cb: int | None
) -> str:
    """Classify a document/segment for the enqueue button UI.

    - "latest": queued at the most recent codebook revision — clicking
      enqueue would be a no-op for that revision.
    - "earlier": queued, but only at a strictly older codebook revision —
      the UI greys out the button and asks for a confirming second click.
    - "none": never queued.
    """
    if max_cb is None:
        return "none"
    if latest_cb is not None and max_cb >= latest_cb:
        return "latest"
    return "earlier"


def _segment_payload(segment_id: int) -> dict[str, Any]:
    seg = store.get_segment(segment_id)
    if seg is None:
        raise HTTPException(status_code=404, detail="segment not found")
    from thematic_analysis_inc.db.models import Document, is_sentinel_code

    document_filename: str | None = None
    if seg.document_id is not None:
        with store.session() as s:
            doc = s.get(Document, seg.document_id)
            document_filename = doc.filename if doc is not None else None

    coder_codes = db_coding.load_segment_coder_codes(seg)
    queue_entries = db_coding.list_queue_entries_for_segment(segment_id)
    queue_entries.sort(key=lambda q: q.coder_id)
    max_cb = max((q.codebook_used_id for q in queue_entries), default=None)
    latest = store.latest_codebook()
    latest_cb_version = latest.version if latest is not None else None
    queue_state = _queue_state(max_cb, latest_cb_version)
    coder_blocks: list[dict[str, Any]] = []
    for q in queue_entries:
        codes_for_coder = coder_codes.get(q.coder_id, [])
        real_codes = [c for c in codes_for_coder if not is_sentinel_code(c)]
        no_codes = bool(codes_for_coder) and not real_codes
        coder_blocks.append(
            {
                "coder_id": q.coder_id,
                "status": q.status,
                "codebook_version": q.codebook_used_id,
                "finished_at": (
                    q.finished_at.isoformat() if q.finished_at else None
                ),
                "no_codes": no_codes,
                "codes": [
                    {
                        "code_id": c.code_id,
                        "code": c.code,
                        "rationale": c.rationale,
                        "description": c.description,
                        "quotes": [
                            {"quote_id": str(q.quote_id), "text": q.text}
                            for q in c.supporting_quotes
                        ],
                    }
                    for c in real_codes
                ],
            }
        )

    # `list_aggregator_codes_for_segment` already filters out the empty-
    # aggregation sentinel. To detect the "ran, produced nothing" state
    # we ask `segment_has_aggregator_code` (which counts the sentinel as
    # well) and report no_codes when an aggregator row exists but no real
    # ones came back.
    agg_codes = db_aggregation.list_aggregator_codes_for_segment(segment_id)
    aggregator_no_codes = (
        db_aggregation.segment_has_aggregator_code(segment_id)
        and not agg_codes
    )
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
                "codebook_used_id": ac.codebook_used_id,
                "quotes": quotes,
                "review": edge_payload,
            }
        )
    return {
        "segment_id": seg.segment_id,
        "document_id": seg.document_id,
        "document_filename": document_filename,
        "title": seg.title,
        "content": seg.content,
        "line_from": seg.line_from,
        "line_to": seg.line_to,
        "position": seg.position,
        "status": db_status.derive_segment_status(seg),
        "coder_codes": coder_blocks,
        "aggregator_codes": agg_payload,
        "aggregator_no_codes": aggregator_no_codes,
        "queue_state": queue_state,
    }


def create_app(db_path: str | Path) -> FastAPI:
    global _DB_PATH
    _DB_PATH = Path(db_path)
    store.init_db(_DB_PATH).close()

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            try:
                workers.get_background_runner().stop(timeout=10.0)
            except Exception:
                pass

    app = FastAPI(
        title="Thematic Analysis Inspector",
        version="0.2.0",
        lifespan=_lifespan,
    )

    @app.get("/api/status")
    def get_status() -> dict[str, Any]:
        s1 = store.status_counts()
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
            "research_context_description": (
                ctx.description if ctx is not None else ""
            ),
            "latest_research_context_version": latest_rc_version,
            "stage1": {
                "documents_total": s1.documents_total,
                "segments_total": s1.segments_total,
                "segments_by_status": s1.segments_by_status,
                "codes_total": s1.codes_total,
                "quotes_total": s1.quotes_total,
                "themes_total": s1.themes_total,
                "coders_total": s1.coders_total,
                "coding_queue_total": s1.coding_queue_total,
                "coding_queue_by_status": s1.coding_queue_by_status,
                "aggregator_codes_total": s1.aggregator_codes_total,
                "aggregator_segments_total": s1.aggregator_segments_total,
                "reviewer_codes_total": s1.reviewer_codes_total,
                "review_decisions_by_kind": s1.review_decisions_by_kind,
                "codebook_version": s1.codebook_version,
                "codebook_codes": s1.codebook_codes,
                "documents_by_coding_status": s1.documents_by_coding_status,
                "reviews_pending": s1.reviews_pending,
                "reviews_completed_since_codebook": (
                    s1.reviews_completed_since_codebook
                ),
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
                "review_decisions_applied": sum(
                    s1.review_decisions_by_kind.values()
                ),
            },
            "per_coder": _coder_progress(),
        }

    @app.get("/api/codebook-preview")
    def get_codebook_preview() -> dict[str, Any]:
        """Preview the next codebook revision without writing it.

        Uses the same mechanism as ``materialize_codebook_revision``:
        ``live_codes_for_batch(latest)`` is the effective membership the
        next revision would have. Returns the *diff* against the latest
        revision — codes that would be added (new reviewer codes) and
        codes that would be removed (replaced by merges) — so the user
        can see what the next codebook will look like.
        """
        _ensure_connected()
        from sqlalchemy.orm import selectinload
        from sqlmodel import select
        from thematic_analysis_inc.db.codebook import live_codes_for_batch
        from thematic_analysis_inc.db.models import (
            Code,
            CodesDerived,
            DERIVATION_REVIEW,
            Segment,
        )

        latest = store.latest_codebook()
        if latest is None:
            return {
                "parent_version": None,
                "added": [],
                "removed": [],
                "unchanged_count": 0,
                "has_changes": False,
            }
        parent_full = store.get_codebook_with_codes_and_research_context(
            latest.version
        )
        if parent_full is None:
            return {
                "parent_version": latest.version,
                "added": [],
                "removed": [],
                "unchanged_count": 0,
                "has_changes": False,
            }
        parent_codes = list(parent_full.codes)

        live = live_codes_for_batch(parent_full)
        parent_ids = {c.code_id for c in parent_codes}
        live_ids = {c.code_id for c in live}
        added_ids = live_ids - parent_ids
        removed_ids = parent_ids - live_ids
        unchanged = len(parent_ids & live_ids)

        live_by_id = {c.code_id: c for c in live}
        parent_by_id = {c.code_id: c for c in parent_codes}

        def _code_summary(c) -> dict[str, Any]:
            return {
                "code_id": c.code_id,
                "code": c.code,
                "description": c.description,
                "coder_id": c.coder_id,
                "n_quotes": len(c.supporting_quotes or []),
            }

        added: list[dict[str, Any]] = []
        added_ids_sorted = sorted(added_ids)
        if added_ids_sorted:
            with store.session() as s:
                all_edges = list(
                    s.exec(
                        select(CodesDerived)
                        .where(
                            CodesDerived.new_code_id.in_(  # type: ignore[union-attr]
                                added_ids_sorted
                            ),
                            CodesDerived.derivation_type == DERIVATION_REVIEW,
                        )
                        .options(
                            selectinload(CodesDerived.source_code)  # type: ignore[arg-type]
                            .selectinload(Code.segment)
                            .selectinload(Segment.document),
                        )
                    ).all()
                )
                s.expunge_all()
            edges_by_cid: dict[int, list[CodesDerived]] = {}
            for e in all_edges:
                edges_by_cid.setdefault(e.new_code_id, []).append(e)
        else:
            edges_by_cid = {}
        for cid in added_ids_sorted:
            c = live_by_id[cid]
            payload = _code_summary(c)
            edges = edges_by_cid.get(cid, [])
            sources: list[dict[str, Any]] = []
            for e in edges:
                src = e.source_code
                src_seg = (
                    src.segment
                    if src is not None and src.segment_id
                    else None
                )
                src_doc = src_seg.document if src_seg is not None else None
                sources.append(
                    {
                        "code_id": e.source_code_id,
                        "code": src.code if src else None,
                        "coder_id": src.coder_id if src else None,
                        "decision": e.decision,
                        "rationale": e.rationale,
                        "segment_id": src.segment_id if src else None,
                        "document_id": (
                            src_seg.document_id if src_seg else None
                        ),
                        "document_filename": (
                            src_doc.filename if src_doc else None
                        ),
                    }
                )
            decisions = {e.decision for e in edges}
            if decisions & {"M", "U"}:
                payload["change"] = "merge"
            elif "A" in decisions:
                payload["change"] = "new"
            else:
                payload["change"] = "added"
            payload["sources"] = sources
            added.append(payload)

        removed: list[dict[str, Any]] = []
        for cid in sorted(removed_ids):
            c = parent_by_id.get(cid)
            if c is None:
                with store.session() as s:
                    c = s.get(store.Code, cid)
                    if c is not None:
                        s.expunge(c)
            if c is not None:
                removed.append(_code_summary(c))

        return {
            "parent_version": latest.version,
            "added": added,
            "removed": removed,
            "unchanged_count": unchanged,
            "has_changes": bool(added or removed),
        }

    @app.get("/api/recent-codes")
    def get_recent_codes(
        limit: int = Query(default=20, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """Last N real (non-sentinel) Code rows, newest first. Each entry
        carries enough metadata to render a row and link to /code/:id for
        full lineage."""
        _ensure_connected()
        from sqlalchemy.orm import selectinload
        from sqlmodel import select
        from thematic_analysis_inc.db.models import (
            Code,
            Segment,
            SENTINEL_CODE_LABEL,
        )

        out: list[dict[str, Any]] = []
        with store.session() as s:
            rows = list(
                s.exec(
                    select(Code)
                    .where(Code.code != SENTINEL_CODE_LABEL)
                    .order_by(Code.code_id.desc())  # type: ignore[union-attr]
                    .limit(limit)
                    .options(
                        selectinload(Code.coder),  # type: ignore[arg-type]
                        selectinload(Code.segment).selectinload(  # type: ignore[arg-type]
                            Segment.document
                        ),
                    )
                ).all()
            )
            for c in rows:
                coder = c.coder
                seg = c.segment if c.segment_id else None
                doc = seg.document if seg is not None else None
                if c.coder_id == 0:
                    kind = "aggregation"
                elif c.coder_id == -1:
                    kind = "review"
                else:
                    kind = "coder"
                out.append(
                    {
                        "code_id": c.code_id,
                        "code": c.code,
                        "description": c.description,
                        "coder_id": c.coder_id,
                        "coder_identity": coder.identity if coder else None,
                        "kind": kind,
                        "codebook_used_id": c.codebook_used_id,
                        "segment_id": c.segment_id,
                        "document_id": seg.document_id if seg else None,
                        "document_filename": doc.filename if doc else None,
                    }
                )
        return out

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
        rc = store.add_research_context_and_codebook_revision(ctx)
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
        new_rc = store.add_research_context_and_codebook_revision(new_ctx)
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
        from sqlmodel import select

        docs = store.list_documents()
        coders = store.list_coders()
        coder_ids = [c.coder_id for c in coders]

        with store.session() as s:
            queue_rows = list(
                s.exec(
                    select(
                        store.Segment.document_id,
                        store.CodingQueueEntry.coder_id,
                        store.CodingQueueEntry.codebook_used_id,
                        store.CodingQueueEntry.claimed_at,
                        store.CodingQueueEntry.finished_at,
                        store.CodingQueueEntry.error,
                    ).join(
                        store.Segment,
                        store.Segment.segment_id  # type: ignore[arg-type]
                        == store.CodingQueueEntry.segment_id,
                    )
                ).all()
            )
            agg_rows = list(
                s.exec(
                    select(
                        store.Segment.document_id,
                        store.Code.segment_id,
                    )
                    .join(
                        store.Segment,
                        store.Segment.segment_id  # type: ignore[arg-type]
                        == store.Code.segment_id,
                    )
                    .where(store.Code.coder_id == 0)
                    .distinct()
                ).all()
            )

        def _derive(claimed_at, finished_at, error) -> str:
            if error is not None:
                return "failed"
            if finished_at is not None:
                return "done"
            if claimed_at is not None:
                return "running"
            return "pending"

        # (document_id, coder_id) -> {status: count}
        per_doc_coder: dict[tuple[int, int], dict[str, int]] = {}
        # document_id -> max codebook_used_id over its queue entries
        max_cb_per_doc: dict[int, int] = {}
        for doc_id, coder_id, cb_used, claimed_at, finished_at, error in queue_rows:
            key = (doc_id, coder_id)
            buckets = per_doc_coder.setdefault(key, {})
            st = _derive(claimed_at, finished_at, error)
            buckets[st] = buckets.get(st, 0) + 1
            prev = max_cb_per_doc.get(doc_id)
            if prev is None or cb_used > prev:
                max_cb_per_doc[doc_id] = cb_used

        latest = store.latest_codebook()
        latest_cb_version = latest.version if latest is not None else None

        # document_id -> count of distinct segments with an aggregator code
        agg_done_per_doc: dict[int, int] = {}
        for doc_id, _seg_id in agg_rows:
            agg_done_per_doc[doc_id] = agg_done_per_doc.get(doc_id, 0) + 1

        items = []
        for d in docs:
            size_bytes = sum(len(seg.content) for seg in d.segments)
            per_coder = []
            for cid in coder_ids:
                b = per_doc_coder.get((d.document_id, cid), {})
                per_coder.append(
                    {
                        "coder_id": cid,
                        "runs_done": b.get("done", 0),
                        "runs_running": b.get("running", 0),
                        "runs_failed": b.get("failed", 0),
                    }
                )
            agg_done = agg_done_per_doc.get(d.document_id, 0)
            items.append(
                {
                    "document_id": d.document_id,
                    "filename": d.filename,
                    "created_at": (
                        d.created_at.isoformat() if d.created_at else None
                    ),
                    "segments_total": len(d.segments),
                    "size_bytes": size_bytes,
                    "per_coder": per_coder,
                    "aggregations_by_status": (
                        {"done": agg_done} if agg_done else {}
                    ),
                    "queue_state": _queue_state(
                        max_cb_per_doc.get(d.document_id), latest_cb_version
                    ),
                }
            )
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

    @app.post("/api/documents/{document_id}/enqueue")
    def enqueue_document(
        document_id: int, body: EnqueueIn | None = None
    ) -> dict[str, Any]:
        """Schedule every segment of a document for coding (default: all
        registered real coders) at the latest codebook + research-context
        revisions. Re-enqueueing at the same revisions is a no-op."""
        _ensure_connected()
        with store.session() as s:
            doc = s.get(store.Document, document_id)
            if doc is None:
                raise HTTPException(
                    status_code=404, detail="document not found"
                )
        coder_ids = body.coder_ids if body else None
        n = db_coding.enqueue_document(document_id, coder_ids=coder_ids)
        return {"enqueued": n}

    @app.post("/api/segments/{segment_id}/enqueue")
    def enqueue_segment(
        segment_id: int, body: EnqueueIn | None = None
    ) -> dict[str, Any]:
        """Schedule a single segment for coding (default: all registered
        real coders) at the latest codebook + research-context revisions."""
        _ensure_connected()
        seg = store.get_segment(segment_id)
        if seg is None:
            raise HTTPException(status_code=404, detail="segment not found")
        coder_ids = body.coder_ids if body else None
        n = db_coding.enqueue_segment(segment_id, coder_ids=coder_ids)
        return {"enqueued": n}

    @app.post("/api/documents/enqueue-random")
    def enqueue_random_documents(
        body: EnqueueIn | None = None, n: int = 10
    ) -> dict[str, Any]:
        """Pick up to ``n`` documents that have no coding-queue entries yet
        and enqueue every segment in each for all registered real coders.
        Returns the chosen document ids and the number of queue rows inserted."""
        import random
        from sqlmodel import select

        _ensure_connected()
        with store.session() as s:
            enqueued_doc_ids = set(
                s.exec(
                    select(store.Segment.document_id)
                    .join(
                        store.CodingQueueEntry,
                        store.CodingQueueEntry.segment_id  # type: ignore[arg-type]
                        == store.Segment.segment_id,
                    )
                    .distinct()
                ).all()
            )
            stmt = select(store.Document.document_id)
            if enqueued_doc_ids:
                stmt = stmt.where(
                    store.Document.document_id.not_in(enqueued_doc_ids)  # type: ignore[union-attr]
                )
            candidate_ids = list(s.exec(stmt).all())
        random.shuffle(candidate_ids)
        chosen = candidate_ids[:n]
        coder_ids = body.coder_ids if body else None
        total = 0
        for did in chosen:
            total += db_coding.enqueue_document(did, coder_ids=coder_ids)
        return {"document_ids": chosen, "enqueued": total}

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
        if entries:
            from sqlalchemy import func, tuple_
            from sqlmodel import select

            keys = [(q.segment_id, q.coder_id) for q in entries]
            with store.session() as s:
                count_rows = s.exec(
                    select(
                        store.Code.segment_id,
                        store.Code.coder_id,
                        func.count(store.Code.code_id),
                    )
                    .where(
                        tuple_(store.Code.segment_id, store.Code.coder_id).in_(
                            keys
                        )
                    )
                    .group_by(store.Code.segment_id, store.Code.coder_id)
                ).all()
            counts: dict[tuple[int, int], int] = {
                (seg_id, cid): int(n) for seg_id, cid, n in count_rows
            }
        else:
            counts = {}
        for q in entries:
            n_codes = counts.get((q.segment_id, q.coder_id), 0)
            items.append(
                {
                    "segment_id": q.segment_id,
                    "coder_id": q.coder_id,
                    "codebook_version": q.codebook_used_id,
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

    # Legacy aliases the bundled SPA references. The queue's primary key is
    # composite (segment_id, coder_id, codebook_used_id), so we encode
    # it into a single string `id` of the form "{segment_id}_{coder_id}".
    @app.get("/api/coder-runs")
    def list_coder_runs(
        coder_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=100, le=1000),
        offset: int = 0,
    ) -> dict[str, Any]:
        cid: int | None = None
        if coder_id:
            try:
                cid = int(coder_id)
            except ValueError:
                c = store.get_coder_by_external_id(coder_id) if hasattr(
                    store, "get_coder_by_external_id"
                ) else None
                cid = c.coder_id if c is not None else None
                if cid is None:
                    return {"total": 0, "items": []}
        payload = list_coding_queue(coder_id=cid, limit=limit, offset=offset)
        items = []
        for q in payload["items"]:
            if status and q["status"] != status:
                continue
            items.append({"id": f"{q['segment_id']}_{q['coder_id']}", **q})
        return {"total": payload["total"], "items": items}

    @app.delete("/api/coder-runs/{run_id}")
    def delete_coder_run(run_id: str) -> dict[str, Any]:
        try:
            seg_str, coder_str = run_id.split("_", 1)
            seg_id = int(seg_str)
            cid = int(coder_str)
        except ValueError:
            raise HTTPException(status_code=404, detail="run not found")
        return reset_coding_assignment(seg_id, cid)

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
        for d in rows:
            src = d.source_code
            new = d.new_code
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
        from sqlalchemy import func
        from sqlmodel import select
        from thematic_analysis_inc.db.models import CodebookCode

        with store.session() as s:
            counts = dict(
                s.exec(
                    select(
                        CodebookCode.codebook_version,
                        func.count(CodebookCode.code_id),
                    ).group_by(CodebookCode.codebook_version)
                ).all()
            )
        return [
            {
                "version": cv.version,
                "parent_version": cv.parent_version,
                "research_context_version": cv.research_context_version,
                "created_at": (
                    cv.created_at.isoformat() if cv.created_at else None
                ),
                "n_codes": int(counts.get(cv.version, 0)),
            }
            for cv in store.list_codebooks()
        ]

    @app.get("/api/codebook/versions/{version}")
    def get_codebook_version(version: int) -> dict[str, Any]:
        _ensure_connected()
        from sqlalchemy.orm import selectinload
        from sqlmodel import select
        from thematic_analysis_inc.db.coders import SYSTEM_REVIEWER_ID
        from thematic_analysis_inc.db.models import (
            Code,
            CodebookCode,
            CodesDerived,
            DERIVATION_REVIEW,
            Quote,
            Segment,
        )

        with store.session() as s:
            cv = s.get(store.Codebook, version)
            if cv is None:
                raise HTTPException(
                    status_code=404, detail="version not found"
                )
            codes = list(
                s.exec(
                    select(Code)
                    .join(CodebookCode, CodebookCode.code_id == Code.code_id)
                    .where(CodebookCode.codebook_version == version)
                    .order_by(Code.code)
                    .options(
                        selectinload(Code.supporting_quotes)  # type: ignore[arg-type]
                        .selectinload(Quote.segment)
                        .selectinload(Segment.document),
                        selectinload(Code.derivation_sources).selectinload(  # type: ignore[arg-type]
                            CodesDerived.source_code
                        ),
                    )
                ).all()
            )

            def _change_tags(c: Code) -> list[str]:
                # Codes carried forward from before the parent revision
                # weren't touched in this update -> no tags.
                if (
                    cv.parent_version is not None
                    and c.codebook_used_id < cv.parent_version
                ):
                    return []
                review_edges = [
                    e
                    for e in c.derivation_sources
                    if e.derivation_type == DERIVATION_REVIEW
                ]
                if not review_edges:
                    return []
                decisions = {e.decision for e in review_edges}
                tags: list[str] = []
                if "A" in decisions:
                    tags.append("new")
                if decisions & {"M", "U"}:
                    prev = next(
                        (
                            e.source_code
                            for e in review_edges
                            if e.source_code is not None
                            and e.source_code.coder_id == SYSTEM_REVIEWER_ID
                        ),
                        None,
                    )
                    if prev is not None and prev.code != c.code:
                        tags.append("renamed")
                    tags.append("new_quotes")
                return tags

            codes_payload = []
            for c in codes:
                quotes = []
                for q in sorted(
                    c.supporting_quotes, key=lambda x: x.quote_id
                ):
                    qseg = q.segment
                    qdoc = qseg.document if qseg is not None else None
                    quotes.append(
                        {
                            "quote_id": q.quote_id,
                            "text": q.text,
                            "segment_id": q.segment_id,
                            "document_id": (
                                qseg.document_id if qseg else None
                            ),
                            "document_filename": (
                                qdoc.filename if qdoc else None
                            ),
                        }
                    )
                codes_payload.append(
                    {
                        "code_id": c.code_id,
                        "code": c.code,
                        "description": c.description,
                        "rationale": c.rationale,
                        "coder_id": c.coder_id,
                        "quotes": quotes,
                        "change_tags": _change_tags(c),
                    }
                )
            return {
                "version": cv.version,
                "parent_version": cv.parent_version,
                "research_context_version": cv.research_context_version,
                "created_at": (
                    cv.created_at.isoformat() if cv.created_at else None
                ),
                "n_codes": len(codes_payload),
                "codes": codes_payload,
            }

    @app.get("/api/codes/{code_id}/similar")
    def get_similar_codes(
        code_id: int,
        top_k: int = Query(default=30, ge=1, le=200),
    ) -> dict[str, Any]:
        """Top-k reviewer codes most similar to ``code_id`` by cosine
        similarity over the embeddings the reviewer agent uses. Restricted
        to codes that share at least one codebook revision with the
        current code (so they're candidates for a merge in the same
        codebook context)."""
        _ensure_connected()
        from sqlalchemy.orm import selectinload
        from sqlmodel import select
        from thematic_analysis_inc.db import embeddings as db_embeddings
        from thematic_analysis_inc.db.models import (
            Code,
            CodebookCode,
        )

        with store.session() as s:
            c = s.get(Code, code_id)
            if c is None:
                raise HTTPException(status_code=404, detail="code not found")
            if c.embedding is None:
                raise HTTPException(
                    status_code=400,
                    detail="code has no embedding (only reviewer codes have one)",
                )
            in_versions = [
                int(v)
                for v in s.exec(
                    select(CodebookCode.codebook_version).where(
                        CodebookCode.code_id == code_id
                    )
                ).all()
            ]
            if not in_versions:
                return {"codebook_version": None, "items": []}
            codebook_version = max(in_versions)
            peers = list(
                s.exec(
                    select(Code)
                    .join(CodebookCode, CodebookCode.code_id == Code.code_id)
                    .where(
                        CodebookCode.codebook_version == codebook_version,
                        CodebookCode.code_id != code_id,
                    )
                    .options(
                        selectinload(Code.segment),  # type: ignore[arg-type]
                        selectinload(Code.supporting_quotes),  # type: ignore[arg-type]
                    )
                ).all()
            )
            peers = [p for p in peers if p.embedding is not None]
            query_emb = db_embeddings.decode(c.embedding)
            similar = db_embeddings.find_similar(query_emb, peers, top_k=top_k)
            items: list[dict[str, Any]] = []
            for entry, score in similar:
                seg = entry.segment if entry.segment_id else None
                items.append(
                    {
                        "code_id": entry.code_id,
                        "code": entry.code,
                        "description": entry.description,
                        "coder_id": entry.coder_id,
                        "similarity": round(score, 4),
                        "segment_id": entry.segment_id,
                        "document_id": seg.document_id if seg else None,
                        "n_quotes": len(entry.supporting_quotes or []),
                    }
                )
            return {
                "codebook_version": codebook_version,
                "items": items,
            }

    class MergeIn(BaseModel):
        selected_code_ids: list[int]
        codebook_version: int | None = None

    @app.post("/api/codes/{code_id}/merge")
    def merge_codes(code_id: int, body: MergeIn) -> dict[str, Any]:
        """Manually merge ``code_id`` with ``selected_code_ids``: create one
        new reviewer Code (copy of the current label/embedding) with one
        'R' edge per merged source, and a new codebook revision whose
        membership replaces the merged codes with the new one."""
        _ensure_connected()
        from sqlmodel import select
        from thematic_analysis_inc.db.models import Code, CodebookCode

        if not body.selected_code_ids:
            raise HTTPException(
                status_code=400, detail="selected_code_ids must be non-empty"
            )
        with store.session() as s:
            c = s.get(Code, code_id)
            if c is None:
                raise HTTPException(status_code=404, detail="code not found")
            if body.codebook_version is not None:
                codebook_version = body.codebook_version
            else:
                in_versions = [
                    int(v)
                    for v in s.exec(
                        select(CodebookCode.codebook_version).where(
                            CodebookCode.code_id == code_id
                        )
                    ).all()
                ]
                if not in_versions:
                    raise HTTPException(
                        status_code=400,
                        detail="code is not a member of any codebook revision",
                    )
                codebook_version = max(in_versions)

        try:
            new_code_id, new_version = db_review.manual_merge_reviewer_codes(
                current_code_id=code_id,
                selected_code_ids=body.selected_code_ids,
                codebook_version=codebook_version,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "new_code_id": new_code_id,
            "new_codebook_version": new_version,
        }

    @app.get("/api/quotes/{quote_id}")
    def get_quote(quote_id: int) -> dict[str, Any]:
        _ensure_connected()
        from thematic_analysis_inc.db.models import Document, Quote, Segment

        with store.session() as s:
            q = s.get(Quote, quote_id)
            if q is None:
                raise HTTPException(status_code=404, detail="quote not found")
            seg = s.get(Segment, q.segment_id)
            doc = s.get(Document, seg.document_id) if seg else None
            return {
                "quote_id": q.quote_id,
                "text": q.text,
                "segment_id": q.segment_id,
                "document_id": seg.document_id if seg else None,
                "document_filename": doc.filename if doc else None,
            }

    @app.get("/api/codes/{code_id}")
    def get_code(code_id: int) -> dict[str, Any]:
        """Full details for one Code with one level of derivation sources.

        The tree-walk on the client is one HTTP call per node: each
        source row carries enough to render, and following its
        ``code_id`` to ``/api/codes/{id}`` returns its own sources.
        """
        _ensure_connected()
        from sqlalchemy import func as _func
        from sqlalchemy.orm import selectinload
        from sqlmodel import select
        from thematic_analysis_inc.db.models import (
            Code,
            CodebookCode,
            CodesDerived,
            Quote,
            Segment,
        )

        with store.session() as s:
            c = s.exec(
                select(Code)
                .where(Code.code_id == code_id)
                .options(
                    selectinload(Code.coder),  # type: ignore[arg-type]
                    selectinload(Code.segment).selectinload(  # type: ignore[arg-type]
                        Segment.document
                    ),
                    selectinload(Code.supporting_quotes)  # type: ignore[arg-type]
                    .selectinload(Quote.segment)
                    .selectinload(Segment.document),
                    selectinload(Code.derivation_sources)  # type: ignore[arg-type]
                    .selectinload(CodesDerived.source_code)
                    .options(
                        selectinload(Code.coder),
                        selectinload(Code.segment).selectinload(
                            Segment.document
                        ),
                    ),
                )
            ).first()
            if c is None:
                raise HTTPException(status_code=404, detail="code not found")
            seg = c.segment if c.segment_id else None
            seg_doc = seg.document if seg is not None else None
            coder = c.coder

            quotes = []
            for q in sorted(c.supporting_quotes, key=lambda x: x.quote_id):
                qseg = q.segment
                qdoc = qseg.document if qseg is not None else None
                quotes.append(
                    {
                        "quote_id": q.quote_id,
                        "text": q.text,
                        "segment_id": q.segment_id,
                        "document_id": qseg.document_id if qseg else None,
                        "document_filename": qdoc.filename if qdoc else None,
                    }
                )

            edges = list(c.derivation_sources)
            # Batch "has_more_sources": one grouped query for every source
            # code id, instead of N count queries inside the loop.
            src_ids = [e.source_code_id for e in edges]
            has_more_map: dict[int, bool] = {}
            if src_ids:
                hm_rows = s.exec(
                    select(
                        CodesDerived.new_code_id,
                        _func.count(CodesDerived.source_code_id),
                    )
                    .where(CodesDerived.new_code_id.in_(src_ids))  # type: ignore[union-attr]
                    .group_by(CodesDerived.new_code_id)
                ).all()
                has_more_map = {int(nid): int(n) > 0 for nid, n in hm_rows}
            sources: list[dict[str, Any]] = []
            for e in edges:
                src = e.source_code
                src_coder = src.coder if src is not None else None
                src_seg = (
                    src.segment
                    if src is not None and src.segment_id
                    else None
                )
                src_doc = src_seg.document if src_seg is not None else None
                sources.append(
                    {
                        "code_id": e.source_code_id,
                        "derivation_type": e.derivation_type,
                        "decision": e.decision,
                        "rationale": e.rationale,
                        "code": src.code if src else None,
                        "description": src.description if src else None,
                        "coder_id": src.coder_id if src else None,
                        "coder_identity": (
                            src_coder.identity if src_coder else None
                        ),
                        "codebook_used_id": (
                            src.codebook_used_id if src else None
                        ),
                        "segment_id": src.segment_id if src else None,
                        "document_id": (
                            src_seg.document_id if src_seg else None
                        ),
                        "document_filename": (
                            src_doc.filename if src_doc else None
                        ),
                        "has_more_sources": has_more_map.get(
                            e.source_code_id, False
                        ),
                    }
                )

            in_codebooks = [
                int(v)
                for v in s.exec(
                    select(CodebookCode.codebook_version).where(
                        CodebookCode.code_id == code_id
                    )
                ).all()
            ]
            return {
                "code_id": c.code_id,
                "code": c.code,
                "description": c.description,
                "rationale": c.rationale,
                "coder_id": c.coder_id,
                "coder_identity": coder.identity if coder else None,
                "codebook_used_id": c.codebook_used_id,
                "segment_id": c.segment_id,
                "segment": (
                    {
                        "segment_id": seg.segment_id,
                        "title": seg.title,
                        "document_id": seg.document_id,
                        "document_filename": (
                            seg_doc.filename if seg_doc else None
                        ),
                        "line_from": seg.line_from,
                        "line_to": seg.line_to,
                        "preview": seg.content[:240],
                    }
                    if seg is not None
                    else None
                ),
                "quotes": quotes,
                "derivation_sources": sources,
                "in_codebook_versions": in_codebooks,
            }

    # ── themes ───────────────────────────────────────────────────────────

    def _theme_quote_payload(q) -> dict[str, Any]:
        seg = q.segment
        doc = seg.document if seg is not None else None
        return {
            "quote_id": q.quote_id,
            "text": q.text,
            "segment_id": q.segment_id,
            "document_id": seg.document_id if seg else None,
            "document_filename": doc.filename if doc else None,
        }

    def _theme_code_payload(c) -> dict[str, Any]:
        return {
            "code_id": c.code_id,
            "code": c.code,
            "description": c.description,
            "coder_id": c.coder_id,
            "n_quotes": len(c.supporting_quotes or []),
        }

    def _theme_summary(t) -> dict[str, Any]:
        return {
            "theme_id": t.theme_id,
            "title": t.title,
            "description": t.description,
            "rationale": t.rationale,
            "source": t.source,
            "theme_coding_job_id": t.theme_coding_job_id,
            "codebook_used_id": t.codebook_used_id,
            "deleted": t.deleted,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "n_codes": len(t.codes or []),
            "n_quotes": len(t.supporting_quotes or []),
        }

    def _theme_full_payload(t) -> dict[str, Any]:
        return {
            **_theme_summary(t),
            "codes": [_theme_code_payload(c) for c in (t.codes or [])],
            "quotes": [
                _theme_quote_payload(q) for q in (t.supporting_quotes or [])
            ],
        }

    def _job_run_status(
        all_themes: list, real_themes: list
    ) -> str:
        """Four-state job status used by the UI:
        - "not_run":   no Theme rows at all for this job
        - "running":   a running-sentinel row is present (worker in flight)
        - "no_themes": ran, but only the empty sentinel — agent returned nothing
        - "has_themes": ran and produced at least one real theme
        """
        from thematic_analysis_inc.db.models import is_running_sentinel_theme

        if not all_themes:
            return "not_run"
        if real_themes:
            return "has_themes"
        if any(is_running_sentinel_theme(t) for t in all_themes):
            return "running"
        return "no_themes"

    @app.get("/api/theme-coding-jobs")
    def list_theme_coding_jobs() -> list[dict[str, Any]]:
        _ensure_connected()
        from thematic_analysis_inc.db.models import is_sentinel_theme
        from thematic_analysis_inc.db.theme import list_themes_for_job

        out: list[dict[str, Any]] = []
        for job in store.list_theme_coding_jobs():
            all_themes = list_themes_for_job(job.id, include_sentinel=True)
            real_themes = [t for t in all_themes if not is_sentinel_theme(t)]
            n_active = sum(1 for t in real_themes if not t.deleted)
            out.append(
                {
                    "id": job.id,
                    "codebook_used_id": job.codebook_used_id,
                    "prompt": job.prompt,
                    "created_at": (
                        job.created_at.isoformat()
                        if job.created_at
                        else None
                    ),
                    "n_themes": len(real_themes),
                    "n_themes_active": n_active,
                    "run_status": _job_run_status(all_themes, real_themes),
                }
            )
        return out

    @app.post("/api/theme-coding-jobs")
    def create_theme_coding_job(body: ThemeCodingJobIn) -> dict[str, Any]:
        """Create a theme-coding job (codebook revision + researcher
        prompt). The job is NOT run — call ``POST .../{id}/run`` or the
        bulk ``POST .../run-pending`` endpoint to invoke the theme coder."""
        _ensure_connected()

        if not body.prompt.strip():
            raise HTTPException(
                status_code=400, detail="prompt must not be empty"
            )
        cb = store.get_codebook(body.codebook_version)
        if cb is None:
            raise HTTPException(
                status_code=400,
                detail=f"codebook version {body.codebook_version} not found",
            )
        job = store.add_theme_coding_job(
            codebook_used_id=body.codebook_version, prompt=body.prompt
        )
        return {
            "id": job.id,
            "codebook_used_id": job.codebook_used_id,
            "prompt": job.prompt,
            "created_at": (
                job.created_at.isoformat() if job.created_at else None
            ),
            "run_status": "not_run",
        }

    @app.post("/api/theme-coding-jobs/{job_id}/run")
    def run_theme_coding_job_endpoint(job_id: int) -> dict[str, Any]:
        """Synchronously run a single job. Refuses if it has already
        been run (i.e. has any Theme rows). May take a while — the LLM
        call is in-line."""
        _ensure_connected()
        from thematic_analysis_inc import workers
        from thematic_analysis_inc.db.theme import list_themes_for_job

        job = store.get_theme_coding_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        existing = list_themes_for_job(job_id, include_sentinel=True)
        if existing:
            raise HTTPException(
                status_code=409,
                detail="job has already been run",
            )
        try:
            themes = workers.run_theme_coding_job(job)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {
            "id": job.id,
            "n_themes": len(themes),
            "run_status": "has_themes" if themes else "no_themes",
        }

    @app.post("/api/theme-coding-jobs/run-pending")
    def run_pending_theme_coding_jobs_endpoint() -> dict[str, Any]:
        """Run every job that has not yet been run. Sequential."""
        _ensure_connected()
        from thematic_analysis_inc import workers

        try:
            results = workers.run_pending_theme_coding_jobs()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        n_themes = sum(len(themes) for _, themes in results)
        n_empty = sum(1 for _, themes in results if not themes)
        return {
            "n_jobs_run": len(results),
            "n_themes": n_themes,
            "n_jobs_empty": n_empty,
        }

    @app.get("/api/theme-coding-jobs/{job_id}")
    def get_theme_coding_job(job_id: int) -> dict[str, Any]:
        _ensure_connected()
        from thematic_analysis_inc.db.models import is_sentinel_theme
        from thematic_analysis_inc.db.theme import list_themes_for_job

        job = store.get_theme_coding_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        all_themes = list_themes_for_job(job_id, include_sentinel=True)
        real_themes = [t for t in all_themes if not is_sentinel_theme(t)]
        return {
            "id": job.id,
            "codebook_used_id": job.codebook_used_id,
            "prompt": job.prompt,
            "created_at": (
                job.created_at.isoformat() if job.created_at else None
            ),
            "run_status": _job_run_status(all_themes, real_themes),
            "themes": [_theme_full_payload(t) for t in real_themes],
        }

    @app.get("/api/theme-coding-jobs/meta/system-prompt")
    def get_theme_coder_system_prompt() -> dict[str, Any]:
        """Return the static system prompt the theme coder uses and the
        template by which the researcher's framing is appended to the
        user message — for display in the create-job UI."""
        from thematic_analysis.agents.theme_coder import (
            THEME_CODER_SYSTEM_PROMPT,
            _CODEBOOK_HEADER,
            _RESEARCHER_FRAMING_HEADER,
        )

        return {
            "system_prompt": THEME_CODER_SYSTEM_PROMPT,
            "user_framing_template": _RESEARCHER_FRAMING_HEADER,
            "user_codebook_template": _CODEBOOK_HEADER,
        }

    @app.get("/api/themes")
    def list_current_themes() -> list[dict[str, Any]]:
        _ensure_connected()
        return [_theme_full_payload(t) for t in store.list_current_themes()]

    @app.post("/api/themes")
    def create_manual_theme(body: ManualThemeIn) -> dict[str, Any]:
        _ensure_connected()
        from thematic_analysis_inc.db.theme import add_manual_theme

        title = body.title.strip()
        if not title:
            raise HTTPException(
                status_code=400, detail="title must not be empty"
            )
        t = add_manual_theme(
            title=title,
            description=body.description,
            rationale=body.rationale,
        )
        return _theme_summary(t)

    @app.get("/api/themes/{theme_id}")
    def get_theme_detail(theme_id: int) -> dict[str, Any]:
        _ensure_connected()
        from thematic_analysis_inc.db.theme import (
            list_themes_derived_from,
            list_themes_derived_into,
        )

        t = store.get_theme(theme_id)
        if t is None:
            raise HTTPException(status_code=404, detail="theme not found")
        derived_from = list_themes_derived_from(theme_id)
        derived_into = list_themes_derived_into(theme_id)
        return {
            **_theme_full_payload(t),
            "derived_from": [_theme_summary(x) for x in derived_from],
            "derived_into": [_theme_summary(x) for x in derived_into],
        }

    @app.delete("/api/themes/{theme_id}")
    def delete_theme(theme_id: int) -> dict[str, Any]:
        _ensure_connected()
        ok = store.mark_theme_deleted(theme_id, True)
        if not ok:
            raise HTTPException(status_code=404, detail="theme not found")
        return {"deleted": True}

    @app.post("/api/themes/{theme_id}/restore")
    def restore_theme(theme_id: int) -> dict[str, Any]:
        _ensure_connected()
        ok = store.mark_theme_deleted(theme_id, False)
        if not ok:
            raise HTTPException(status_code=404, detail="theme not found")
        return {"deleted": False}

    # ── background runner ───────────────────────────────────────────────
    def _runner_payload() -> dict[str, Any]:
        runner = workers.get_background_runner()
        st = runner.status()
        ok, reason = llm_configured()
        st["llm_configured"] = ok
        st["llm_unavailable_reason"] = reason
        return st

    @app.get("/api/background-runner")
    def get_background_runner_state() -> dict[str, Any]:
        return _runner_payload()

    @app.post("/api/background-runner/start")
    def start_background_runner(body: BackgroundRunnerStartIn) -> dict[str, Any]:
        ok, reason = llm_configured()
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=f"LLM not configured: {reason}",
            )
        _ensure_connected()
        runner = workers.get_background_runner()
        started = runner.start(
            workers=body.workers,
            use_mock_embeddings=body.use_mock_embeddings,
        )
        return {"started": started, **_runner_payload()}

    @app.post("/api/background-runner/stop")
    def stop_background_runner() -> dict[str, Any]:
        runner = workers.get_background_runner()
        stopped = runner.stop()
        return {"stopped": stopped, **_runner_payload()}

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
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable uvicorn auto-reload (dev only).",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help=(
            "also launch the Vite dev server (frontend/ npm run dev) as a "
            "subprocess. Open http://localhost:5173 — it proxies /api here."
        ),
    )

    args = parser.parse_args(argv)

    db_path = Path(args.db).resolve()
    if not db_path.exists():
        print(f"note: {db_path} does not exist; it will be created", flush=True)

    import uvicorn

    app = create_app(db_path)
    print(f"serving {db_path} at http://{args.host}:{args.port}", flush=True)

    if args.dev:
        _start_vite_dev()

    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)
    return 0


def _start_vite_dev() -> None:
    """Spawn `npm run dev` in frontend/ as a child process and ensure it
    is killed when this process exits. The Vite server proxies /api to
    this backend (see frontend/vite.config.ts)."""
    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if not (frontend_dir / "package.json").is_file():
        print(
            f"--dev: no package.json under {frontend_dir}; skipping Vite",
            flush=True,
        )
        return
    try:
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=str(frontend_dir),
            start_new_session=True,
        )
    except FileNotFoundError:
        print("--dev: `npm` not on PATH; skipping Vite", flush=True)
        return

    def _kill() -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    atexit.register(_kill)
    print(
        "dev: vite running in frontend/ — open http://localhost:5173",
        flush=True,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
