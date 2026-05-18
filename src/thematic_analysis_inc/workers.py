"""Worker functions for the incremental Stage 1 + Stage 2 pipelines.

These compose the helpers in `thematic_analysis_inc.db`. No SQL lives
here.
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
from thematic_analysis.agents.reviewer import (
    ReviewDecision,
    ReviewerAgent,
    ReviewerConfig,
)
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

from thematic_analysis_inc import db
from thematic_analysis_inc.db import (
    aggregation as db_aggregation,
    cascades as db_cascades,
    codebook as db_codebook,
    coders as db_coders,
    coding as db_coding,
    review as db_review,
    theme as db_theme,
)
from thematic_analysis_inc.refinement import wrap_with_refinement


# ── Codebook cache ───────────────────────────────────────────────────────────

_codebook_cache: dict[int, Codebook] = {}


def clear_codebook_cache() -> None:
    _codebook_cache.clear()


def _apply_research_context(conn: sqlite3.Connection, agent: Any) -> None:
    if not hasattr(agent, "research_context"):
        return
    ctx = db.get_research_context(conn)
    if ctx is None or ctx.is_empty():
        return
    agent.research_context = ctx


def _get_codebook(
    conn: sqlite3.Connection, version: int, use_mock_embeddings: bool
) -> Codebook:
    cb = _codebook_cache.get(version)
    if cb is not None:
        return cb
    snapshot = db_codebook.codebook_to_json_for_version(conn, version)
    cb = Codebook.from_json(snapshot, use_mock_embeddings=use_mock_embeddings)
    _codebook_cache[version] = cb
    return cb


# ── Coder agent factory ──────────────────────────────────────────────────────

AgentFactory = Callable[[Codebook, db_coders.Coder], Any]


def default_coder_factory(codebook: Codebook, coder: db_coders.Coder) -> Any:
    base = CoderAgent(
        config=CoderConfig(identity=coder.identity), codebook=codebook
    )
    return wrap_with_refinement(base)


# ── Stage-A coder worker ─────────────────────────────────────────────────────


def code_one(
    conn: sqlite3.Connection,
    coder_id: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    """Process one un-coded segment for the given coder. Returns None when
    the coder has nothing left to do."""
    coder = db_coders.get_coder(conn, coder_id)
    if coder is None or coder_id < 1:
        raise ValueError(f"unknown or system coder_id: {coder_id}")

    db_coding.sync_coding_queue(conn)
    assignment = db_coding.claim_next_coding_assignment(conn, coder_id)
    if assignment is None:
        return None

    factory = agent_factory or default_coder_factory
    segment_id = assignment.segment_id
    text = assignment.content
    version = assignment.codebook_version

    try:
        codebook = _get_codebook(conn, version, use_mock_embeddings)
        agent = factory(codebook, coder)
        _apply_research_context(conn, agent)
        t0 = time.monotonic()
        result = agent.code_segment(str(segment_id), text)
        db_coding.record_coding_result(
            conn,
            segment_id=segment_id,
            coder_id=coder_id,
            version=version,
            codes=list(result.codes),
            rationales=list(result.rationales),
        )
        elapsed = time.monotonic() - t0
        res: dict[str, Any] = {
            "ok": True,
            "coder_id": coder_id,
            "segment_id": segment_id,
            "version": version,
            "n_codes": len(result.codes),
            "elapsed": elapsed,
        }
        trace = getattr(agent, "last_trace", None)
        if trace is not None:
            res["trace"] = trace
        return res
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        db_coding.record_coding_failure(
            conn, segment_id=segment_id, coder_id=coder_id, error=msg
        )
        return {
            "ok": False,
            "coder_id": coder_id,
            "segment_id": segment_id,
            "version": version,
            "error": msg,
        }


async def code_one_async(
    conn: sqlite3.Connection,
    coder_id: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    coder = db_coders.get_coder(conn, coder_id)
    if coder is None or coder_id < 1:
        raise ValueError(f"unknown or system coder_id: {coder_id}")

    db_coding.sync_coding_queue(conn)
    assignment = db_coding.claim_next_coding_assignment(conn, coder_id)
    if assignment is None:
        return None

    factory = agent_factory or default_coder_factory
    segment_id = assignment.segment_id
    text = assignment.content
    version = assignment.codebook_version

    try:
        codebook = _get_codebook(conn, version, use_mock_embeddings)
        agent = factory(codebook, coder)
        _apply_research_context(conn, agent)
        t0 = time.monotonic()
        if hasattr(agent, "code_segment_async"):
            result = await agent.code_segment_async(str(segment_id), text)
        else:
            result = agent.code_segment(str(segment_id), text)
        db_coding.record_coding_result(
            conn,
            segment_id=segment_id,
            coder_id=coder_id,
            version=version,
            codes=list(result.codes),
            rationales=list(result.rationales),
        )
        elapsed = time.monotonic() - t0
        res: dict[str, Any] = {
            "ok": True,
            "coder_id": coder_id,
            "segment_id": segment_id,
            "version": version,
            "n_codes": len(result.codes),
            "elapsed": elapsed,
        }
        trace = getattr(agent, "last_trace", None)
        if trace is not None:
            res["trace"] = trace
        return res
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        db_coding.record_coding_failure(
            conn, segment_id=segment_id, coder_id=coder_id, error=msg
        )
        return {
            "ok": False,
            "coder_id": coder_id,
            "segment_id": segment_id,
            "version": version,
            "error": msg,
        }


async def drain_code_async(
    conn: sqlite3.Connection,
    coder_id: int,
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


# ── Aggregator worker ────────────────────────────────────────────────────────

AggregatorFactory = Callable[[Codebook], Any]


def default_aggregator_factory(codebook: Codebook) -> CodeAggregatorAgent:
    return CodeAggregatorAgent(config=AggregatorConfig(), codebook=codebook)


def _build_assignments(
    segment_id: int, text: str, coder_codes: list[db_coding.CoderCodes]
) -> list[CodeAssignment]:
    return [
        CodeAssignment(
            segment_id=str(segment_id),
            segment_text=text,
            codes=[c.code for c in cc.codes],
            rationales=[c.rationale for c in cc.codes],
            is_new_code=[False] * len(cc.codes),
        )
        for cc in coder_codes
    ]


def aggregate_one(
    conn: sqlite3.Connection,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AggregatorFactory | None = None,
) -> dict | None:
    """Aggregate codes for one ready segment."""
    row = db_aggregation.next_segment_to_aggregate(conn)
    if row is None:
        return None
    segment_id = int(row["segment_id"])
    text = row["content"]

    # Get latest codebook version for the aggregator code's `version` column.
    latest = db_codebook.latest_codebook_version(conn)
    if latest is None:
        raise RuntimeError("no codebook version exists; run init first")
    version = latest.version

    coder_codes = db_coding.load_segment_coder_codes(conn, segment_id)
    # Build (coder_id, code_text) -> code_id map for resolving sources.
    code_to_id: dict[tuple[int, str], int] = {}
    for cc in coder_codes:
        for c in cc.codes:
            code_to_id[(cc.coder_id, c.code)] = c.code_id

    n_in = sum(len(cc.codes) for cc in coder_codes)
    try:
        codebook = Codebook(use_mock_embeddings=use_mock_embeddings)
        factory = agent_factory or default_aggregator_factory
        agent = factory(codebook)
        t0 = time.monotonic()
        assignments = _build_assignments(segment_id, text, coder_codes)
        result = agent.aggregate(assignments)

        # Race-safety check: if another worker already aggregated this
        # segment, bail before writing.
        if db_aggregation.segment_has_aggregator_code(conn, segment_id):
            return {
                "ok": False,
                "segment_id": segment_id,
                "error": "aggregator code already exists (race)",
                "skipped": True,
            }

        inputs: list[db_aggregation.AggregatorMergeInput] = []
        for mc in result.all_codes():
            # Resolve source code_ids by matching original_codes against the
            # codes produced by any coder for this segment.
            source_ids: list[int] = []
            seen: set[int] = set()
            for orig in mc.original_codes:
                for (cid, ctext), code_id in code_to_id.items():
                    if ctext == orig and code_id not in seen:
                        source_ids.append(code_id)
                        seen.add(code_id)
            inputs.append(
                db_aggregation.AggregatorMergeInput(
                    code=mc.code,
                    description="",
                    rationale=mc.merge_rationale or "",
                    quote_texts=[q.text for q in mc.quotes],
                    source_code_ids=source_ids,
                )
            )

        db_aggregation.record_aggregation_result(
            conn, segment_id=segment_id, version=version, inputs=inputs
        )
        return {
            "ok": True,
            "segment_id": segment_id,
            "n_in": n_in,
            "n_merged": len(result.merged_codes),
            "n_retained": len(result.retained_codes),
            "elapsed": time.monotonic() - t0,
        }
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        return {
            "ok": False,
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
            continue
        if res["ok"]:
            counters["done"] += 1
        else:
            counters["failed"] += 1
        if on_event is not None:
            on_event(res, counters)
    return counters


# ── Reviewer worker ──────────────────────────────────────────────────────────

ReviewerFactory = Callable[[Codebook], Any]


def default_reviewer_factory(codebook: Codebook) -> ReviewerAgent:
    return ReviewerAgent(config=ReviewerConfig(), codebook=codebook)


_DECISION_TO_CHAR = {
    ReviewDecision.ADD_NEW: db_review.DECISION_ADD,
    ReviewDecision.MERGE: db_review.DECISION_MERGE,
    ReviewDecision.UPDATE: db_review.DECISION_UPDATE,
}


def review_one(
    conn: sqlite3.Connection,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
) -> dict | None:
    target = db_review.next_aggregated_code_to_review(conn)
    if target is None:
        return None

    quotes_data = db_aggregation.load_aggregated_code_quotes(
        conn, target.code_id
    )
    quotes = [
        Quote(quote_id=str(q["quote_id"]), text=q["text"]) for q in quotes_data
    ]

    latest = db_codebook.latest_codebook_version(conn)
    if latest is None:
        raise RuntimeError("no codebook version exists; run init first")
    parent_version = latest.version

    codebook = _get_codebook(conn, parent_version, use_mock_embeddings)
    factory = agent_factory or default_reviewer_factory
    agent = factory(codebook)
    _apply_research_context(conn, agent)

    t0 = time.monotonic()
    result = agent.review_code(target.code, quotes)
    agent.apply_review(result)
    # The in-memory codebook is now stale w.r.t. the DB-derived version,
    # so evict the cache for the new (about-to-be-created) version.
    _codebook_cache.pop(parent_version, None)

    new_version: int | None = None
    if result.decision == ReviewDecision.SKIP:
        # SKIP leaves no DB trace per the design.
        return {
            "ok": True,
            "aggregated_code_id": target.code_id,
            "segment_id": target.segment_id,
            "code": target.code,
            "decision": result.decision.value,
            "target_code": result.target_code,
            "new_version": None,
            "elapsed": time.monotonic() - t0,
        }

    decision_char = _DECISION_TO_CHAR[result.decision]
    target_code_id: int | None = None
    if result.decision in (ReviewDecision.MERGE, ReviewDecision.UPDATE):
        if result.target_code is None:
            raise RuntimeError(
                f"reviewer decision {result.decision.value} requires "
                "target_code"
            )
        target_code_id = db_review.resolve_target_code_id(
            conn, version=parent_version, code_text=result.target_code
        )
        if target_code_id is None:
            raise RuntimeError(
                f"target code {result.target_code!r} not found in codebook "
                f"v{parent_version}"
            )

    new_text = result.code
    new_version = db_review.record_review(
        conn,
        source_agg_code_id=target.code_id,
        decision=decision_char,
        new_code_text=new_text,
        new_description="",
        rationale=result.rationale or "",
        parent_version=parent_version,
        target_code_id=target_code_id,
    )

    # On MERGE: the new reviewer code isn't in the codebook; copy quotes
    # onto the existing target reviewer code so the on-disk codebook
    # accumulates them.
    if result.decision == ReviewDecision.MERGE and target_code_id is not None:
        for q in quotes:
            qid = db.add_quote(conn, target.segment_id, q.text)
            db.link_code_quote(conn, target_code_id, qid)
    else:
        # ADD / UPDATE: link quotes onto the new reviewer code.
        new_reviewer_code_id = db_review.find_reviewer_code_by_text(
            conn, new_text
        )
        if new_reviewer_code_id is not None:
            for q in quotes:
                qid = db.add_quote(conn, target.segment_id, q.text)
                db.link_code_quote(conn, new_reviewer_code_id, qid)

    _codebook_cache.pop(new_version, None)

    return {
        "ok": True,
        "aggregated_code_id": target.code_id,
        "segment_id": target.segment_id,
        "code": target.code,
        "decision": result.decision.value,
        "target_code": result.target_code,
        "new_version": new_version,
        "elapsed": time.monotonic() - t0,
    }


def drain_review(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    counters = {"done": 0, "failed": 0}
    # SKIP decisions don't write a `codes_derived` edge (by design), so
    # `next_aggregated_code_to_review` would keep returning the same code.
    # Track skipped code_ids in-memory for this drain so we make progress.
    skipped: set[int] = set()
    while True:
        if limit is not None and counters["done"] + counters["failed"] >= limit:
            break
        target = db_review.next_aggregated_code_to_review(conn)
        if target is None:
            break
        if target.code_id in skipped:
            # Everything left in the queue is something we've skipped this
            # drain; treat as "nothing more to do".
            break
        res = review_one(
            conn,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
        if res is None:
            break
        counters["done"] += 1
        if res.get("decision") == ReviewDecision.SKIP.value:
            skipped.add(int(res["aggregated_code_id"]))
        if on_event is not None:
            on_event(res, counters)
    return counters


# ── Stage 2 helpers ──────────────────────────────────────────────────────────


def _theme_result_from_json(json_str: str) -> ThemeResult:
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


ThemeCoderFactory = Callable[[Codebook, db_theme.ThemeCoder], Any]


def default_theme_coder_factory(
    codebook: Codebook, theme_coder: db_theme.ThemeCoder
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
    theme_coder = db_theme.get_theme_coder(conn, theme_coder_id)
    if theme_coder is None:
        raise ValueError(f"unknown theme_coder_id: {theme_coder_id!r}")

    existing_status = db_theme.get_existing_theme_coder_run_status(
        conn, theme_coder_id, codebook_version
    )
    if existing_status == "done":
        return None

    run_id = db_theme.start_theme_coder_run(
        conn, theme_coder_id, codebook_version
    )
    if run_id is None:
        return None

    factory = agent_factory or default_theme_coder_factory
    try:
        codebook = _get_codebook(conn, codebook_version, use_mock_embeddings)
        agent = factory(codebook, theme_coder)
        _apply_research_context(conn, agent)
        t0 = time.monotonic()
        result = agent.develop_themes()
        result_json = result.to_json()
        db_theme.record_theme_coder_result(conn, run_id, result_json)
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
        db_theme.record_theme_coder_failure(conn, run_id, msg)
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
    pending = [
        r["theme_coder_id"]
        for r in db_theme.theme_coders_to_run(conn, codebook_version)
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
    if not db_theme.all_theme_coders_done(conn, codebook_version):
        return None

    agg_id = db_theme.start_theme_aggregation(conn, codebook_version)
    if agg_id is None:
        return None

    runs = db_theme.load_done_theme_coder_runs(conn, codebook_version)
    theme_results = [_theme_result_from_json(r.result_json) for r in runs]
    run_ids = [r.run_id for r in runs]

    factory = agent_factory or default_theme_aggregator_factory
    try:
        agent = factory()
        _apply_research_context(conn, agent)
        t0 = time.monotonic()
        result: ThemeAggregationResult = agent.aggregate(theme_results)
        result_json = result.to_json()
        db_theme.record_theme_aggregation_result(
            conn, agg_id, result_json, run_ids
        )
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
        db_theme.record_theme_aggregation_failure(conn, agg_id, msg)
        return {
            "ok": False,
            "aggregation_id": agg_id,
            "codebook_version": codebook_version,
            "error": msg,
        }
