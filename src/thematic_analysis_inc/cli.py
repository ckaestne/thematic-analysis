"""Single-entry CLI for the incremental Stage 1 pipeline.

Subcommands:
    init, add-coder, rm-coder, list-coders,
    enqueue, add-document, code, status, export-codebook
The remaining worker subcommands (aggregate, review, run) are added in
later steps.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path


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

from thematic_analysis_inc import research_context_cli, store, workers  # noqa: E402


# init ------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    conn = store.init_db(args.db)
    latest = store.latest_codebook_version(conn)
    assert latest is not None
    print(f"initialized {args.db} (codebook v{latest.version}, 0 codes)")
    return 0


# coders ----------------------------------------------------------------------


def _cmd_add_coder(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    inserted = store.add_coder(conn, args.coder_id, args.identity)
    if inserted:
        print(f"added coder '{args.coder_id}' (identity: {args.identity!r})")
    else:
        print(f"coder '{args.coder_id}' already exists; not modified")
    return 0


def _cmd_rm_coder(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        removed, runs_deleted = store.remove_coder(
            conn, args.coder_id, force=args.force
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        print("hint: pass --force to also drop their coder_runs", file=sys.stderr)
        return 1
    if removed:
        suffix = f" (also dropped {runs_deleted} run(s))" if runs_deleted else ""
        print(f"removed coder '{args.coder_id}'{suffix}")
        return 0
    print(f"no coder with id '{args.coder_id}'", file=sys.stderr)
    return 1


def _cmd_list_coders(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    coders = store.list_coders(conn)
    if not coders:
        print("(no coders)")
        return 0
    for c in coders:
        print(f"{c.coder_id}\t{c.identity}")
    return 0


# segments --------------------------------------------------------------------


def _load_segments_file(path: str) -> list[tuple[str, str]]:
    p = Path(path)
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    items: list = []
    if raw.startswith("["):
        items = json.loads(raw)
    else:
        for line in raw.splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
    out: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, dict):
            out.append((str(it["segment_id"]), str(it["text"])))
        elif isinstance(it, list | tuple) and len(it) == 2:
            out.append((str(it[0]), str(it[1])))
        else:
            raise ValueError(f"unrecognized segment entry: {it!r}")
    return out


def _cmd_enqueue(args: argparse.Namespace) -> int:
    segments = _load_segments_file(args.segments)
    if not segments:
        print("no segments found in input file", file=sys.stderr)
        return 1
    conn = store.connect(args.db)
    result = store.enqueue_segments(conn, segments, batch=args.batch)
    print(
        f"enqueued: inserted={result.inserted_segments} "
        f"skipped={result.skipped_segments}"
    )
    return 0


def _cmd_add_document(args: argparse.Namespace) -> int:
    from thematic_analysis.loaders import load_text_file  # lazy

    paths = [Path(p) for p in args.files]
    missing = [p for p in paths if not p.exists()]
    if missing:
        for p in missing:
            print(f"file not found: {p}", file=sys.stderr)
        return 1

    conn = store.connect(args.db)
    total_files = total_inserted = total_skipped = 0
    for path in paths:
        doc = load_text_file(path)
        rows: list[tuple[str, str, str | None]]
        if args.segmentation == "llm":
            from thematic_analysis_inc.segmenter_llm import segment_by_llm

            titled = segment_by_llm(
                doc.text,
                doc_id=path.stem,
                model=args.model,
                min_words=args.min_words,
            )
            rows = [(s.segment_id, s.text, s.title) for s in titled]
        else:
            segments = doc.segment(
                method=args.segmentation,
                min_words=args.min_words,
                max_words=args.max_words,
            )
            rows = [(s.segment_id, s.text, None) for s in segments]

        if not rows:
            print(
                f"[add-document] {path.name}: 0 segments (skipped)",
                file=sys.stderr,
            )
            continue

        document_id = store.add_document(
            conn, filename=path.name, content=path.read_bytes()
        )
        enqueue_rows = [
            (sid, txt, title, document_id, i)
            for i, (sid, txt, title) in enumerate(rows)
        ]
        result = store.enqueue_segments(conn, enqueue_rows, batch=args.batch)
        total_files += 1
        total_inserted += result.inserted_segments
        total_skipped += result.skipped_segments
        print(
            f"[add-document] {path.name}: doc_id={document_id} "
            f"segments={len(rows)} "
            f"inserted={result.inserted_segments} "
            f"skipped={result.skipped_segments}"
        )
    print(
        f"done: {total_files} file(s), inserted={total_inserted} "
        f"skipped={total_skipped}"
    )
    return 0


# code ------------------------------------------------------------------------


def _format_codes(assignment) -> str:
    if assignment is None:
        return "  (none)"
    codes = list(getattr(assignment, "codes", []) or [])
    rationales = list(getattr(assignment, "rationales", []) or [])
    is_new = list(getattr(assignment, "is_new_code", []) or [])
    if not codes:
        return "  (no codes — out of scope / nothing to code)"
    lines: list[str] = []
    for i, code in enumerate(codes):
        rat = rationales[i] if i < len(rationales) else ""
        new = " [NEW]" if i < len(is_new) and is_new[i] else ""
        lines.append(f"  {i + 1}. {code}{new}")
        if rat:
            lines.append(f"     → {rat}")
    return "\n".join(lines)


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
    print(sub)
    print("First-pass codes (coder):")
    print(_format_codes(trace.get("first")))
    print(sub)
    critique = trace.get("critique")
    if critique is None:
        print("Critique: (skipped — first pass produced no codes)")
    else:
        print("Critique (challenger):")
        for line in critique.strip().splitlines() or [""]:
            print(f"  {line}")
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


def _cmd_code(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    coder = store.get_coder(conn, args.coder_id)
    if coder is None:
        print(f"unknown coder_id: {args.coder_id}", file=sys.stderr)
        return 1

    if args.retry_failed:
        n = store.reset_unfinished_coder_runs(conn, args.coder_id)
        if n:
            print(f"[code] cleared {n} failed/running run(s) for retry")

    n_workers = _resolve_workers(args.workers)

    todo = len(store.segments_to_code(conn, args.coder_id))
    print(
        f"[code] coder={args.coder_id} todo={todo} workers={n_workers}"
        + (f" limit={args.limit}" if args.limit else "")
    )
    if todo == 0:
        return 0

    def on_event(res: dict, c: dict) -> None:
        n = c["done"] + c["failed"]
        if res["ok"]:
            if args.verbose and res.get("trace") is not None:
                _print_trace(res["segment_id"], res["trace"])
            print(
                f"[code] {res['segment_id']} coder={res['coder_id']} "
                f"codes={res['n_codes']} v={res['version']} "
                f"({n}/{todo} ok={c['done']} failed={c['failed']} "
                f"{res['elapsed']:.1f}s)"
            )
        else:
            print(
                f"[code] {res['segment_id']} coder={res['coder_id']} "
                f"FAILED v={res['version']}: {res['error']}",
                file=sys.stderr,
            )

    async def _run() -> dict:
        # Each segment fires up to 3 concurrent LLM calls via the refining
        # coder; size the thread pool so workers aren't blocked queueing on it.
        from concurrent.futures import ThreadPoolExecutor
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=max(8, n_workers * 3)))
        return await workers.drain_code_async(
            conn,
            args.coder_id,
            workers=n_workers,
            limit=args.limit,
            use_mock_embeddings=args.mock_embeddings,
            on_event=on_event,
        )

    counters = asyncio.run(_run())
    print(f"[code] done: {counters['done']} ok, {counters['failed']} failed")
    return 0


# aggregate -------------------------------------------------------------------


def _cmd_aggregate(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)

    if args.retry_failed:
        n = store.reset_unfinished_aggregations(conn)
        if n:
            print(f"[aggregate] cleared {n} failed/pending aggregation(s)")

    print(
        "[aggregate] starting"
        + (f" limit={args.limit}" if args.limit else "")
    )

    def on_event(res: dict, c: dict) -> None:
        n = c["done"] + c["failed"]
        if res["ok"]:
            print(
                f"[aggregate] {res['segment_id']} in={res['n_in']} "
                f"merged={res['n_merged']} retained={res['n_retained']} "
                f"({n} ok={c['done']} failed={c['failed']} "
                f"{res['elapsed']:.1f}s)"
            )
        else:
            print(
                f"[aggregate] {res['segment_id']} FAILED: {res['error']}",
                file=sys.stderr,
            )

    counters = workers.drain_aggregate(
        conn,
        limit=args.limit,
        use_mock_embeddings=args.mock_embeddings,
        on_event=on_event,
    )
    print(
        f"[aggregate] done: {counters['done']} ok, "
        f"{counters['failed']} failed"
    )
    return 0


# review ----------------------------------------------------------------------


def _cmd_review(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)

    print(
        "[review] starting"
        + (f" limit={args.limit}" if args.limit else "")
    )

    def on_event(res: dict, c: dict) -> None:
        n = c["done"] + c["failed"]
        v_str = f"v→{res['new_version']}" if res.get("new_version") else "skip"
        print(
            f"[review] {res['segment_id']} code={res['code']!r} "
            f"decision={res['decision']} {v_str} "
            f"({n} ok={c['done']} failed={c['failed']} "
            f"{res['elapsed']:.1f}s)"
        )

    counters = workers.drain_review(
        conn,
        limit=args.limit,
        use_mock_embeddings=args.mock_embeddings,
        on_event=on_event,
    )
    print(
        f"[review] done: {counters['done']} ok, "
        f"{counters['failed']} failed"
    )
    return 0


# status / export -------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    print(store.status_counts(conn).format())
    return 0


def _cmd_export_codebook(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    if args.version is None:
        cv = store.latest_codebook_version(conn)
    else:
        cv = store.get_codebook_version(conn, args.version)
    if cv is None:
        print("no codebook version found", file=sys.stderr)
        return 1
    if args.output == "-" or args.output is None:
        sys.stdout.write(cv.snapshot_json)
        if not cv.snapshot_json.endswith("\n"):
            sys.stdout.write("\n")
    else:
        Path(args.output).write_text(cv.snapshot_json, encoding="utf-8")
        codes = len(json.loads(cv.snapshot_json).get("codes", []))
        print(f"wrote codebook v{cv.version} ({codes} codes) to {args.output}")
    return 0


def _cmd_list_codebooks(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    versions = store.list_codebook_versions(conn)
    if not versions:
        print("(no codebook versions)")
        return 0
    print(f"{'version':>7}  {'parent':>6}  {'codes':>5}  {'created_by':<20}  created_at")
    for cv in versions:
        n_codes = len(json.loads(cv.snapshot_json).get("codes", []))
        parent = "-" if cv.parent_version is None else str(cv.parent_version)
        print(
            f"{cv.version:>7}  {parent:>6}  {n_codes:>5}  "
            f"{cv.created_by:<20}  {cv.created_at}"
        )
    return 0


def _cmd_show_codebook(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    if args.version is None:
        cv = store.latest_codebook_version(conn)
    else:
        cv = store.get_codebook_version(conn, args.version)
    if cv is None:
        msg = (
            f"no codebook version {args.version}"
            if args.version is not None
            else "no codebook versions found"
        )
        print(msg, file=sys.stderr)
        return 1
    data = json.loads(cv.snapshot_json)
    codes = data.get("codes", [])
    parent = "-" if cv.parent_version is None else str(cv.parent_version)
    print(
        f"Codebook v{cv.version} (parent={parent}, "
        f"created_by={cv.created_by}, created_at={cv.created_at})"
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


# parser ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ta-stage1", description=__doc__)
    p.add_argument("--db", required=True, help="path to the SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the schema and codebook v1")
    p_init.set_defaults(func=_cmd_init)

    p_ac = sub.add_parser("add-coder", help="register a coder")
    p_ac.add_argument("coder_id")
    p_ac.add_argument(
        "identity",
        help="free-text identity/persona shown to the coder agent",
    )
    p_ac.set_defaults(func=_cmd_add_coder)

    p_rc = sub.add_parser(
        "rm-coder",
        help="remove a coder (use --force to also drop their coder_runs)",
    )
    p_rc.add_argument("coder_id")
    p_rc.add_argument(
        "--force",
        action="store_true",
        help="also delete this coder's coder_runs and coder_codes",
    )
    p_rc.set_defaults(func=_cmd_rm_coder)

    p_lc = sub.add_parser("list-coders", help="list registered coders")
    p_lc.set_defaults(func=_cmd_list_coders)

    p_enq = sub.add_parser("enqueue", help="add segments from a JSON/JSONL file")
    p_enq.add_argument("--segments", required=True)
    p_enq.add_argument("--batch", type=int, default=None)
    p_enq.set_defaults(func=_cmd_enqueue)

    p_doc = sub.add_parser(
        "add-document", help="load .md/.txt files, segment, and add"
    )
    p_doc.add_argument("files", nargs="+")
    p_doc.add_argument(
        "--segmentation",
        choices=("llm", "paragraph", "sentence", "fixed"),
        default="llm",
    )
    p_doc.add_argument(
        "--min-words",
        type=int,
        default=50,
        help="minimum words per segment (drop for paragraph/sentence; merge for llm)",
    )
    p_doc.add_argument("--max-words", type=int, default=500)
    p_doc.add_argument(
        "--model",
        default="gemini/gemini-2.5-flash-lite",
        help="litellm model id for --segmentation llm",
    )
    p_doc.add_argument("--batch", type=int, default=None)
    p_doc.set_defaults(func=_cmd_add_document)

    p_code = sub.add_parser("code", help="code all unprocessed segments for a coder")
    p_code.add_argument("coder_id")
    p_code.add_argument("--limit", type=int, default=None)
    p_code.add_argument(
        "--workers",
        default="auto",
        help='integer or "auto" (default: auto — 8 for Claude, 4 for Gemini/other)',
    )
    p_code.add_argument(
        "--retry-failed",
        action="store_true",
        help="delete failed/running runs for this coder before starting",
    )
    p_code.add_argument(
        "--mock-embeddings",
        action="store_true",
        help="use deterministic mock embeddings (testing / no-network)",
    )
    p_code.add_argument(
        "--verbose",
        action="store_true",
        help="print the first-pass codes, critique, and refined codes for each segment",
    )
    p_code.set_defaults(func=_cmd_code)

    p_agg = sub.add_parser(
        "aggregate", help="aggregate codes for ready segments"
    )
    p_agg.add_argument("--limit", type=int, default=None)
    p_agg.add_argument(
        "--retry-failed",
        action="store_true",
        help="delete failed/pending aggregation rows before starting",
    )
    p_agg.add_argument(
        "--mock-embeddings",
        action="store_true",
        help="use deterministic mock embeddings (testing / no-network)",
    )
    p_agg.set_defaults(func=_cmd_aggregate)

    p_rev = sub.add_parser(
        "review", help="review aggregated codes and update the codebook"
    )
    p_rev.add_argument("--limit", type=int, default=None)
    p_rev.add_argument(
        "--mock-embeddings",
        action="store_true",
        help="use deterministic mock embeddings (testing / no-network)",
    )
    p_rev.set_defaults(func=_cmd_review)

    p_st = sub.add_parser("status", help="print pipeline counts")
    p_st.set_defaults(func=_cmd_status)

    p_lcb = sub.add_parser(
        "list-codebooks", help="list all codebook versions"
    )
    p_lcb.set_defaults(func=_cmd_list_codebooks)

    p_scb = sub.add_parser(
        "show-codebook",
        help="print a codebook version in a human-readable format",
    )
    p_scb.add_argument(
        "--version",
        type=int,
        default=None,
        help="codebook version (default: latest)",
    )
    p_scb.set_defaults(func=_cmd_show_codebook)

    p_ex = sub.add_parser("export-codebook", help="write codebook snapshot")
    p_ex.add_argument("--version", type=int, default=None)
    p_ex.add_argument("-o", "--output", default="-")
    p_ex.set_defaults(func=_cmd_export_codebook)

    research_context_cli.register(sub)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
