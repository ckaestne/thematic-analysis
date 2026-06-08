"""Unified `ta` command-line interface for the thematic-analysis pipeline.

Subcommands are grouped (Setup, Documents, Stage 1) in `ta --help`.
Run `ta <command> --help` for per-command arguments.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Callable


# Quiet down noisy ML deps before anything imports them. The Coder/Reviewer
# embed codes via sentence-transformers, which by default streams a
# rich-formatted load report, a BERT load table, and per-encode tqdm bars
# to stdout/stderr. We don't need any of that in the CLI.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
for _name in ("sentence_transformers", "transformers", "huggingface_hub"):
    logging.getLogger(_name).setLevel(logging.ERROR)
try:
    from functools import partialmethod

    from tqdm import tqdm as _tqdm
    from tqdm.auto import tqdm as _tqdm_auto

    _tqdm.__init__ = partialmethod(_tqdm.__init__, disable=True)  # type: ignore[method-assign]
    _tqdm_auto.__init__ = partialmethod(_tqdm_auto.__init__, disable=True)  # type: ignore[method-assign]
except ImportError:
    pass

import click  # noqa: E402  (typer wraps click; we catch its exceptions)
import typer  # noqa: E402

from thematic_analysis_inc import (  # noqa: E402
    cli_utils,
    research_context_cli,
    workers,
)
from thematic_analysis_inc import db as store  # noqa: E402, N812


log = logging.getLogger("ta")


# Rich help panels (groups) ---------------------------------------------------

PANEL_SETUP = "Setup"
PANEL_DOCUMENTS = "Documents"
PANEL_S1_CODERS = "Stage 1 — coder management"
PANEL_S1_PIPELINE = "Stage 1 — codebook pipeline"
PANEL_S1_STATUS = "Stage 1 — status & codebook inspection"
PANEL_S2_THEMES = "Stage 2 — themes"
PANEL_DEBUG = "Debugging"


# Subcommands that operate on an existing DB. Excludes `init` (which is
# allowed to create the DB) and `segment` (which doesn't touch the DB).
_REQUIRES_EXISTING_DB = {
    "add-coder",
    "rm-coder",
    "list-coders",
    "add-document",
    "list-documents",
    "code",
    "update-codebook",
    "status",
    "list-codebooks",
    "show-codebook",
    "export-codebook",
    "create-theme-job",
    "create-themes",
    "list-themes",
    "show-theme",
    "set-research-context",
    "show-research-context",
    "clear-research-context",
    "test-code",
    "test-aggregate",
    "test-review",
    "test-theme",
    "batch",
}


def _make_progress(label: str):
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )

    return Progress(
        TextColumn(label),
        BarColumn(),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("elapsed"),
        TimeElapsedColumn(),
        TextColumn("eta"),
        TimeRemainingColumn(),
        console=Console(stderr=True),
        transient=False,
    )


# Handler implementations -----------------------------------------------------
#
# These take a `SimpleNamespace` of arguments and return an int exit code.
# Each Typer command below builds the namespace from its parameters and
# delegates here, so the underlying logic stays argparse-compatible and
# small adjustments to flags don't need wholesale rewrites.


def _cmd_init(args: SimpleNamespace) -> int:
    store.init_db(args.db)
    latest = store.latest_codebook()
    assert latest is not None
    print(f"initialized {args.db} (codebook v{latest.version}, 0 codes)")
    return 0


def _parse_coder_id(ref: str) -> int | None:
    """Parse an integer coder_id from a CLI argument."""
    try:
        cid = int(ref)
    except (TypeError, ValueError):
        return None
    coder = store.get_coder(cid)
    return coder.coder_id if coder else None


def _cmd_add_coder(args: SimpleNamespace) -> int:
    store.connect(args.db)
    coder = store.add_coder(args.identity)
    print(
        f"added coder id={coder.coder_id} (identity: {args.identity!r})"
    )
    return 0


def _cmd_rm_coder(args: SimpleNamespace) -> int:
    store.connect(args.db)
    cid = _parse_coder_id(args.coder_id)
    if cid is None:
        print(f"no coder with id '{args.coder_id}'", file=sys.stderr)
        return 1
    coder = store.get_coder(cid)
    if coder is None:
        print(f"no coder with id '{args.coder_id}'", file=sys.stderr)
        return 1
    try:
        removed, runs_deleted = store.cascades.delete_coder_cascade(
            coder, force=args.force
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        print("hint: pass --force to also drop their coding queue rows", file=sys.stderr)
        return 1
    if removed:
        suffix = (
            f" (also dropped {runs_deleted} queue row(s))"
            if runs_deleted else ""
        )
        print(f"removed coder id={cid}{suffix}")
        return 0
    print(f"no coder with id '{args.coder_id}'", file=sys.stderr)
    return 1


def _cmd_list_coders(args: SimpleNamespace) -> int:
    store.connect(args.db)
    coders = store.list_coders()
    if not coders:
        print("(no coders)")
        return 0
    for c in coders:
        print(f"{c.coder_id}\t{c.identity}")
    return 0


def _cmd_list_documents(args: SimpleNamespace) -> int:
    store.connect(args.db)
    docs = store.list_documents()
    if not docs:
        print("(no documents)")
        return 0
    for doc in docs:
        print(
            f"{doc.document_id}\t{doc.filename}\t{len(doc.segments)}"
        )
    return 0


def _cmd_add_document(args: SimpleNamespace) -> int:
    from thematic_analysis.loaders import load_document  # lazy

    paths = [Path(p) for p in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"file not found: {p}", file=sys.stderr)
        return 1
    dirs = [p for p in paths if p.is_dir()]
    if dirs:
        for p in dirs:
            print(f"expected a file, got a directory: {p}", file=sys.stderr)
        return 1

    store.connect(args.db)
    total_files = total_inserted = total_skipped = total_errors = 0
    with _make_progress("[add-document]") as prog:
        task = prog.add_task("", total=len(paths))
        for path in paths:
            existing_doc = store.find_document_by_filename(path.name)
            if existing_doc is not None:
                total_skipped += 1
                print(
                    f"[add-document] {path.name}: already exists "
                    f"(doc_id={existing_doc.document_id}, skipped)"
                )
                prog.advance(task)
                continue

            try:
                doc = load_document(path)
            except Exception as e:  # noqa: BLE001
                log.error("[add-document] %s: failed to load (%s)", path.name, e)
                total_errors += 1
                prog.advance(task)
                continue

            rows: list[tuple[str | None, str]]
            try:
                if args.segmentation == "llm":
                    from thematic_analysis_inc.segmenter_llm import segment_by_llm

                    titled = segment_by_llm(
                        doc.text,
                        doc_id=path.stem,
                        model=args.model,
                        min_words=args.min_words,
                    )
                    rows = [(s.title, s.text) for s in titled]
                else:
                    segments = doc.segment(
                        method=args.segmentation,
                        min_words=args.min_words,
                        max_words=args.max_words,
                    )
                    rows = [(None, s.text) for s in segments]
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[add-document] %s: segmentation failed (%s: %s)",
                    path.name,
                    type(e).__name__,
                    e,
                )
                total_errors += 1
                prog.advance(task)
                continue

            if not rows:
                print(
                    f"[add-document] {path.name}: 0 segments (skipped)",
                    file=sys.stderr,
                )
                prog.advance(task)
                continue

            try:
                new_doc = store.add_document(path.name)
                enqueue_rows = [
                    (title, txt, 0, 0, i)
                    for i, (title, txt) in enumerate(rows)
                ]
                inserted_segments = store.enqueue_segments(
                    new_doc, enqueue_rows
                )
            except Exception as e:  # noqa: BLE001
                log.error(
                    "[add-document] %s: store failure (%s: %s)",
                    path.name,
                    type(e).__name__,
                    e,
                )
                total_errors += 1
                prog.advance(task)
                continue

            total_files += 1
            total_inserted += len(inserted_segments)
            print(
                f"[add-document] {path.name}: doc_id={new_doc.document_id} "
                f"segments={len(rows)} "
                f"inserted={len(inserted_segments)}"
            )
            print(f"created_document_id\t{new_doc.document_id}")
            if args.enqueue:
                n_enqueued = store.coding.enqueue_document(new_doc.document_id)
                print(
                    f"[add-document] {path.name}: enqueued={n_enqueued} "
                    f"assignment(s) for coding"
                )
            prog.advance(task)
    summary = (
        f"done: {total_files} file(s), inserted={total_inserted} "
        f"skipped={total_skipped}"
    )
    if total_errors:
        summary += f" errors={total_errors}"
    print(summary)
    return 1 if total_errors and total_files == 0 else 0


def _format_codes(codes) -> str:
    """Format a ``list[Code]`` (each with attached supporting_quotes) for
    the verbose trace."""
    if codes is None:
        return "  (none)"
    items = list(codes)
    if not items:
        return "  (no codes — out of scope / nothing to code)"
    lines: list[str] = []
    for i, c in enumerate(items, 1):
        code_label = getattr(c, "code", str(c))
        description = getattr(c, "description", "") or ""
        header = f"  {i}. {code_label}"
        if description:
            header += f" — {description}"
        lines.append(header)
        quotes = getattr(c, "supporting_quotes", None) or []
        for q in quotes:
            qtext = getattr(q, "text", str(q))
            lines.append(f'     - "{qtext}"')
    return "\n".join(lines)


def _print_prompt(label: str, prompt: str) -> None:
    print(f"{label}:")
    for line in (prompt or "").splitlines() or [""]:
        print(f"  {line}")


def _redact_segment_in_prompt(prompt: str | None, segment_text: str) -> str:
    if not prompt:
        return ""
    if not segment_text:
        return prompt
    return prompt.replace(segment_text, "[segment text omitted; see Segment above]")


def _print_trace(segment_id: str, trace: dict) -> None:
    bar = "=" * 72
    sub = "-" * 72
    text = trace.get("segment_text") or ""
    snippet = text.strip().replace("\n", " ")
    if len(snippet) > 400:
        snippet = snippet[:397] + "..."
    print()
    print(bar)
    print(f"VERBOSE TRACE  segment={segment_id}")
    print(bar)
    print("Segment:")
    print(f"  {snippet}")
    coder_system_prompt = _redact_segment_in_prompt(
        trace.get("coder_system_prompt"), text
    )
    coder_user_prompt = _redact_segment_in_prompt(
        trace.get("coder_user_prompt"), text
    )
    if coder_system_prompt is not None:
        print(sub)
        _print_prompt("Coder system prompt", coder_system_prompt)
    if coder_user_prompt is not None:
        print(sub)
        _print_prompt("Coder user prompt", coder_user_prompt)
    first_response = trace.get("first_response")
    if first_response is not None:
        print(sub)
        _print_prompt("First-pass raw response", first_response)
    first_retry = trace.get("first_quote_retry")
    if first_retry:
        print(sub)
        _print_prompt(
            "Quote-verification retry follow-up",
            first_retry.get("followup_prompt") or "",
        )
        print(sub)
        _print_prompt(
            "Second coder response after retry",
            first_retry.get("retry_response") or "",
        )
    print(sub)
    print("First-pass codes (coder):")
    print(_format_codes(trace.get("first")))
    print(sub)
    critique = trace.get("critique")
    if critique is None:
        print("Critique: (skipped — first pass produced no codes)")
    else:
        critic_system_prompt = _redact_segment_in_prompt(
            trace.get("critic_system_prompt"), text
        )
        critic_user_prompt = _redact_segment_in_prompt(
            trace.get("critic_user_prompt"), text
        )
        if critic_system_prompt is not None:
            _print_prompt("Critic system prompt", critic_system_prompt)
            print(sub)
        if critic_user_prompt is not None:
            _print_prompt("Critic user prompt", critic_user_prompt)
            print(sub)
        print("Critique (challenger):")
        for line in critique.strip().splitlines() or [""]:
            print(f"  {line}")
        refinement_user_prompt = _redact_segment_in_prompt(
            trace.get("refinement_user_prompt"), text
        )
        if refinement_user_prompt is not None:
            print(sub)
            _print_prompt("Refinement user prompt", refinement_user_prompt)
    refined_response = trace.get("refined_response")
    if refined_response is not None:
        print(sub)
        _print_prompt("Refined raw response", refined_response)
    refined_retry = trace.get("refined_quote_retry")
    if refined_retry:
        print(sub)
        _print_prompt(
            "Refined quote-verification retry follow-up",
            refined_retry.get("followup_prompt") or "",
        )
        print(sub)
        _print_prompt(
            "Second refined response after retry",
            refined_retry.get("retry_response") or "",
        )
    print(sub)
    print("Refined codes (coder after critique):")
    print(_format_codes(trace.get("refined") or trace.get("first")))
    print(bar)
    print()


def _resolve_workers(value: str | int) -> int:
    if isinstance(value, int):
        return max(1, value)
    if value != "auto":
        try:
            return max(1, int(value))
        except ValueError:
            raise SystemExit(f"--workers must be an integer or 'auto', got {value!r}")
    model = (os.environ.get("LLM_MODEL") or "").lower()
    if "claude" in model or "anthropic" in model or "sonnet" in model or "opus" in model or "haiku" in model:
        return 8
    return 4


def _cmd_enqueue(args: SimpleNamespace) -> int:
    store.connect(args.db)
    coder_ids: list[int] | None = None
    if args.coders:
        resolved: list[int] = []
        for raw in args.coders:
            cid = _parse_coder_id(raw)
            if cid is None:
                print(f"unknown coder_id: {raw}", file=sys.stderr)
                return 1
            resolved.append(cid)
        coder_ids = resolved

    if args.document is not None and args.segment is not None:
        print(
            "specify either --document or --segment, not both",
            file=sys.stderr,
        )
        return 1
    if args.document is None and args.segment is None:
        print("specify --document or --segment", file=sys.stderr)
        return 1

    if args.document is not None:
        n = store.coding.enqueue_document(args.document, coder_ids=coder_ids)
        print(f"[enqueue] document={args.document} enqueued={n}")
    else:
        n = store.coding.enqueue_segment(args.segment, coder_ids=coder_ids)
        print(f"[enqueue] segment={args.segment} enqueued={n}")
    return 0


def _cmd_code(args: SimpleNamespace) -> int:
    store.connect(args.db)
    coders = store.list_coders()
    if not coders:
        print("(no coders)")
        return 0

    if args.recode:
        n_total = sum(
            store.coding.reset_all_assignments(c) for c in coders
        )
        if n_total:
            print(f"[code] cleared {n_total} existing queue row(s) for recoding")
    elif args.retry_failed:
        n_total = sum(
            store.coding.reset_failed_assignments(c) for c in coders
        )
        if n_total:
            print(f"[code] cleared {n_total} failed queue row(s) for retry")

    n_workers = _resolve_workers(args.workers)
    total_todo = store.coding.pending_count()
    bar_total = min(total_todo, args.limit) if args.limit else total_todo

    print(
        f"[code] todo={total_todo} workers={n_workers}"
        + (f" limit={args.limit}" if args.limit else "")
    )
    if total_todo == 0:
        return 0

    with _make_progress("[code]") as prog:
        task = prog.add_task("", total=bar_total)

        def on_event(res: dict, c: dict) -> None:
            n = c["done"] + c["failed"]
            if res["ok"]:
                if args.trace and res.get("trace") is not None:
                    _print_trace(res["segment_id"], res["trace"])
                skipped = " skipped" if res.get("skipped") else ""
                print(
                    f"[code] segment={res['segment_id']} coder={res['coder_id']} "
                    f"codes={res['n_codes']} v={res['version']}{skipped} "
                    f"({n}/{bar_total} ok={c['done']} failed={c['failed']} "
                    f"{res['elapsed']:.1f}s)"
                )
            else:
                print(
                    f"[code] segment={res['segment_id']} coder={res['coder_id']} "
                    f"FAILED v={res['version']}: {res['error']}",
                    file=sys.stderr,
                )
            prog.advance(task)

        async def _run() -> dict:
            # Each segment fires up to 3 concurrent LLM calls via the refining
            # coder; size the thread pool so workers aren't blocked queueing on it.
            from concurrent.futures import ThreadPoolExecutor
            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=max(8, n_workers * 3))
            )
            return await workers.drain_code_async(
                workers=n_workers,
                limit=args.limit,
                use_mock_embeddings=args.mock_embeddings,
                on_event=on_event,
            )

        counters = asyncio.run(_run())
    print(f"[code] done: {counters['done']} ok, {counters['failed']} failed")
    return 0


def _cmd_aggregate(args: SimpleNamespace) -> int:
    store.connect(args.db)

    if args.retry_failed:
        # In the new schema, failed aggregations don't leave persistent rows;
        # a re-run simply re-attempts any segment that has no aggregator code.
        pass

    total_todo = store.aggregation.pending_aggregation_count()
    bar_total = min(total_todo, args.limit) if args.limit else total_todo

    print(
        f"[aggregate] todo={total_todo}"
        + (f" limit={args.limit}" if args.limit else "")
    )
    if total_todo == 0:
        return 0

    with _make_progress("[aggregate]") as prog:
        task = prog.add_task("", total=bar_total)

        def on_event(res: dict, c: dict) -> None:
            n = c["done"] + c["failed"]
            if res["ok"]:
                print(
                    f"[aggregate] segment={res['segment_id']} in={res['n_in']} "
                    f"out={res['n_out']} new={res['n_new']} "
                    f"({n}/{bar_total} ok={c['done']} failed={c['failed']} "
                    f"{res['elapsed']:.1f}s)"
                )
            else:
                print(
                    f"[aggregate] {res['segment_id']} FAILED: {res['error']}",
                    file=sys.stderr,
                )
            prog.advance(task)

        counters = workers.drain_aggregate(
            limit=args.limit,
            use_mock_embeddings=args.mock_embeddings,
            on_event=on_event,
        )
    print(
        f"[aggregate] done: {counters['done']} ok, "
        f"{counters['failed']} failed"
    )
    return 0


def _cmd_review(args: SimpleNamespace) -> int:
    store.connect(args.db)

    total_todo = store.review.pending_review_count()
    bar_total = min(total_todo, args.limit) if args.limit else total_todo

    print(
        f"[review] todo={total_todo}"
        + (f" limit={args.limit}" if args.limit else "")
    )
    if total_todo == 0:
        return 0

    with _make_progress("[review]") as prog:
        task = prog.add_task("", total=bar_total)

        def on_event(res: dict, c: dict) -> None:
            n = c["done"] + c["failed"]
            print(
                f"[review] reviewed_code={res['aggregated_code_id']} ({res['code']!r}) "
                f"decision={res['decision']} "
                f"({n}/{bar_total} ok={c['done']} failed={c['failed']} "
                f"{res['elapsed']:.1f}s)"
            )
            prog.advance(task)

        counters = workers.drain_review(
            limit=args.limit,
            use_mock_embeddings=args.mock_embeddings,
            on_event=on_event,
        )
    print(
        f"[review] done: {counters['done']} ok, "
        f"{counters['failed']} failed"
    )
    return 0


def _cmd_update_codebook(args: SimpleNamespace) -> int:
    if not getattr(args, "skip_aggregate", False):
        rc = _cmd_aggregate(args)
        if rc:
            return rc
    if not getattr(args, "skip_review", False):
        rc = _cmd_review(args)
        if rc:
            return rc
    store.connect(args.db)
    # Materialize one new Codebook revision capturing all reviewer
    # decisions written above (no-op if nothing changed).
    new_version = workers.finalize_codebook()
    if new_version is None:
        print("[update-codebook] codebook unchanged")
    else:
        print(f"[update-codebook] created codebook v{new_version}")
    return 0


def _format_eta(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN guard
        return "?"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return f"{d}d{h:02d}h{m:02d}m"
    return f"{h:d}h{m:02d}m{s:02d}s"


class _PendingCounts(SimpleNamespace):
    coding_pending: int
    aggregation_pending: int
    review_pending: int

    @property
    def any_pending(self) -> bool:
        return bool(
            self.coding_pending
            or self.aggregation_pending
            or self.review_pending
        )

    def describe(self) -> str:
        return (
            f"coding_pending={self.coding_pending}, "
            f"aggregation_pending={self.aggregation_pending}, "
            f"review_pending={self.review_pending}"
        )


def _pending_stage_counts() -> _PendingCounts:
    # Mirror what the drain workers can actually pick up: ``pending_count``
    # excludes claimed-but-not-finished rows (stale claims from a crashed
    # previous run cannot be re-claimed by ``claim_next_assignment``, so
    # they aren't resumable work — only true "queued" rows are).
    return _PendingCounts(
        coding_pending=store.coding.pending_count(),
        aggregation_pending=store.aggregation.pending_aggregation_count(),
        review_pending=store.review.pending_review_count(),
    )


def _run_stage(
    label: str,
    todo: int,
    run_drain: Callable[[Callable[[dict, dict], None]], dict],
    *,
    extras: str = "",
    on_event: Callable[[dict, dict], None] | None = None,
    crash_continuation: str,
) -> None:
    """Drive one drain inside a progress bar.

    ``run_drain`` is a thunk that, given an ``on_event`` callback, calls
    the underlying ``drain_*`` worker and returns its counters dict. We
    isolate the try/except + progress wiring here so the per-stage
    blocks in ``_cmd_batch`` stay tiny.
    """
    suffix = f" {extras}" if extras else ""
    print(f"{label} todo={todo}{suffix}")
    if todo <= 0:
        return
    with _make_progress(label) as prog:
        task = prog.add_task("", total=todo)

        def _advance(res: dict, c: dict) -> None:
            if on_event is not None:
                on_event(res, c)
            prog.advance(task)

        try:
            counters = run_drain(_advance)
        except Exception as exc:  # noqa: BLE001
            print(
                f"{label} drain crashed: {type(exc).__name__}: {exc}; "
                f"{crash_continuation}",
                file=sys.stderr,
            )
            counters = {"done": 0, "failed": 0}
    print(
        f"{label} done: {counters['done']} ok, {counters['failed']} failed"
    )


def _drain_code_stage(n_workers: int, mock_embeddings: bool) -> None:
    def _run(on_event: Callable[[dict, dict], None]) -> dict:
        async def _async() -> dict:
            from concurrent.futures import ThreadPoolExecutor

            loop = asyncio.get_running_loop()
            loop.set_default_executor(
                ThreadPoolExecutor(max_workers=max(8, n_workers * 3))
            )
            return await workers.drain_code_async(
                workers=n_workers,
                use_mock_embeddings=mock_embeddings,
                on_event=on_event,
            )

        return asyncio.run(_async())

    def _on_event(res: dict, _c: dict) -> None:
        if not res["ok"]:
            print(
                f"[batch.code] segment={res['segment_id']} "
                f"coder={res['coder_id']} FAILED: {res['error']}",
                file=sys.stderr,
            )

    _run_stage(
        "[batch.code]",
        store.coding.pending_count(),
        _run,
        extras=f"workers={n_workers}",
        on_event=_on_event,
        crash_continuation="continuing with aggregate/review",
    )


def _drain_aggregate_stage(mock_embeddings: bool) -> None:
    def _run(on_event: Callable[[dict, dict], None]) -> dict:
        return workers.drain_aggregate(
            use_mock_embeddings=mock_embeddings,
            on_event=on_event,
        )

    def _on_event(res: dict, _c: dict) -> None:
        if not res.get("ok"):
            print(
                f"[batch.aggregate] segment={res['segment_id']} "
                f"FAILED: {res.get('error')}",
                file=sys.stderr,
            )

    _run_stage(
        "[batch.aggregate]",
        store.aggregation.pending_aggregation_count(),
        _run,
        on_event=_on_event,
        crash_continuation="continuing with review",
    )


def _drain_review_stage(mock_embeddings: bool) -> None:
    def _run(on_event: Callable[[dict, dict], None]) -> dict:
        return workers.drain_review(
            use_mock_embeddings=mock_embeddings,
            on_event=on_event,
        )

    _run_stage(
        "[batch.review]",
        store.review.pending_review_count(),
        _run,
        crash_continuation="continuing to finalize",
    )


def _finalize_codebook_step(label: str) -> int | None:
    try:
        v = workers.finalize_codebook()
    except Exception as exc:  # noqa: BLE001
        print(
            f"{label} finalize crashed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    if v is None:
        print(f"{label} codebook unchanged")
    else:
        print(f"{label} created codebook v{v}")
    return v


def _enqueue_new_batch(batch_size: int) -> int:
    """Pick and enqueue up to ``batch_size`` random uncoded documents.
    Returns the number of documents enqueued (0 means: nothing left)."""
    docs = store.list_documents_without_queue_entries(batch_size)
    if not docs:
        return 0
    total_enqueued = 0
    for d in docs:
        total_enqueued += store.coding.enqueue_document(d.document_id)
    preview = [d.filename for d in docs[:3]]
    ellipsis = "..." if len(docs) > 3 else ""
    print(
        f"[batch] enqueued batch of {len(docs)} document(s), "
        f"{total_enqueued} assignment(s) ({preview}{ellipsis})"
    )
    return len(docs)


def _print_batch_start(
    batches_run: int, batch_size: int, durations: list[float]
) -> None:
    remaining_docs = store.count_documents_without_queue_entries()
    total_est = batches_run + 1 + (remaining_docs + batch_size - 1) // batch_size
    avg = sum(durations) / len(durations) if durations else 0.0
    eta = avg * (total_est - batches_run - 1) if avg else 0.0
    print(
        f"[batch] starting batch {batches_run + 1}/~{total_est} "
        f"(remaining docs after this batch: {remaining_docs}, "
        f"avg/batch: {_format_eta(avg) if avg else 'n/a'}, "
        f"eta remaining: {_format_eta(eta) if avg else 'n/a'})"
    )


def _print_batch_done(
    batches_run: int, elapsed: float, batch_size: int, durations: list[float]
) -> None:
    avg = sum(durations) / len(durations)
    remaining_docs = store.count_documents_without_queue_entries()
    remaining_batches = (remaining_docs + batch_size - 1) // batch_size
    eta = avg * remaining_batches
    print(
        f"[batch] finished batch {batches_run} in {_format_eta(elapsed)} "
        f"(avg {_format_eta(avg)}, "
        f"docs remaining: {remaining_docs}, "
        f"batches remaining: {remaining_batches}, "
        f"eta: {_format_eta(eta) if remaining_batches else '0s'})"
    )


# Allow up to this many consecutive iterations that find pending work at
# loop entry before aborting. The first one is the normal resume path on
# startup; one more covers transient flakiness in a drain; past that
# we're almost certainly looping on the same failure.
_MAX_CONSECUTIVE_PARTIAL_BATCHES = 2


def _cmd_batch(args: SimpleNamespace) -> int:
    """Run coding + codebook updates in repeated document-batches until
    no uncoded documents remain. Resumable: on startup any in-progress
    queue entries are treated as the current batch."""
    import time

    store.connect(args.db)
    if not store.list_coders():
        print(
            "[batch] no coders registered; run 'ta add-coder <identity>' first",
            file=sys.stderr,
        )
        return 1

    n_workers = _resolve_workers(args.workers)
    batch_size = max(1, int(args.batch_size))
    max_batches: int | None = args.max_batches

    durations: list[float] = []
    batches_run = 0
    consecutive_partial = 0

    while True:
        if max_batches is not None and batches_run >= max_batches:
            print(f"[batch] reached --max-batches={max_batches}, stopping")
            break

        pending = _pending_stage_counts()
        if pending.any_pending:
            consecutive_partial += 1
            if consecutive_partial > _MAX_CONSECUTIVE_PARTIAL_BATCHES:
                print(
                    f"[batch] aborting: {consecutive_partial} consecutive "
                    f"iterations found pending work at start "
                    f"({pending.describe()}). Inspect the queue / logs "
                    f"and re-run once the blocking issue is fixed.",
                    file=sys.stderr,
                )
                return 1
            print(
                f"[batch] resuming prior batch (attempt "
                f"{consecutive_partial}/{_MAX_CONSECUTIVE_PARTIAL_BATCHES}: "
                f"{pending.describe()})"
            )
        else:
            consecutive_partial = 0
            # A previous run may have crashed between the last review and
            # the codebook revision. finalize_codebook is idempotent
            # (None when membership is unchanged).
            v = _finalize_codebook_step("[batch] startup-finalize:")
            if v is not None:
                print(
                    f"[batch] finalized leftover codebook v{v} "
                    f"from prior batch"
                )
            if _enqueue_new_batch(batch_size) == 0:
                print("[batch] no more uncoded documents — done")
                break

        _print_batch_start(batches_run, batch_size, durations)
        t_batch = time.monotonic()

        _drain_code_stage(n_workers, args.mock_embeddings)
        _drain_aggregate_stage(args.mock_embeddings)
        _drain_review_stage(args.mock_embeddings)
        _finalize_codebook_step("[batch.update-codebook]")

        elapsed = time.monotonic() - t_batch
        durations.append(elapsed)
        batches_run += 1
        _print_batch_done(batches_run, elapsed, batch_size, durations)

    return 0

    return 0


def _cmd_status(args: SimpleNamespace) -> int:
    store.connect(args.db)
    print(store.status_counts().format())
    return 0


def _cmd_export_codebook(args: SimpleNamespace) -> int:
    store.connect(args.db)
    if args.version is None:
        cv = store.latest_codebook()
    else:
        cv = store.get_codebook(args.version)
    if cv is None:
        print("no codebook version found", file=sys.stderr)
        return 1
    snapshot_json = store.codebook_to_json_for_version(cv.version)
    if args.output == "-" or args.output is None:
        sys.stdout.write(snapshot_json)
        if not snapshot_json.endswith("\n"):
            sys.stdout.write("\n")
    else:
        Path(args.output).write_text(snapshot_json, encoding="utf-8")
        codes = len(json.loads(snapshot_json).get("codes", []))
        print(f"wrote codebook v{cv.version} ({codes} codes) to {args.output}")
    return 0


def _cmd_list_codebooks(args: SimpleNamespace) -> int:
    store.connect(args.db)
    versions = store.list_codebooks()
    if not versions:
        print("(no codebook versions)")
        return 0
    print(f"{'version':>7}  {'parent':>6}  {'codes':>5}  created_at")
    for cv in versions:
        snap = json.loads(store.codebook_to_json_for_version(cv.version))
        n_codes = len(snap.get("codes", []))
        parent = "-" if cv.parent_version is None else str(cv.parent_version)
        print(
            f"{cv.version:>7}  {parent:>6}  {n_codes:>5}  {cv.created_at}"
        )
    return 0


def _cmd_show_codebook(args: SimpleNamespace) -> int:
    store.connect(args.db)
    if args.version is None:
        cv = store.latest_codebook()
    else:
        cv = store.get_codebook(args.version)
    if cv is None:
        msg = (
            f"no codebook version {args.version}"
            if args.version is not None
            else "no codebook versions found"
        )
        print(msg, file=sys.stderr)
        return 1
    snapshot_json = store.codebook_to_json_for_version(cv.version)
    data = json.loads(snapshot_json)
    codes = data.get("codes", [])
    parent = "-" if cv.parent_version is None else str(cv.parent_version)
    print(
        f"Codebook v{cv.version} (parent={parent}, "
        f"created_at={cv.created_at})"
    )
    print(f"{len(codes)} code(s)")
    print()
    if not codes:
        print("(empty)")
        return 0
    for i, entry in enumerate(codes, 1):
        code = entry.get("code", "")
        quotes = entry.get("quotes", [])
        print(f"{i}. {code}")
        if quotes:
            print(f"   quotes ({len(quotes)}):")
            for q in quotes:
                qid = q.get("quote_id", "")
                text = q.get("text", "").replace("\n", " ").strip()
                if len(text) > 200:
                    text = text[:197] + "..."
                print(f"     - [{qid}] {text}")
        print()
    return 0


def _cmd_segment(args: SimpleNamespace) -> int:
    from thematic_analysis.loaders import load_document  # lazy

    path = Path(args.file)
    if not path.exists():
        print(f"file not found: {path}", file=sys.stderr)
        return 1

    doc = load_document(path)

    if args.method == "llm":
        from thematic_analysis_inc.segmenter_llm import segment_by_llm

        titled = segment_by_llm(
            doc.text,
            doc_id=path.stem,
            model=args.model,
            min_words=args.min_words,
        )
        rows: list[tuple[str, str, str]] = [
            (s.segment_id, s.text, s.title) for s in titled
        ]
    else:
        segments = doc.segment(
            method=args.method,
            min_words=args.min_words,
            max_words=args.max_words,
        )
        rows = [(s.segment_id, s.text, "") for s in segments]

    if not rows:
        print("(no segments produced)", file=sys.stderr)
        return 1

    for i, (seg_id, text, title) in enumerate(rows, 1):
        words = len(text.split())
        suffix = f"  — {title}" if title else ""
        print(f"── [{i}/{len(rows)}] {seg_id}  ({words} words){suffix}")
        print(text)
        print()

    print(f"── {len(rows)} segment(s) from {path.name}", file=sys.stderr)
    return 0


def _print_coder_interaction(trace: dict) -> None:
    """Dump every LLM message in the coder's chat session.

    Used by ``test-code`` so the human debugger sees the full
    interaction: initial system + user prompts, the first response, any
    quote-verification retry follow-up + retry response, and (on
    success) the critic and refinement turns. Trace values may be
    ``None`` when a phase didn't run.
    """
    sep = "─" * 72
    print(sep)
    print("Coder system prompt")
    print(sep)
    print(trace.get("coder_system_prompt") or "(missing)")
    print(sep)
    print("Coder user prompt")
    print(sep)
    print(trace.get("coder_user_prompt") or "(missing)")
    print(sep)
    print("First coder response (assistant)")
    print(sep)
    first_response = trace.get("first_response")
    print(first_response if first_response is not None
          else "(no response — LLM call failed before returning)")

    first_retry = trace.get("first_quote_retry")
    if first_retry:
        print(sep)
        print("Quote-verification retry follow-up (user)")
        print(sep)
        print(first_retry.get("followup_prompt") or "(missing)")
        print(sep)
        print("Second coder response after retry (assistant)")
        print(sep)
        print(first_retry.get("retry_response") or "(missing)")

    critique = trace.get("critique")
    if critique is not None:
        print(sep)
        print("Critic system prompt")
        print(sep)
        print(trace.get("critic_system_prompt") or "(missing)")
        print(sep)
        print("Critic user prompt")
        print(sep)
        print(trace.get("critic_user_prompt") or "(missing)")
        print(sep)
        print("Critic response (assistant)")
        print(sep)
        print(critique)
        print(sep)
        print("Refinement user prompt")
        print(sep)
        print(trace.get("refinement_user_prompt") or "(missing)")
        print(sep)
        print("Refined coder response (assistant)")
        print(sep)
        refined_response = trace.get("refined_response")
        print(refined_response if refined_response is not None
              else "(no response)")

        refined_retry = trace.get("refined_quote_retry")
        if refined_retry:
            print(sep)
            print("Refined quote-verification retry follow-up (user)")
            print(sep)
            print(refined_retry.get("followup_prompt") or "(missing)")
            print(sep)
            print("Second refined response after retry (assistant)")
            print(sep)
            print(refined_retry.get("retry_response") or "(missing)")
    print(sep)


def _cmd_test_code(args: SimpleNamespace) -> int:
    store.connect(args.db)
    cid = _parse_coder_id(args.coder_id)
    if cid is None:
        print(f"unknown coder_id: {args.coder_id}", file=sys.stderr)
        return 1

    try:
        res = workers.test_code_segment(
            args.segment_id,
            cid,
            use_mock_embeddings=args.mock_embeddings,
        )
    except Exception as e:
        trace = getattr(e, "coder_trace", None)
        if trace is not None:
            _print_coder_interaction(trace)
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    trace = res.get("trace")
    if trace is not None:
        _print_trace(str(res["segment_id"]), trace)
    else:
        print("Codes:")
        print(_format_codes(res.get("codes")))
        print()

    print(
        f"[test-code] segment={res['segment_id']} coder={res['coder_id']} "
        f"codes={res['n_codes']} v={res['version']} ({res['elapsed']:.1f}s)"
    )
    return 0


def _cmd_test_aggregate(args: SimpleNamespace) -> int:
    store.connect(args.db)
    try:
        res = workers.test_aggregate_segment(
            args.segment_id,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    sep = "─" * 72
    agent = res["agent"]
    print(sep)
    print("System prompt")
    print(sep)
    print(agent.last_system_prompt)
    print()

    print(sep)
    print("User prompt")
    print(sep)
    print(agent.last_user_prompt or "(no LLM call — empty input)")
    print()

    if res["llm_error"]:
        print(sep)
        print(f"LLM error: {res['llm_error']}", file=sys.stderr)
        print(sep)
        return 1

    print(sep)
    print(
        f"Raw LLM response  ({agent.last_elapsed:.1f}s, "
        f"{agent.last_attempts} attempt(s))"
    )
    print(sep)
    print(agent.last_raw_response or "(empty)")
    print()

    print(sep)
    print("Aggregator output — Code objects the worker would write")
    print(sep)
    result = res["result"] or []
    if not result:
        print("(no codes — empty input or parse failure)")
    for c in result:
        kind = "retained" if c.code_id is not None else "new"
        src_labels = [e.source_code.code for e in (c.derivation_sources or [])]
        print(f"  - [{kind}] {c.code!r} (obj_id={id(c)})")
        if c.rationale:
            print(f"      rationale: {c.rationale}")
        if src_labels:
            print(
                f"      source codes: {src_labels} "
                f"(ids={[e.source_code.code_id for e in c.derivation_sources or []]})"
            )
        quotes = list(c.supporting_quotes or [])
        print(f"      quotes ({len(quotes)}):")
        for q in quotes:
            qts = (q.text or "").replace("\n", " ").strip()
            if len(qts) > 200:
                qts = qts[:197] + "..."
            print(f"          [quote_id={q.quote_id} obj_id={id(q)}] \"{qts}\"")

    print()
    print(f"[test-aggregate] segment={res['segment_id']} no DB writes")
    return 0


def _cmd_test_review(args: SimpleNamespace) -> int:
    store.connect(args.db)
    try:
        res = workers.test_review_aggregated_code(args.code_id)
    except (ValueError, RuntimeError) as e:
        print(str(e), file=sys.stderr)
        return 1

    sep = "─" * 72
    agent = res["agent"]
    result = res["result"]
    target = res["target"]

    print(sep)
    print(
        f"Input  code_id={res['code_id']} segment_id={res['segment_id']} "
        f"codebook=v{res['codebook_version']}"
    )
    print(sep)
    print(f"  code: {res['code']!r}")
    target_quotes = list(target.supporting_quotes or [])
    print(f"  quotes ({len(target_quotes)}):")
    for q in target_quotes:
        qt = (q.text or "").replace("\n", " ").strip()
        if len(qt) > 200:
            qt = qt[:197] + "..."
        print(f"      [quote_id={q.quote_id}] \"{qt}\"")
    print()

    print(sep)
    print(
        f"Similar codes from codebook (top-k={agent.reviewer_config.top_k_similar})"
    )
    print(sep)
    if not agent.last_similar:
        print("(none — codebook empty or no embeddings)")
    for entry, score in agent.last_similar:
        marker = ""
        if score >= agent.reviewer_config.merge_threshold:
            marker = "  ← auto-merge"
        elif score >= agent.reviewer_config.similarity_threshold:
            marker = "  ← above similarity threshold"
        print(f"  - {entry.code!r}  similarity={score:.3f}{marker}")
        for q in (entry.supporting_quotes or [])[:3]:
            qt = (q.text or "").replace("\n", " ").strip()
            if len(qt) > 160:
                qt = qt[:157] + "..."
            print(f"      [quote_id={q.quote_id}] \"{qt}\"")
    print()

    if agent.last_shortcut == "auto_merge":
        print(sep)
        print("Shortcut: top similarity ≥ merge_threshold → auto-merge, no LLM call")
        print(sep)
        print()
    elif agent.last_shortcut == "no_similar":
        print(sep)
        print(
            "Shortcut: nothing above similarity_threshold → add_new, no LLM call"
        )
        print(sep)
        print()
    else:
        print(sep)
        print("System prompt")
        print(sep)
        print(agent.last_system_prompt)
        print()

        print(sep)
        print("User prompt (JSON payload)")
        print(sep)
        print(agent.last_user_prompt or "(empty)")
        print()

        if res["llm_error"]:
            print(sep)
            print(f"LLM error: {res['llm_error']}", file=sys.stderr)
            print(sep)
            return 1

        print(sep)
        print(f"Raw LLM response  ({agent.last_elapsed:.1f}s)")
        print(sep)
        print(agent.last_raw_response or "(empty)")
        print()

    print(sep)
    print("Reviewer decision (would-be new Code)")
    print(sep)
    if result is None:
        print("(no result — see LLM error above)")
    else:
        edges = list(result.derivation_sources or [])
        decision = edges[0].decision if edges else "?"
        print(f"  decision:    {decision}")
        print(f"  new label:   {result.code!r}")
        print(f"  rationale:   {result.rationale}")
        sources = [
            (e.source_code.code_id, e.source_code.code, e.source_code.coder_id)
            for e in edges
            if e.source_code is not None
        ]
        print(f"  provenance ({len(sources)} edges):")
        for cid, label, coder in sources:
            kind = {0: "aggregator", -1: "previous reviewer"}.get(
                coder, f"coder={coder}"
            )
            print(f"    ← code_id={cid} [{kind}] {label!r}")
        print(f"  quotes ({len(result.supporting_quotes or [])}):")
        for q in (result.supporting_quotes or []):
            qt = (q.text or "").replace("\n", " ").strip()
            if len(qt) > 160:
                qt = qt[:157] + "..."
            print(f"      [quote_id={q.quote_id}] \"{qt}\"")

    print()
    print(f"[test-review] code_id={res['code_id']} no DB writes")
    return 0


# Stage 2 — theme coding handlers --------------------------------------------


def _resolve_codebook_version(version_arg: int | None) -> int | None:
    if version_arg is not None:
        return version_arg
    cv = store.latest_codebook()
    if cv is None:
        print(
            "no codebook version found; run 'ta --db ... init' first",
            file=sys.stderr,
        )
        return None
    return cv.version


def _cmd_create_theme_job(args: SimpleNamespace) -> int:
    store.connect(args.db)
    version = _resolve_codebook_version(args.codebook_version)
    if version is None:
        return 1

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not (prompt or "").strip():
        print(
            "error: provide --prompt or --prompt-file with the job "
            "instructions (research question, persona, etc.)",
            file=sys.stderr,
        )
        return 2

    job = store.add_theme_coding_job(
        codebook_used_id=version, prompt=prompt,
    )
    print(
        f"[create-theme-job] job id={job.id} codebook=v{version} "
        f"prompt={len(prompt)} chars (not run — use 'create-themes' to run "
        f"pending jobs)"
    )
    return 0


def _cmd_create_themes(args: SimpleNamespace) -> int:
    store.connect(args.db)
    pending = store.list_unrun_theme_coding_jobs()
    if not pending:
        print("[create-themes] no pending theme-coding jobs")
        return 0
    print(f"[create-themes] running {len(pending)} pending job(s)")

    n_themes = 0
    n_empty = 0
    for job in pending:
        try:
            themes = workers.run_theme_coding_job(job)
        except Exception as e:
            print(
                f"[create-themes] job id={job.id} FAILED: {e}",
                file=sys.stderr,
            )
            return 1
        if themes:
            n_themes += len(themes)
            print(
                f"[create-themes] job id={job.id} codebook=v{job.codebook_used_id} "
                f"produced {len(themes)} theme(s)"
            )
            for t in themes:
                print(
                    f"  theme_id={t.theme_id} codes={len(t.codes)} "
                    f"quotes={len(t.supporting_quotes)} {t.title!r}"
                )
        else:
            n_empty += 1
            print(
                f"[create-themes] job id={job.id} codebook=v{job.codebook_used_id} "
                f"produced no themes (sentinel recorded)"
            )
    print(
        f"[create-themes] done: {len(pending)} job(s), "
        f"{n_themes} theme(s), {n_empty} empty"
    )
    return 0


def _cmd_list_themes(args: SimpleNamespace) -> int:
    store.connect(args.db)
    if args.job is not None:
        themes = store.list_themes_for_job(args.job)
        scope = f"job={args.job}"
    else:
        themes = store.list_current_themes()
        scope = "current"
    if not themes:
        print(f"(no themes — {scope})")
        return 0
    print(f"{len(themes)} theme(s) — {scope}")
    for t in themes:
        flag = " [deleted]" if t.deleted else ""
        print(
            f"  theme_id={t.theme_id} src={t.source} "
            f"codes={len(t.codes)} quotes={len(t.supporting_quotes)}"
            f"{flag} {t.title!r}"
        )
    return 0


def _cmd_show_theme(args: SimpleNamespace) -> int:
    store.connect(args.db)
    t = store.get_theme(args.theme_id)
    if t is None:
        print(f"no theme with id {args.theme_id}", file=sys.stderr)
        return 1
    print(f"theme_id={t.theme_id}  {t.title}")
    print(
        f"  source={t.source}  job={t.theme_coding_job_id}  "
        f"codebook=v{t.codebook_used_id}  deleted={t.deleted}"
    )
    if t.description:
        print(f"  description: {t.description}")
    if t.rationale:
        print(f"  rationale:   {t.rationale}")
    print(f"  codes ({len(t.codes)}):")
    for c in t.codes:
        print(f"    [code_id={c.code_id}] {c.code}")
    print(f"  supporting quotes ({len(t.supporting_quotes)}):")
    for q in t.supporting_quotes:
        text = (q.text or "").replace("\n", " ").strip()
        if len(text) > 160:
            text = text[:157] + "..."
        print(f"    [quote_id={q.quote_id}] \"{text}\"")
    return 0


def _cmd_test_theme(args: SimpleNamespace) -> int:
    store.connect(args.db)
    version = _resolve_codebook_version(args.codebook_version)
    if version is None:
        return 1

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    prompt = prompt or ""

    try:
        res = workers.test_theme_code(version, prompt)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1

    themes = res["themes"]
    turns = res.get("turns") or []

    # Debug dump: every prompt + response in the five-step flow. Turns
    # 1–3 and 5 share the same chat (one system message at the front);
    # turn 4 (critic) is a separate session with its own system prompt.
    # Collapse any system prompt that was already shown earlier so the
    # trace doesn't reprint the long theme-coder prompt for the final
    # turn after the critic.
    seen_systems: dict[str, int] = {}
    for i, turn in enumerate(turns, start=1):
        print(f"===== turn {i}: {turn.label} ({turn.elapsed:.2f}s) =====")
        print("----- system prompt -----")
        if turn.system_prompt in seen_systems:
            print(f"(same as turn {seen_systems[turn.system_prompt]})")
        else:
            print(turn.system_prompt)
            seen_systems[turn.system_prompt] = i
        print("----- user prompt -----")
        print(turn.user_prompt)
        print("----- response -----")
        print(turn.response)
        print()

    titles = ", ".join(repr(t.title) for t in themes) or "(none)"
    print(
        f"[test-theme] codebook=v{res['codebook_version']} "
        f"themes={len(themes)} [{titles}] "
        f"turns={len(turns)} ({res['elapsed']:.1f}s) no DB writes"
    )
    return 0


# Typer wiring ---------------------------------------------------------------


app = typer.Typer(
    name="ta",
    help=__doc__,
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _run(ctx: typer.Context, handler, **kwargs) -> None:
    """Validate the DB requirement, then dispatch to the legacy handler."""
    sub = ctx.info_name
    db = ctx.obj.get("db") if ctx.obj else None
    needs_db = sub in _REQUIRES_EXISTING_DB
    if (sub == "init" or needs_db) and not db:
        typer.echo(f"--db is required for subcommand '{sub}'", err=True)
        raise typer.Exit(2)
    if needs_db and db:
        rc = cli_utils.require_db(db)
        if rc:
            raise typer.Exit(rc)
    args = SimpleNamespace(db=db, **kwargs)
    rc = handler(args)
    if rc:
        raise typer.Exit(rc)


@app.callback()
def _root(
    ctx: typer.Context,
    db: Annotated[
        str | None,
        typer.Option(
            "--db",
            help="path to the SQLite database (required by every subcommand "
            "except `segment`)",
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("-v", "--verbose", help="verbose logging (INFO level)"),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            help="debug logging and full tracebacks on unexpected errors",
        ),
    ] = False,
) -> None:
    cli_utils.setup_logging(verbose=verbose, debug=debug)
    ctx.ensure_object(dict)
    ctx.obj["db"] = db
    ctx.obj["debug"] = debug


# Setup ----------------------------------------------------------------------


@app.command(
    name="init",
    rich_help_panel=PANEL_SETUP,
    help=(
        "create an empty database — schema, an empty codebook (v1), "
        "and no research context"
    ),
)
def _cli_init(ctx: typer.Context) -> None:
    _run(ctx, _cmd_init)


# Documents ------------------------------------------------------------------


@app.command(
    name="add-document",
    rich_help_panel=PANEL_DOCUMENTS,
    help="load .md/.txt/.pdf files, segment, and add",
)
def _cli_add_document(
    ctx: typer.Context,
    files: Annotated[list[str], typer.Argument(help="files to ingest")],
    segmentation: Annotated[
        str,
        typer.Option(
            "--segmentation",
            click_type=click.Choice(["llm", "paragraph", "sentence", "fixed"]),
        ),
    ] = "llm",
    min_words: Annotated[
        int,
        typer.Option(
            "--min-words",
            help=(
                "minimum words per segment "
                "(drop for paragraph/sentence; merge for llm)"
            ),
        ),
    ] = 50,
    max_words: Annotated[int, typer.Option("--max-words")] = 500,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "litellm model id for --segmentation llm "
                "(defaults to LLM_MODEL_SEGMENTER env or gemini/gemini-2.5-flash-lite)"
            ),
        ),
    ] = None,
    batch: Annotated[int | None, typer.Option("--batch")] = None,
    enqueue: Annotated[
        bool,
        typer.Option(
            "--enqueue",
            help="enqueue all inserted segments for coding by every coder",
        ),
    ] = False,
) -> None:
    _run(
        ctx,
        _cmd_add_document,
        files=files,
        segmentation=segmentation,
        min_words=min_words,
        max_words=max_words,
        model=model,
        batch=batch,
        enqueue=enqueue,
    )


@app.command(
    name="list-documents",
    rich_help_panel=PANEL_DOCUMENTS,
    help="list document ids, filenames, and segment counts",
)
def _cli_list_documents(ctx: typer.Context) -> None:
    _run(ctx, _cmd_list_documents)


# Stage 1 coder management ---------------------------------------------------


@app.command(
    name="add-coder",
    rich_help_panel=PANEL_S1_CODERS,
    help="register a coder",
)
def _cli_add_coder(
    ctx: typer.Context,
    identity: Annotated[
        str,
        typer.Argument(help="free-text identity/persona shown to the coder agent"),
    ],
) -> None:
    _run(ctx, _cmd_add_coder, identity=identity)


@app.command(
    name="rm-coder",
    rich_help_panel=PANEL_S1_CODERS,
    help="remove a coder (use --force to also drop their coder_runs)",
)
def _cli_rm_coder(
    ctx: typer.Context,
    coder_id: str,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="also delete this coder's coder_runs and coder_codes",
        ),
    ] = False,
) -> None:
    _run(ctx, _cmd_rm_coder, coder_id=coder_id, force=force)


@app.command(
    name="list-coders",
    rich_help_panel=PANEL_S1_CODERS,
    help="list registered coders",
)
def _cli_list_coders(ctx: typer.Context) -> None:
    _run(ctx, _cmd_list_coders)


# Stage 1 codebook pipeline --------------------------------------------------


@app.command(
    name="enqueue",
    rich_help_panel=PANEL_S1_PIPELINE,
    help="enqueue a document or segment for coding (defaults to all coders)",
)
def _cli_enqueue(
    ctx: typer.Context,
    document: Annotated[
        int | None,
        typer.Option(
            "--document",
            "-d",
            help="document_id whose segments should be enqueued",
        ),
    ] = None,
    segment: Annotated[
        int | None,
        typer.Option(
            "--segment",
            "-s",
            help="segment_id to enqueue (single segment form)",
        ),
    ] = None,
    coder: Annotated[
        list[str] | None,
        typer.Option(
            "--coder",
            "-c",
            help=(
                "coder_id(s) to enqueue for; pass multiple times. "
                "Defaults to every registered coder."
            ),
        ),
    ] = None,
) -> None:
    _run(
        ctx,
        _cmd_enqueue,
        document=document,
        segment=segment,
        coders=coder or [],
    )


@app.command(
    name="code",
    rich_help_panel=PANEL_S1_PIPELINE,
    help="code all queued segments for all coders",
)
def _cli_code(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    workers: Annotated[
        str,
        typer.Option(
            "--workers",
            help='integer or "auto" (default: auto — 8 for Claude, 4 for Gemini/other)',
        ),
    ] = "auto",
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="delete failed/running runs for this coder before starting",
        ),
    ] = False,
    recode: Annotated[
        bool,
        typer.Option(
            "--recode",
            help=(
                "delete ALL existing runs for this coder (including 'done') "
                "and re-code every segment from scratch"
            ),
        ),
    ] = False,
    mock_embeddings: Annotated[
        bool,
        typer.Option(
            "--mock-embeddings",
            help="use deterministic mock embeddings (testing / no-network)",
        ),
    ] = False,
    trace: Annotated[
        bool,
        typer.Option(
            "--trace",
            help=(
                "print the system/user prompts, first-pass codes, critique, "
                "and refined codes for each segment"
            ),
        ),
    ] = False,
) -> None:
    _run(
        ctx,
        _cmd_code,
        limit=limit,
        workers=workers,
        retry_failed=retry_failed,
        recode=recode,
        mock_embeddings=mock_embeddings,
        trace=trace,
    )


@app.command(
    name="update-codebook",
    rich_help_panel=PANEL_S1_PIPELINE,
    help="aggregate codes for ready segments, then review them into the codebook",
)
def _cli_update_codebook(
    ctx: typer.Context,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    retry_failed: Annotated[
        bool,
        typer.Option(
            "--retry-failed",
            help="delete failed/pending aggregation rows before starting",
        ),
    ] = False,
    mock_embeddings: Annotated[
        bool,
        typer.Option(
            "--mock-embeddings",
            help="use deterministic mock embeddings (testing / no-network)",
        ),
    ] = False,
    skip_aggregate: Annotated[
        bool,
        typer.Option(
            "--skip-aggregate",
            help="skip the aggregate step; use already-aggregated rows as-is",
        ),
    ] = False,
    skip_review: Annotated[
        bool,
        typer.Option(
            "--skip-review",
            help="skip the review step; use already-reviewed rows as-is",
        ),
    ] = False,
) -> None:
    _run(
        ctx,
        _cmd_update_codebook,
        limit=limit,
        retry_failed=retry_failed,
        mock_embeddings=mock_embeddings,
        skip_aggregate=skip_aggregate,
        skip_review=skip_review,
    )


@app.command(
    name="batch",
    rich_help_panel=PANEL_S1_PIPELINE,
    help=(
        "iterate code+aggregate+review+finalize over batches of randomly "
        "selected uncoded documents. Resumable: on restart any in-progress "
        "queue entries are finished first as the current batch. Per-segment "
        "failures are recorded and the run continues."
    ),
)
def _cli_batch(
    ctx: typer.Context,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="documents per batch (default 100)",
        ),
    ] = 100,
    workers: Annotated[
        str,
        typer.Option(
            "--workers",
            help='integer or "auto" (default: auto)',
        ),
    ] = "auto",
    max_batches: Annotated[
        int | None,
        typer.Option(
            "--max-batches",
            help="stop after this many batches (default: run until no docs left)",
        ),
    ] = None,
    mock_embeddings: Annotated[
        bool,
        typer.Option(
            "--mock-embeddings",
            help="use deterministic mock embeddings (testing / no-network)",
        ),
    ] = False,
) -> None:
    _run(
        ctx,
        _cmd_batch,
        batch_size=batch_size,
        workers=workers,
        max_batches=max_batches,
        mock_embeddings=mock_embeddings,
    )


# Stage 1 status & inspection ------------------------------------------------


@app.command(
    name="status",
    rich_help_panel=PANEL_S1_STATUS,
    help="print pipeline counts",
)
def _cli_status(ctx: typer.Context) -> None:
    _run(ctx, _cmd_status)


@app.command(
    name="list-codebooks",
    rich_help_panel=PANEL_S1_STATUS,
    help="list all codebook versions",
)
def _cli_list_codebooks(ctx: typer.Context) -> None:
    _run(ctx, _cmd_list_codebooks)


@app.command(
    name="show-codebook",
    rich_help_panel=PANEL_S1_STATUS,
    help="print a codebook version in a human-readable format",
)
def _cli_show_codebook(
    ctx: typer.Context,
    version: Annotated[
        int | None,
        typer.Option("--version", help="codebook version (default: latest)"),
    ] = None,
) -> None:
    _run(ctx, _cmd_show_codebook, version=version)


@app.command(
    name="export-codebook",
    rich_help_panel=PANEL_S1_STATUS,
    help="write codebook snapshot",
)
def _cli_export_codebook(
    ctx: typer.Context,
    version: Annotated[int | None, typer.Option("--version")] = None,
    output: Annotated[str, typer.Option("-o", "--output")] = "-",
) -> None:
    _run(ctx, _cmd_export_codebook, version=version, output=output)


# Stage 2 — themes -----------------------------------------------------------


@app.command(
    name="create-theme-job",
    rich_help_panel=PANEL_S2_THEMES,
    help=(
        "create a theme-coding job (codebook version + researcher prompt) "
        "without running it — use 'create-themes' to run pending jobs"
    ),
)
def _cli_create_theme_job(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            help=(
                "job instructions appended to the system prompt — "
                "research question, persona, framing"
            ),
        ),
    ] = None,
    prompt_file: Annotated[
        str | None,
        typer.Option(
            "--prompt-file",
            help="read job instructions from this file instead of --prompt",
        ),
    ] = None,
    codebook_version: Annotated[
        int | None,
        typer.Option(
            "--codebook-version",
            help="codebook version to theme-code (default: latest)",
        ),
    ] = None,
) -> None:
    _run(
        ctx,
        _cmd_create_theme_job,
        prompt=prompt,
        prompt_file=prompt_file,
        codebook_version=codebook_version,
    )


@app.command(
    name="create-themes",
    rich_help_panel=PANEL_S2_THEMES,
    help=(
        "run every theme-coding job that has not been run yet (no "
        "associated themes); jobs that produce no themes record a "
        "sentinel so they aren't re-run"
    ),
)
def _cli_create_themes(ctx: typer.Context) -> None:
    _run(ctx, _cmd_create_themes)


@app.command(
    name="list-themes",
    rich_help_panel=PANEL_S2_THEMES,
    help=(
        "list themes — by default the 'current' set "
        "(not deleted, not folded into a derived theme)"
    ),
)
def _cli_list_themes(
    ctx: typer.Context,
    job: Annotated[
        int | None,
        typer.Option(
            "--job",
            help="instead show every theme attributed to this theme-coding job",
        ),
    ] = None,
) -> None:
    _run(ctx, _cmd_list_themes, job=job)


@app.command(
    name="show-theme",
    rich_help_panel=PANEL_S2_THEMES,
    help="print one theme with its codes and supporting quotes",
)
def _cli_show_theme(
    ctx: typer.Context,
    theme_id: Annotated[int, typer.Argument(help="theme_id to show")],
) -> None:
    _run(ctx, _cmd_show_theme, theme_id=theme_id)


# Research-context commands are registered via the helper module so its
# specific options stay collocated with its handlers.
research_context_cli.register_typer(app, _run, panel=PANEL_SETUP)


# Debugging ------------------------------------------------------------------


@app.command(
    name="test-segment",
    rich_help_panel=PANEL_DEBUG,
    help="segment a document and print segments (no DB write)",
)
def _cli_segment(
    ctx: typer.Context,
    file: Annotated[str, typer.Argument(help="path to .md/.txt/.pdf")],
    method: Annotated[
        str,
        typer.Option(
            "--method",
            click_type=click.Choice(["paragraph", "sentence", "fixed", "llm"]),
        ),
    ] = "llm",
    min_words: Annotated[
        int,
        typer.Option(
            "--min-words",
            help=(
                "minimum words per segment (for paragraph/sentence: drop; "
                "for llm: merge)"
            ),
        ),
    ] = 20,
    max_words: Annotated[int, typer.Option("--max-words")] = 500,
    model: Annotated[
        str | None,
        typer.Option(
            "--model",
            help=(
                "litellm model id for --method llm "
                "(defaults to LLM_MODEL_SEGMENTER env or gemini/gemini-2.5-flash-lite)"
            ),
        ),
    ] = None,
) -> None:
    _run(
        ctx,
        _cmd_segment,
        file=file,
        method=method,
        min_words=min_words,
        max_words=max_words,
        model=model,
    )


@app.command(
    name="test-code",
    rich_help_panel=PANEL_DEBUG,
    help="run coding for one segment/coder, print prompts and results, no DB write",
)
def _cli_test_code(
    ctx: typer.Context,
    segment_id: Annotated[int, typer.Argument(help="segment_id to code")],
    coder_id: Annotated[str, typer.Argument(help="coder_id to use")],
    mock_embeddings: Annotated[
        bool,
        typer.Option(
            "--mock-embeddings",
            help="use deterministic mock embeddings (testing / no-network)",
        ),
    ] = False,
) -> None:
    _run(
        ctx,
        _cmd_test_code,
        segment_id=segment_id,
        coder_id=coder_id,
        mock_embeddings=mock_embeddings,
    )


@app.command(
    name="test-aggregate",
    rich_help_panel=PANEL_DEBUG,
    help="run aggregator for one segment, print all steps, no DB write",
)
def _cli_test_aggregate(
    ctx: typer.Context,
    segment_id: Annotated[int, typer.Argument(help="segment_id to aggregate")],
) -> None:
    _run(
        ctx,
        _cmd_test_aggregate,
        segment_id=segment_id,
    )


@app.command(
    name="test-review",
    rich_help_panel=PANEL_DEBUG,
    help="run reviewer for one aggregator code, print all steps, no DB write",
)
def _cli_test_review(
    ctx: typer.Context,
    code_id: Annotated[int, typer.Argument(help="aggregator code_id to review")],
) -> None:
    _run(
        ctx,
        _cmd_test_review,
        code_id=code_id,
    )


@app.command(
    name="test-theme",
    rich_help_panel=PANEL_DEBUG,
    help=(
        "dry-run the theme coder against a codebook version + prompt; "
        "prints the proposed themes without writing anything to the DB"
    ),
)
def _cli_test_theme(
    ctx: typer.Context,
    prompt: Annotated[
        str | None,
        typer.Option(
            "--prompt",
            help=(
                "researcher's framing (research question, persona, "
                "extra instructions) — same shape as `theme-code`"
            ),
        ),
    ] = None,
    prompt_file: Annotated[
        str | None,
        typer.Option(
            "--prompt-file",
            help="read the framing from this file instead of --prompt",
        ),
    ] = None,
    codebook_version: Annotated[
        int | None,
        typer.Option(
            "--codebook-version",
            help="codebook version to use (default: latest)",
        ),
    ] = None,
) -> None:
    _run(
        ctx,
        _cmd_test_theme,
        prompt=prompt,
        prompt_file=prompt_file,
        codebook_version=codebook_version,
    )


# Entry point ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Parse argv and run the Typer app, returning an int exit code.

    Wraps the Typer/Click invocation with `standalone_mode=False` so we can
    map Click's exceptions to the same exit codes the previous argparse
    entry point used (and so tests can call `main([...])` directly).
    """
    debug_env = "--debug" in (argv or sys.argv[1:])
    try:
        rv = app(args=argv, standalone_mode=False)
        return int(rv) if isinstance(rv, int) else 0
    except click.exceptions.UsageError as e:
        e.show()
        return int(e.exit_code) if e.exit_code is not None else 2
    except click.exceptions.Abort:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except click.exceptions.ClickException as e:
        e.show()
        return int(e.exit_code) if e.exit_code is not None else 1
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FileNotFoundError as e:
        print(f"error: file not found: {e.filename or e}", file=sys.stderr)
        if debug_env:
            import traceback

            traceback.print_exception(e, file=sys.stderr)
        return 1
    except PermissionError as e:
        print(f"error: permission denied: {e.filename or e}", file=sys.stderr)
        return 1
    except IsADirectoryError as e:
        print(
            f"error: expected a file, got a directory: {e.filename or e}",
            file=sys.stderr,
        )
        return 1
    except json.JSONDecodeError as e:
        print(
            f"error: invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
            file=sys.stderr,
        )
        return 1
    except sqlite3.DatabaseError as e:
        print(f"error: database error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        if debug_env:
            import traceback

            print(
                f"error: unexpected error: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            traceback.print_exception(e, file=sys.stderr)
        else:
            print(
                f"error: unexpected error: {type(e).__name__}: {e} "
                "(run with --debug for a full traceback)",
                file=sys.stderr,
            )
        return 1


if __name__ == "__main__":
    sys.exit(main())
