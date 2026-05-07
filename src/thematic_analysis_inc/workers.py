"""Worker functions for the incremental Stage 1 pipeline.

Coders are first-class rows (id + identity). `code_one` picks the next
segment that this coder hasn't yet coded, opens a `running` coder_run
row, calls the LLM, then writes 'done' (with codes) or 'failed'.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any, Callable

from thematic_analysis.agents.coder import CoderAgent, CoderConfig
from thematic_analysis.codebook import Codebook

from thematic_analysis_inc import store


# Codebook cache --------------------------------------------------------------

_codebook_cache: dict[int, Codebook] = {}


def clear_codebook_cache() -> None:
    _codebook_cache.clear()


def _get_codebook(
    conn: sqlite3.Connection, version: int, use_mock_embeddings: bool
) -> Codebook:
    cb = _codebook_cache.get(version)
    if cb is not None:
        return cb
    cv = store.get_codebook_version(conn, version)
    if cv is None:
        raise RuntimeError(f"codebook version {version} not found")
    cb = Codebook.from_json(cv.snapshot_json, use_mock_embeddings=use_mock_embeddings)
    _codebook_cache[version] = cb
    return cb


# Coder agent factory ---------------------------------------------------------

AgentFactory = Callable[[Codebook, "store.Coder"], Any]


def default_coder_factory(codebook: Codebook, coder: "store.Coder") -> CoderAgent:
    return CoderAgent(
        config=CoderConfig(identity=coder.identity), codebook=codebook
    )


# Result helpers --------------------------------------------------------------


def _persist(
    conn: sqlite3.Connection,
    run_id: int,
    coder_id: str,
    segment_id: str,
    version: int,
    assignment,
    elapsed: float,
) -> dict:
    store.record_coder_result(
        conn,
        run_id=run_id,
        codes=list(assignment.codes),
        rationales=list(assignment.rationales),
        is_new=list(assignment.is_new_code),
        raw_response=None,
    )
    return {
        "ok": True,
        "run_id": run_id,
        "coder_id": coder_id,
        "segment_id": segment_id,
        "version": version,
        "n_codes": len(assignment.codes),
        "elapsed": elapsed,
    }


def _fail(
    conn: sqlite3.Connection,
    run_id: int,
    coder_id: str,
    segment_id: str,
    version: int,
    exc: BaseException,
) -> dict:
    msg = f"{type(exc).__name__}: {exc}"
    store.record_coder_failure(conn, run_id, msg)
    return {
        "ok": False,
        "run_id": run_id,
        "coder_id": coder_id,
        "segment_id": segment_id,
        "version": version,
        "error": msg,
    }


# Single-task workers ---------------------------------------------------------


def _next_unit(
    conn: sqlite3.Connection, coder: "store.Coder"
) -> tuple[int, str, str, int] | None:
    """Pick one (segment_id, text) the coder hasn't done yet, open a
    'running' coder_run row at the latest codebook version, and return
    (run_id, segment_id, text, version). Skips segments where a row was
    inserted concurrently."""
    latest = store.latest_codebook_version(conn)
    if latest is None:
        raise RuntimeError("no codebook version exists; run init first")
    for row in store.segments_to_code(conn, coder.coder_id):
        run_id = store.start_coder_run(
            conn, row["segment_id"], coder.coder_id, latest.version
        )
        if run_id is not None:
            return run_id, row["segment_id"], row["text"], latest.version
    return None


def code_one(
    conn: sqlite3.Connection,
    coder_id: str,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    """Process one un-coded segment for the given coder. Returns None when
    the coder has nothing left to do."""
    coder = store.get_coder(conn, coder_id)
    if coder is None:
        raise ValueError(f"unknown coder_id: {coder_id}")
    unit = _next_unit(conn, coder)
    if unit is None:
        return None
    run_id, segment_id, text, version = unit
    factory = agent_factory or default_coder_factory
    try:
        codebook = _get_codebook(conn, version, use_mock_embeddings)
        agent = factory(codebook, coder)
        t0 = time.monotonic()
        assignment = agent.code_segment(segment_id, text)
        return _persist(
            conn, run_id, coder_id, segment_id, version, assignment,
            time.monotonic() - t0,
        )
    except Exception as exc:
        return _fail(conn, run_id, coder_id, segment_id, version, exc)


async def code_one_async(
    conn: sqlite3.Connection,
    coder_id: str,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    coder = store.get_coder(conn, coder_id)
    if coder is None:
        raise ValueError(f"unknown coder_id: {coder_id}")
    unit = _next_unit(conn, coder)
    if unit is None:
        return None
    run_id, segment_id, text, version = unit
    factory = agent_factory or default_coder_factory
    try:
        codebook = _get_codebook(conn, version, use_mock_embeddings)
        agent = factory(codebook, coder)
        t0 = time.monotonic()
        if hasattr(agent, "code_segment_async"):
            assignment = await agent.code_segment_async(segment_id, text)
        else:
            assignment = agent.code_segment(segment_id, text)
        return _persist(
            conn, run_id, coder_id, segment_id, version, assignment,
            time.monotonic() - t0,
        )
    except Exception as exc:
        return _fail(conn, run_id, coder_id, segment_id, version, exc)


async def drain_code_async(
    conn: sqlite3.Connection,
    coder_id: str,
    *,
    workers: int = 1,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    counters = {"done": 0, "failed": 0}
    stop = False

    async def loop() -> None:
        nonlocal stop
        while True:
            if stop:
                return
            if limit is not None and counters["done"] + counters["failed"] >= limit:
                stop = True
                return
            res = await code_one_async(
                conn,
                coder_id,
                use_mock_embeddings=use_mock_embeddings,
                agent_factory=agent_factory,
            )
            if res is None:
                stop = True
                return
            if res["ok"]:
                counters["done"] += 1
            else:
                counters["failed"] += 1
            if on_event is not None:
                on_event(res, counters)

    await asyncio.gather(*[loop() for _ in range(max(1, workers))])
    return counters
