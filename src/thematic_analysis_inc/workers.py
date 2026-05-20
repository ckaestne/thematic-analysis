"""Worker functions for the incremental Stage 1 + Stage 2 pipelines.

These compose the helpers in :mod:`thematic_analysis_inc.db`. Stage-1
helpers are SQLModel-backed and no longer take a ``conn`` parameter;
Stage-2 helpers (``theme_*``) still need a ``sqlite3.Connection``.
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
from thematic_analysis.agents.coder import CoderAgent
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
from thematic_analysis.codebook import Codebook as DomainCodebook
from thematic_analysis.codebook.codebook import Quote as DomainQuote

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
from thematic_analysis_inc.db.models import (
    Code,
    Codebook as DBCodebook,
    Coder,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_UPDATE,
    is_sentinel_code,
)
from thematic_analysis_inc.refinement import wrap_with_refinement


# ── Codebook cache ───────────────────────────────────────────────────────────

_codebook_cache: dict[int, DomainCodebook] = {}


def clear_codebook_cache() -> None:
    _codebook_cache.clear()


def _apply_research_context(agent: Any) -> None:
    if not hasattr(agent, "research_context"):
        return
    rc = db.get_research_context()
    if rc is None:
        return
    ctx = db.research_context_to_domain(rc)
    if ctx.is_empty():
        return
    agent.research_context = ctx


def _get_codebook(version: int, use_mock_embeddings: bool) -> DomainCodebook:
    cb = _codebook_cache.get(version)
    if cb is not None:
        return cb
    snapshot = db_codebook.codebook_to_json_for_version(version)
    cb = DomainCodebook.from_json(
        snapshot, use_mock_embeddings=use_mock_embeddings
    )
    # Pin the research context to the codebook revision so agents bound
    # to this codebook can read it off `codebook.research_context`
    # instead of taking a separate constructor argument.
    db_cb = db_codebook.get_codebook_with_codes_and_research_context(version)
    if db_cb is not None and db_cb.research_context is not None:
        cb.research_context = db.research_context_to_domain(db_cb.research_context)
    _codebook_cache[version] = cb
    return cb


# ── Stage-A coder worker ─────────────────────────────────────────────────────

AgentFactory = Callable[[DBCodebook, Coder], Any]


def default_coder_factory(codebook: DBCodebook, coder: Coder) -> Any:
    base = CoderAgent(coder=coder, codebook=codebook)
    return wrap_with_refinement(base)


async def _run_coder_for_segment(
    segment_id: int,
    coder: Coder,
    version: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict[str, Any]:
    seg = db.get_segment(segment_id)
    if seg is None:
        raise ValueError(f"unknown segment_id: {segment_id}")

    codebook = db_codebook.get_codebook_with_codes_and_research_context(version)
    if codebook is None:
        raise ValueError(f"unknown codebook version: {version}")
    factory = agent_factory or default_coder_factory
    agent = factory(codebook, coder)

    t0 = time.monotonic()
    if hasattr(agent, "code_segment_async"):
        codes = await agent.code_segment_async(seg)
    else:
        codes = agent.code_segment(seg)

    res: dict[str, Any] = {
        "ok": True,
        "coder_id": coder.coder_id,
        "segment_id": segment_id,
        "version": version,
        "n_codes": len(codes),
        "elapsed": time.monotonic() - t0,
        "codes": codes,
    }
    trace = getattr(agent, "last_trace", None)
    if trace is not None:
        res["trace"] = trace
    return res


def test_code_segment(
    segment_id: int,
    coder_id: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        test_code_segment_async(
            segment_id,
            coder_id,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
    )


async def test_code_segment_async(
    segment_id: int,
    coder_id: int,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict[str, Any]:
    coder = db_coders.get_coder(coder_id)
    if coder is None or coder_id < 1:
        raise ValueError(f"unknown or system coder_id: {coder_id}")

    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")

    return await _run_coder_for_segment(
        segment_id,
        coder,
        latest.version,
        use_mock_embeddings=use_mock_embeddings,
        agent_factory=agent_factory,
    )


def code_one(
    conn: sqlite3.Connection | None,
    coder_id: int | None = None,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    """Process one un-coded segment. If ``coder_id`` is given, restrict to
    that coder's queue rows; otherwise claim any pending row. ``conn`` is
    accepted (and ignored) for backwards compatibility — Stage-1 helpers
    use their own SQLModel session."""
    return asyncio.run(
        code_one_async(
            conn,
            coder_id,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
    )


async def code_one_async(
    conn: sqlite3.Connection | None,
    coder_id: int | None = None,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
) -> dict | None:
    coder_filter = None
    if coder_id is not None:
        coder_filter = db_coders.get_coder(coder_id)
        if coder_filter is None or coder_id < 1:
            raise ValueError(f"unknown or system coder_id: {coder_id}")
    assignment = db_coding.claim_next_assignment(coder_filter)
    if assignment is None:
        return None
    coder = db_coders.get_coder(assignment.coder_id)
    if coder is None:
        msg = f"unknown coder_id on queue row: {assignment.coder_id}"
        db_coding.record_coding_failure(assignment, error=msg)
        return {
            "ok": False,
            "coder_id": assignment.coder_id,
            "segment_id": assignment.segment_id,
            "version": assignment.codebook_used_id,
            "error": msg,
        }

    segment_id = assignment.segment_id
    version = assignment.codebook_used_id

    if db_coding.assignment_has_codes(assignment):
        db_coding.mark_assignment_finished(assignment)
        return {
            "ok": True,
            "coder_id": coder.coder_id,
            "segment_id": segment_id,
            "version": version,
            "n_codes": 0,
            "elapsed": 0.0,
            "skipped": True,
        }

    factory = agent_factory or default_coder_factory
    try:
        res = await _run_coder_for_segment(
            segment_id,
            coder,
            version,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=factory,
        )
        db_coding.save_codes_and_finish_assignment(assignment, res["codes"])
        res.pop("codes", None)
        return res
    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        db_coding.record_coding_failure(assignment, error=msg)
        return {
            "ok": False,
            "coder_id": coder.coder_id,
            "segment_id": segment_id,
            "version": version,
            "error": msg,
        }


async def drain_code_async(
    conn: sqlite3.Connection | None,
    *,
    workers: int = 1,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: AgentFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    """Drain the coding queue across all coders."""
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

AggregatorFactory = Callable[[], CodeAggregatorAgent]


def default_aggregator_factory() -> CodeAggregatorAgent:
    return CodeAggregatorAgent(config=AggregatorConfig())


def _do_aggregate(
    seg: Any,
    codebook_version: int,
    *,
    agent_factory: AggregatorFactory | None = None,
) -> dict:
    """Core aggregation logic for one (segment, codebook_version) pair."""
    from thematic_analysis_inc.db.models import SENTINEL_CODE_LABEL

    segment_id = seg.segment_id
    t0 = time.monotonic()
    try:
        pair = db_aggregation.load_segment_and_codebook_for_aggregation(
            segment_id, codebook_version
        )
        if pair is None:
            return {
                "ok": False,
                "segment_id": segment_id,
                "codebook_version": codebook_version,
                "error": f"codebook version {codebook_version} not found",
            }
        attached, codebook = pair

        if db_aggregation.segment_has_aggregator_code(
            segment_id, codebook_version
        ):
            return {
                "ok": False,
                "segment_id": segment_id,
                "codebook_version": codebook_version,
                "error": "aggregator code already exists (race)",
                "skipped": True,
            }

        n_in = sum(
            1
            for c in attached.codes
            if c.coder_id >= 1
            and c.codebook_used_id == codebook_version
            and not is_sentinel_code(c)
        )

        factory = agent_factory or default_aggregator_factory
        agent = factory()
        result_codes = agent.aggregate(attached, codebook)

        written = db_aggregation.save_aggregator_codes(result_codes)
        empty = (
            len(result_codes) == 1
            and result_codes[0].code == SENTINEL_CODE_LABEL
        )
        return {
            "ok": True,
            "segment_id": segment_id,
            "codebook_version": codebook_version,
            "n_in": n_in,
            "n_out": len(result_codes),
            "n_new": len(written),
            "elapsed": time.monotonic() - t0,
            "empty": empty,
        }
    except Exception as exc:
        return {
            "ok": False,
            "segment_id": segment_id,
            "codebook_version": codebook_version,
            "error": f"{type(exc).__name__}: {exc}",
        }


def aggregate_one(
    conn: sqlite3.Connection | None = None,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AggregatorFactory | None = None,
) -> dict | None:
    """Find the next (segment, codebook_version) pair that needs aggregation
    and process it. The codebook version is read from the database — the
    latest version is NOT assumed. Returns None when nothing is ready."""
    pair = db_aggregation.next_segment_codebook_to_aggregate()
    if pair is None:
        return None
    seg, codebook_version = pair
    return _do_aggregate(seg, codebook_version, agent_factory=agent_factory)


def aggregate_segment(
    conn: sqlite3.Connection | None = None,
    segment_id: int = 0,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: AggregatorFactory | None = None,
) -> list[dict]:
    """Aggregate all unaggregated codebook versions for a specific segment.

    Finds every codebook version that has finished coder codes but no
    aggregator code yet for ``segment_id``, then calls the core aggregation
    logic for each in ascending version order.
    """
    seg = db.get_segment(segment_id)
    if seg is None:
        raise ValueError(f"unknown segment_id: {segment_id}")

    versions = db_aggregation.unaggregated_codebook_versions_for_segment(
        segment_id
    )
    return [
        _do_aggregate(seg, v, agent_factory=agent_factory) for v in versions
    ]


def test_aggregate_segment(segment_id: int) -> dict[str, Any]:
    """Run the aggregator on one segment without writing anything. Returns
    a dict with the segment, the result Code list, and the agent itself
    (so callers can inspect ``last_system_prompt`` / ``last_user_prompt``
    / ``last_raw_response`` / ``last_elapsed`` / ``last_attempts``)."""
    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")

    pair = db_aggregation.load_segment_and_codebook_for_aggregation(
        segment_id, latest.version
    )
    if pair is None:
        raise ValueError(f"unknown segment_id: {segment_id}")
    attached, codebook = pair

    coder_codes = [
        c
        for c in attached.codes
        if c.coder_id >= 1
        and c.codebook_used_id == latest.version
        and not is_sentinel_code(c)
    ]

    agent = CodeAggregatorAgent(config=AggregatorConfig())
    result: list[Code] = []
    llm_error: str | None = None
    try:
        result = agent.aggregate(attached, codebook)
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {exc}"

    return {
        "segment_id": segment_id,
        "segment_text": attached.content,
        "codebook_version": latest.version,
        "coder_codes": coder_codes,
        "agent": agent,
        "result": result,
        "llm_error": llm_error,
    }


def drain_aggregate(
    conn: sqlite3.Connection | None = None,
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
            None,
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

ReviewerFactory = Callable[[DomainCodebook], Any]


def default_reviewer_factory(codebook: DomainCodebook) -> ReviewerAgent:
    return ReviewerAgent(config=ReviewerConfig(), codebook=codebook)


_DECISION_TO_CHAR = {
    ReviewDecision.ADD_NEW: DECISION_ADD,
    ReviewDecision.MERGE: DECISION_MERGE,
    ReviewDecision.UPDATE: DECISION_UPDATE,
}


def test_review_aggregated_code(
    code_id: int,
    *,
    use_mock_embeddings: bool = False,
) -> dict[str, Any]:
    """Run the reviewer on one aggregator code without writing anything.
    Returns a dict with the input code, the result, and the agent itself
    so callers can inspect ``last_payload`` / ``last_system_prompt`` /
    ``last_user_prompt`` / ``last_raw_response`` / ``last_elapsed`` /
    ``last_shortcut`` / ``last_similar_codes``."""
    target = db_aggregation.get_aggregator_code(code_id)
    if target is None:
        raise ValueError(f"unknown aggregator code_id: {code_id}")

    quotes_data = db_aggregation.load_aggregated_code_quotes(code_id)
    quotes = [
        DomainQuote(quote_id=str(q["quote_id"]), text=q["text"])
        for q in quotes_data
    ]

    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")

    codebook = _get_codebook(latest.version, use_mock_embeddings)
    agent = default_reviewer_factory(codebook)

    llm_error: str | None = None
    result = None
    try:
        result = agent.review_code(target.code, quotes)
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {exc}"

    return {
        "code_id": code_id,
        "code": target.code,
        "segment_id": target.segment_id,
        "codebook_version": latest.version,
        "quotes": quotes,
        "agent": agent,
        "result": result,
        "llm_error": llm_error,
    }


def review_one(
    conn: sqlite3.Connection | None = None,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
) -> dict | None:
    target = db_review.next_aggregated_code_to_review()
    if target is None:
        return None

    quotes_data = db_aggregation.load_aggregated_code_quotes(target.code_id)
    quotes = [
        DomainQuote(quote_id=str(q["quote_id"]), text=q["text"])
        for q in quotes_data
    ]

    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")
    parent_cb = latest

    codebook = _get_codebook(parent_cb.version, use_mock_embeddings)
    factory = agent_factory or default_reviewer_factory
    agent = factory(codebook)

    t0 = time.monotonic()
    result = agent.review_code(target.code, quotes)

    if result.decision == ReviewDecision.SKIP:
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
    target_code: Code | None = None
    if result.decision in (ReviewDecision.MERGE, ReviewDecision.UPDATE):
        if result.target_code is None:
            raise RuntimeError(
                f"reviewer decision {result.decision.value} requires "
                "target_code"
            )
        target_code = db_review.resolve_target_code(
            parent_cb, result.target_code
        )
        if target_code is None:
            raise RuntimeError(
                f"target code {result.target_code!r} not found in codebook "
                f"v{parent_cb.version}"
            )

    new_text = result.code
    new_cb = db_review.apply_review_and_create_codebook_revision(
        source_agg_code=target,
        decision=decision_char,
        new_code_text=new_text,
        new_description="",
        rationale=result.rationale or "",
        parent_codebook=parent_cb,
        target_code=target_code,
    )

    # Quote handling: copy quotes onto the right code in DB so the
    # codebook revision picks them up via codebook_code membership.
    seg = db.get_segment(target.segment_id)
    if result.decision == ReviewDecision.MERGE and target_code is not None:
        for q in quotes:
            qrow = db.add_quote(seg, q.text)
            db.link_code_quote(target_code, qrow)
    else:
        new_reviewer_code = db_review.find_reviewer_code_by_text(new_text)
        if new_reviewer_code is not None:
            for q in quotes:
                qrow = db.add_quote(seg, q.text)
                db.link_code_quote(new_reviewer_code, qrow)

    _codebook_cache.pop(new_cb.version, None)

    return {
        "ok": True,
        "aggregated_code_id": target.code_id,
        "segment_id": target.segment_id,
        "code": target.code,
        "decision": result.decision.value,
        "target_code": result.target_code,
        "new_version": new_cb.version,
        "elapsed": time.monotonic() - t0,
    }


def drain_review(
    conn: sqlite3.Connection | None = None,
    *,
    limit: int | None = None,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
    on_event: Callable[[dict, dict], None] | None = None,
) -> dict:
    counters = {"done": 0, "failed": 0}
    skipped: set[int] = set()
    while True:
        if limit is not None and counters["done"] + counters["failed"] >= limit:
            break
        target = db_review.next_aggregated_code_to_review()
        if target is None:
            break
        if target.code_id in skipped:
            break
        res = review_one(
            None,
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


# ── Stage 2 helpers (raw-SQL theme tables) ───────────────────────────────────


def _theme_result_from_json(json_str: str) -> ThemeResult:
    data = json.loads(json_str)
    themes = [
        Theme(
            name=t["name"],
            description=t["description"],
            codes=t.get("codes", []),
            quotes=[
                DomainQuote(quote_id=q["quote_id"], text=q["text"])
                for q in t.get("quotes", [])
            ],
        )
        for t in data.get("themes", [])
    ]
    return ThemeResult(themes=themes)


ThemeCoderFactory = Callable[[DomainCodebook, db_theme.ThemeCoder], Any]


def default_theme_coder_factory(
    codebook: DomainCodebook, theme_coder: db_theme.ThemeCoder
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
        codebook = _get_codebook(codebook_version, use_mock_embeddings)
        agent = factory(codebook, theme_coder)
        _apply_research_context(agent)
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
            res = theme_code_one(
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
        _apply_research_context(agent)
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
