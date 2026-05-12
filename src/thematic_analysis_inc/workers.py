"""Worker functions for the incremental Stage 1 and Stage 2 pipelines.

Coders are first-class rows (id + identity). `code_one` picks the next
segment that this coder hasn't yet coded, opens a `running` coder_run
row, calls the LLM, then writes 'done' (with codes) or 'failed'.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from typing import Any, Callable

from thematic_analysis.agents.aggregator import (
    AggregatorConfig,
    CodeAggregatorAgent,
)
from thematic_analysis.agents.coder import CodeAssignment, CoderAgent, CoderConfig
from thematic_analysis.agents.reviewer import ReviewDecision, ReviewerAgent, ReviewerConfig
from thematic_analysis.agents.theme_aggregator import (
    ThemeAggregatorAgent,
    ThemeAggregatorConfig,
    ThemeAggregationResult,
)
from thematic_analysis.agents.theme_coder import (
    Theme,
    ThemeCoderAgent,
    ThemeCoderConfig,
    ThemeResult,
)
from thematic_analysis.codebook import Codebook
from thematic_analysis.codebook.codebook import Quote

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


# Aggregator ------------------------------------------------------------------

AggregatorFactory = Callable[[Codebook], Any]


def default_aggregator_factory(codebook: Codebook) -> CodeAggregatorAgent:
    return CodeAggregatorAgent(config=AggregatorConfig(), codebook=codebook)


def _build_assignments(
    segment_id: str, text: str, runs: list[store.CoderRunResult]
) -> list[CodeAssignment]:
    return [
        CodeAssignment(
            segment_id=segment_id,
            segment_text=text,
            codes=list(r.codes),
            rationales=list(r.rationales),
            is_new_code=list(r.is_new),
        )
        for r in runs
    ]


def _source_coders_for(
    original_codes: list[str], code_to_coders: dict[str, list[str]]
) -> list[str]:
    seen: list[str] = []
    for c in original_codes:
        for coder_id in code_to_coders.get(c, ()):
            if coder_id not in seen:
                seen.append(coder_id)
    return seen


def _build_rows(
    result, code_to_coders: dict[str, list[str]]
) -> list[store.AggregatedCodeRow]:
    rows: list[store.AggregatedCodeRow] = []
    for mc in result.all_codes():
        quotes = [{"quote_id": q.quote_id, "text": q.text} for q in mc.quotes]
        sources = _source_coders_for(mc.original_codes, code_to_coders)
        rows.append(
            store.AggregatedCodeRow(
                code=mc.code,
                quotes_json=json.dumps(quotes),
                source_coders_json=json.dumps(sources),
            )
        )
    return rows


def aggregate_one(
    conn: sqlite3.Connection,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AggregatorFactory | None = None,
) -> dict | None:
    """Aggregate codes for one ready segment. Returns None when nothing is
    ready to aggregate."""
    row = store.next_segment_to_aggregate(conn)
    if row is None:
        return None
    segment_id = row["segment_id"]
    text = row["text"]

    agg_id = store.start_aggregation(conn, segment_id)
    if agg_id is None:
        # Race: someone else already started this segment. Treat as "no work
        # claimed this round" but signal we should keep looping.
        return {
            "ok": False,
            "segment_id": segment_id,
            "error": "aggregation row already exists (race)",
            "skipped": True,
        }

    runs = store.load_segment_coder_results(conn, segment_id)
    code_to_coders: dict[str, list[str]] = {}
    for r in runs:
        for code in r.codes:
            code_to_coders.setdefault(code, []).append(r.coder_id)

    n_in = sum(len(r.codes) for r in runs)
    try:
        codebook = Codebook(use_mock_embeddings=use_mock_embeddings)
        factory = agent_factory or default_aggregator_factory
        agent = factory(codebook)
        t0 = time.monotonic()
        assignments = _build_assignments(segment_id, text, runs)
        result = agent.aggregate(assignments)
        rows = _build_rows(result, code_to_coders)
        store.record_aggregation_result(
            conn,
            aggregation_id=agg_id,
            segment_id=segment_id,
            rows=rows,
        )
        return {
            "ok": True,
            "aggregation_id": agg_id,
            "segment_id": segment_id,
            "n_in": n_in,
            "n_merged": len(result.merged_codes),
            "n_retained": len(result.retained_codes),
            "elapsed": time.monotonic() - t0,
        }
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        store.record_aggregation_failure(conn, agg_id, msg)
        return {
            "ok": False,
            "aggregation_id": agg_id,
            "segment_id": segment_id,
            "error": msg,
        }


def drain_aggregate(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: AggregatorFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    """Drain the aggregation queue serially. Stops when there is nothing
    left or the optional limit is reached."""
    counters = {"done": 0, "failed": 0}
    while True:
        if limit is not None and counters["done"] + counters["failed"] >= limit:
            break
        res = aggregate_one(
            conn,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
        if res is None:
            break
        if res.get("skipped"):
            # Another worker grabbed this segment; loop to find another.
            continue
        if res["ok"]:
            counters["done"] += 1
        else:
            counters["failed"] += 1
        if on_event is not None:
            on_event(res, counters)
    return counters


# Reviewer --------------------------------------------------------------------

ReviewerFactory = Callable[[Codebook], Any]


def default_reviewer_factory(codebook: Codebook) -> ReviewerAgent:
    return ReviewerAgent(config=ReviewerConfig(), codebook=codebook)


def review_one(
    conn: sqlite3.Connection,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
) -> dict | None:
    """Review one un-reviewed aggregated_code. Opens an IMMEDIATE transaction
    (single-writer). Returns None when nothing is ready to review."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = store.next_aggregated_code_to_review(conn)
        if row is None:
            conn.execute("ROLLBACK")
            return None

        agg_code_id: int = row["id"]
        code: str = row["code"]
        segment_id: str = row["segment_id"]
        quotes = [
            Quote(quote_id=q["quote_id"], text=q["text"])
            for q in json.loads(row["quotes_json"])
        ]

        latest = store.latest_codebook_version(conn)
        if latest is None:
            conn.execute("ROLLBACK")
            raise RuntimeError("no codebook version exists; run init first")

        codebook = _get_codebook(conn, latest.version, use_mock_embeddings)

        factory = agent_factory or default_reviewer_factory
        agent = factory(codebook)

        t0 = time.monotonic()
        result = agent.review_code(code, quotes)
        agent.apply_review(result)

        # apply_review modifies codebook in-place; evict the cache entry so
        # the next call loads the freshly-serialized version instead.
        _codebook_cache.pop(latest.version, None)

        new_version: int | None = None
        if result.decision != ReviewDecision.SKIP:
            new_version = store.insert_codebook_version(
                conn,
                codebook.to_json(),
                parent=latest.version,
                created_by="reviewer",
            )

        store.record_review_decision(
            conn,
            aggregated_code_id=agg_code_id,
            decision=result.decision.value,
            target_code=result.target_code,
            rationale=result.rationale,
            applied=1,
            resulting_version=new_version,
        )

        # If no un-reviewed codes remain for this segment, mark it done.
        n_remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM aggregated_codes ac "
            "WHERE ac.aggregation_id IN ("
            "  SELECT id FROM aggregations WHERE segment_id = ?"
            ") "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM review_decisions rd "
            "  WHERE rd.aggregated_code_id = ac.id"
            ")",
            (segment_id,),
        ).fetchone()["n"]

        if n_remaining == 0:
            conn.execute(
                "UPDATE segments SET status='done' WHERE segment_id = ?",
                (segment_id,),
            )

        conn.execute("COMMIT")
        return {
            "ok": True,
            "aggregated_code_id": agg_code_id,
            "segment_id": segment_id,
            "code": code,
            "decision": result.decision.value,
            "target_code": result.target_code,
            "new_version": new_version,
            "elapsed": time.monotonic() - t0,
        }
    except Exception:
        conn.execute("ROLLBACK")
        raise


