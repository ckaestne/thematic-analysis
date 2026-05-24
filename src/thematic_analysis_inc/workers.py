"""Worker functions for the incremental Stage 1 pipeline.

These compose the helpers in :mod:`thematic_analysis_inc.db`. Stage-1
helpers are SQLModel-backed and no longer take a ``conn`` parameter.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from thematic_analysis.agents.aggregator import (
    AggregatorConfig,
    CodeAggregatorAgent,
)
from thematic_analysis.agents.coder import CoderAgent
from thematic_analysis.agents.reviewer import (
    ReviewerAgent,
    ReviewerConfig,
)
from thematic_analysis.agents.theme_coder import (
    ThemeCoderAgent,
    ThemeCoderConfig,
)

from thematic_analysis_inc import db
from thematic_analysis_inc.db import (
    aggregation as db_aggregation,
    cascades as db_cascades,
    codebook as db_codebook,
    coders as db_coders,
    coding as db_coding,
    embeddings as db_embeddings,
    review as db_review,
    theme as db_themes,
)
from thematic_analysis_inc.db.models import (
    Code,
    Codebook as DBCodebook,
    Coder,
    DECISION_ADD,
    DECISION_MERGE,
    DECISION_MERGE_AND_RENAME,
    SOURCE_JOB,
    Theme,
    ThemeCodingJob,
    is_sentinel_code,
)
from thematic_analysis_inc.refinement import wrap_with_refinement


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

ReviewerFactory = Callable[
    [DBCodebook, list[Code], db_embeddings.EmbeddingService], Any
]


def default_reviewer_factory(
    codebook: DBCodebook,
    live_codes: list[Code],
    embedding_service: db_embeddings.EmbeddingService,
) -> ReviewerAgent:
    return ReviewerAgent(
        codebook=codebook,
        live_codes=live_codes,
        embedding_service=embedding_service,
        config=ReviewerConfig(),
    )


def _make_embedding_service(
    use_mock_embeddings: bool,
) -> db_embeddings.EmbeddingService:
    return db_embeddings.EmbeddingService(use_mock=use_mock_embeddings)


def _load_review_inputs(
    code_id: int, parent_version: int
) -> tuple[Code, DBCodebook, list[Code]]:
    """Eager-load everything the reviewer needs (detached): the aggregator
    code with its quotes, the parent codebook with codes+quotes+context,
    and the live codes for the in-progress batch."""
    target = db_aggregation.get_aggregator_code(code_id)
    if target is None:
        raise ValueError(f"unknown aggregator code_id: {code_id}")
    # supporting_quotes weren't included in get_aggregator_code's load;
    # re-fetch with eager-load. (Keep both helpers; this is one extra hop
    # for a debug path and the review_one path that's fine.)
    parent = db_codebook.get_codebook_with_codes_and_research_context(
        parent_version
    )
    if parent is None:
        raise RuntimeError(
            f"codebook version {parent_version} disappeared mid-call"
        )
    # Ensure quotes are loaded for parent codes.
    for c in parent.codes:
        _ = list(c.supporting_quotes)
    live = db_codebook.live_codes_for_batch(parent)
    for c in live:
        _ = list(c.supporting_quotes)
    # Re-fetch target with quotes eagerly loaded.
    target_with_quotes = db_review.next_aggregated_code_to_review()
    # If next_aggregated picked something else (target already had its
    # 'R' edge), fall back to a manual fetch.
    if target_with_quotes is None or target_with_quotes.code_id != code_id:
        target_with_quotes = target
        target_with_quotes.supporting_quotes = list(
            target_with_quotes.supporting_quotes or []
        )
    return target_with_quotes, parent, live


def test_review_aggregated_code(
    code_id: int,
    *,
    use_mock_embeddings: bool = False,
) -> dict[str, Any]:
    """Run the reviewer on one aggregator code without writing anything.
    Returns a dict with the input code, the result Code (or None on
    error), and the agent itself so callers can inspect ``last_payload``
    / ``last_system_prompt`` / ``last_user_prompt`` / ``last_raw_response``
    / ``last_elapsed`` / ``last_shortcut`` / ``last_similar``."""
    target = db_aggregation.get_aggregator_code(code_id)
    if target is None:
        raise ValueError(f"unknown aggregator code_id: {code_id}")
    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")

    target_with_quotes, parent, live = _load_review_inputs(
        code_id, latest.version
    )
    service = _make_embedding_service(use_mock_embeddings)
    agent = default_reviewer_factory(parent, live, service)

    llm_error: str | None = None
    result_code: Code | None = None
    try:
        result_code = agent.review_code(target_with_quotes)
    except Exception as exc:
        llm_error = f"{type(exc).__name__}: {exc}"

    return {
        "code_id": code_id,
        "code": target.code,
        "segment_id": target.segment_id,
        "codebook_version": latest.version,
        "target": target_with_quotes,
        "agent": agent,
        "result": result_code,
        "llm_error": llm_error,
    }


def review_one(
    conn: sqlite3.Connection | None = None,
    *,
    use_mock_embeddings: bool = False,
    agent_factory: ReviewerFactory | None = None,
) -> dict | None:
    """Process one aggregator code: build a fresh reviewer Code (with
    provenance) and persist it. **Does not** create a new Codebook
    revision — that happens once at the end via :func:`finalize_codebook`.
    """
    target = db_review.next_aggregated_code_to_review()
    if target is None:
        return None

    latest = db_codebook.latest_codebook()
    if latest is None:
        raise RuntimeError("no codebook revision exists; run init first")
    parent = db_codebook.get_codebook_with_codes_and_research_context(
        latest.version
    )
    if parent is None:
        raise RuntimeError(
            f"codebook version {latest.version} disappeared mid-call"
        )
    for c in parent.codes:
        _ = list(c.supporting_quotes)
    live = db_codebook.live_codes_for_batch(parent)
    for c in live:
        _ = list(c.supporting_quotes)

    service = _make_embedding_service(use_mock_embeddings)
    factory = agent_factory or default_reviewer_factory
    agent = factory(parent, live, service)

    t0 = time.monotonic()
    result_code = agent.review_code(target)
    persisted = db_review.save_reviewer_decision(result_code)
    elapsed = time.monotonic() - t0

    edges = list(persisted.derivation_sources or [])
    decision_char = edges[0].decision if edges else None
    decision_name = {
        DECISION_ADD: "add_new",
        DECISION_MERGE: "merge",
        DECISION_MERGE_AND_RENAME: "merge_and_rename",
    }.get(decision_char or "", "add_new")

    return {
        "ok": True,
        "aggregated_code_id": target.code_id,
        "segment_id": target.segment_id,
        "code": target.code,
        "decision": decision_name,
        "new_code": persisted.code,
        "new_code_id": persisted.code_id,
        "elapsed": elapsed,
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
    while True:
        if limit is not None and counters["done"] + counters["failed"] >= limit:
            break
        res = review_one(
            None,
            use_mock_embeddings=use_mock_embeddings,
            agent_factory=agent_factory,
        )
        if res is None:
            break
        counters["done"] += 1
        if on_event is not None:
            on_event(res, counters)
    return counters


def finalize_codebook() -> int | None:
    """Materialize one new ``Codebook`` revision capturing every reviewer
    decision written against the current latest codebook. Returns the new
    version, or ``None`` if no membership changed (caller can print
    ``codebook unchanged``)."""
    parent = db_codebook.latest_codebook()
    if parent is None:
        return None
    # Re-fetch with codes eager-loaded so live_codes_for_batch / diff work.
    parent_full = db_codebook.get_codebook_with_codes_and_research_context(
        parent.version
    )
    if parent_full is None:
        return None
    new_cb = db_codebook.materialize_codebook_revision(parent_full)
    return new_cb.version if new_cb is not None else None


# ── Stage 2 — theme coding ──────────────────────────────────────────────────


ThemeCoderFactory = Callable[[], ThemeCoderAgent]


def default_theme_coder_factory() -> ThemeCoderAgent:
    return ThemeCoderAgent(config=ThemeCoderConfig())


def run_theme_coding_job(
    job: ThemeCodingJob,
    *,
    agent_factory: ThemeCoderFactory | None = None,
) -> list[Theme]:
    """Run a single theme-coding job end-to-end.

    Loads the codebook revision pinned by ``job``, hands it to a fresh
    ``ThemeCoderAgent`` with ``job.prompt`` as the customisable system
    section, then persists every returned theme (with its code + quote
    links) in one transaction. Returns the persisted (detached) themes.

    Raises ``ValueError`` if the pinned codebook revision is missing.
    Agent failures propagate; nothing is written on failure.
    """
    codebook = db_codebook.get_codebook_with_codes_and_research_context(
        job.codebook_used_id
    )
    if codebook is None:
        raise ValueError(
            f"codebook version {job.codebook_used_id} not found "
            f"(referenced by theme_coding_job id={job.id})"
        )

    factory = agent_factory or default_theme_coder_factory
    agent = factory()
    themes = agent.develop_themes(codebook, job.prompt)
    for t in themes:
        t.source = SOURCE_JOB
        t.theme_coding_job_id = job.id
        # Agent already set codebook_used_id from the codebook it saw;
        # re-assert for clarity in case a test factory forgot.
        t.codebook_used_id = codebook.version
    return db_themes.save_themes(themes)