def drain_review(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    """Drain the review queue serially. Stops when there is nothing left or
    the optional limit is reached."""
    counters = {"done": 0, "failed": 0}
    while True:
        if limit is not None and counters["done"] + counters["failed"] >= limit:
            break
        res = review_one(
            conn,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
        if res is None:
            break
        counters["done"] += 1
        if on_event is not None:
            on_event(res, counters)
    return counters


# ── Stage 2 helpers ───────────────────────────────────────────────────────────


def _theme_result_from_json(json_str: str) -> ThemeResult:
    """Reconstruct a ThemeResult from its stored JSON."""
    data = json.loads(json_str)
    themes = [
        Theme(
            name=t["name"],
            description=t["description"],
            codes=t.get("codes", []),
            quotes=[
                Quote(quote_id=q["quote_id"], text=q["text"])
                for q in t.get("quotes", [])
            ],
        )
        for t in data.get("themes", [])
    ]
    return ThemeResult(themes=themes)


# ── Stage 2 theme-coder worker ────────────────────────────────────────────────

ThemeCoderFactory = Callable[[Codebook, "store.ThemeCoder"], Any]


def default_theme_coder_factory(
    codebook: Codebook, theme_coder: "store.ThemeCoder"
) -> ThemeCoderAgent:
    return ThemeCoderAgent(
        config=ThemeCoderConfig(identity=theme_coder.identity),
        codebook=codebook,
    )


def theme_code_one(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    codebook_version: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ThemeCoderFactory | None = None,
) -> dict | None:
    """Run one theme coder against the given codebook version. Returns None if
    this coder already has a 'done' run for that version (nothing to do)."""
    theme_coder = store.get_theme_coder(conn, theme_coder_id)
    if theme_coder is None:
        raise ValueError(f"unknown theme_coder_id: {theme_coder_id!r}")

    # Check whether there's already a done run (idempotent guard)
    existing = conn.execute(
        "SELECT status FROM theme_coder_runs "
        "WHERE theme_coder_id = ? AND codebook_version = ?",
        (theme_coder_id, codebook_version),
    ).fetchone()
    if existing is not None and existing["status"] == "done":
        return None

    run_id = store.start_theme_coder_run(conn, theme_coder_id, codebook_version)
    if run_id is None:
        # Race: another worker already claimed this slot; signal "no work".
        return None

    factory = agent_factory or default_theme_coder_factory
    try:
        codebook = _get_codebook(conn, codebook_version, use_mock_embeddings)
        agent = factory(codebook, theme_coder)
        t0 = time.monotonic()
        result = agent.develop_themes()
        result_json = result.to_json()
        store.record_theme_coder_result(conn, run_id, result_json)
        return {
            "ok": True,
            "run_id": run_id,
            "theme_coder_id": theme_coder_id,
            "codebook_version": codebook_version,
            "n_themes": len(result.themes),
            "elapsed": time.monotonic() - t0,
        }
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        store.record_theme_coder_failure(conn, run_id, msg)
        return {
            "ok": False,
            "run_id": run_id,
            "theme_coder_id": theme_coder_id,
            "codebook_version": codebook_version,
            "error": msg,
        }


async def theme_code_one_async(
    conn: sqlite3.Connection,
    theme_coder_id: str,
    codebook_version: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ThemeCoderFactory | None = None,
) -> dict | None:
    return theme_code_one(
        conn,
        theme_coder_id,
        codebook_version,
        use_mock_embeddings=use_mock_embeddings,
        agent_factory=agent_factory,
    )


async def drain_theme_code_async(
    conn: sqlite3.Connection,
    codebook_version: int,
    *,
    workers: int = 1,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: ThemeCoderFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    """Run all pending theme coders against codebook_version, up to `workers`
    concurrently. Each coder runs at most once."""
    pending = [
        r["theme_coder_id"]
        for r in store.theme_coders_to_run(conn, codebook_version)
    ]
    if limit is not None:
        pending = pending[:limit]

    counters: dict[str, int] = {"done": 0, "failed": 0}
    sem = asyncio.Semaphore(max(1, workers))

    async def run_one(coder_id: str) -> None:
        async with sem:
            res = await theme_code_one_async(
                conn,
                coder_id,
                codebook_version,
                use_mock_embeddings=use_mock_embeddings,
                agent_factory=agent_factory,
            )
            if res is None:
                return
            if res["ok"]:
                counters["done"] += 1
            else:
                counters["failed"] += 1
            if on_event is not None:
                on_event(res, counters)

    await asyncio.gather(*[run_one(cid) for cid in pending])
    return counters


# ── Stage 2 theme-aggregator worker ──────────────────────────────────────────

ThemeAggregatorFactory = Callable[[], Any]


def default_theme_aggregator_factory() -> ThemeAggregatorAgent:
    return ThemeAggregatorAgent(config=ThemeAggregatorConfig())


def theme_aggregate_one(
    conn: sqlite3.Connection,
    codebook_version: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ThemeAggregatorFactory | None = None,
) -> dict | None:
    """Aggregate theme results for codebook_version. Returns None if not all
    theme coders are done yet, or if an aggregation already exists."""
    if not store.all_theme_coders_done(conn, codebook_version):
        return None

    agg_id = store.start_theme_aggregation(conn, codebook_version)
    if agg_id is None:
        # Already running or done.
        return None

    runs = store.load_done_theme_coder_runs(conn, codebook_version)
    theme_results = [_theme_result_from_json(r.result_json) for r in runs]
    run_ids = [r.run_id for r in runs]

    factory = agent_factory or default_theme_aggregator_factory
    try:
        agent = factory()
        t0 = time.monotonic()
        result: ThemeAggregationResult = agent.aggregate(theme_results)
        result_json = result.to_json()
        store.record_theme_aggregation_result(conn, agg_id, result_json, run_ids)
        return {
            "ok": True,
            "aggregation_id": agg_id,
            "codebook_version": codebook_version,
            "n_input_results": len(theme_results),
            "n_themes": len(result.themes),
            "elapsed": time.monotonic() - t0,
        }
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        store.record_theme_aggregation_failure(conn, agg_id, msg)
        return {
            "ok": False,
            "aggregation_id": agg_id,
            "codebook_version": codebook_version,
            "error": msg,
        }
