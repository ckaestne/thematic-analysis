"""Single-entry CLI for the incremental Stage 2 (theme development) pipeline.

Subcommands:
    init, add-theme-coder, rm-theme-coder, list-theme-coders,
    theme-code, theme-aggregate, status, export-themes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from pathlib import Path


# Quiet down noisy ML deps before anything imports them.
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

from thematic_analysis_inc import store, workers  # noqa: E402


# init ------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    conn = store.init_db(args.db)
    latest = store.latest_codebook_version(conn)
    assert latest is not None
    print(
        f"initialized {args.db} "
        f"(codebook v{latest.version}, stage-2 schema ready)"
    )
    return 0


# theme-coders ----------------------------------------------------------------


def _cmd_add_theme_coder(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    inserted = store.add_theme_coder(conn, args.theme_coder_id, args.identity)
    if inserted:
        print(
            f"added theme_coder '{args.theme_coder_id}' "
            f"(identity: {args.identity!r})"
        )
    else:
        print(
            f"theme_coder '{args.theme_coder_id}' already exists; not modified"
        )
    return 0


def _cmd_rm_theme_coder(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    try:
        removed, runs_deleted = store.remove_theme_coder(
            conn, args.theme_coder_id, force=args.force
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        print("hint: pass --force to also drop their theme_coder_runs", file=sys.stderr)
        return 1
    if removed:
        suffix = f" (also dropped {runs_deleted} run(s))" if runs_deleted else ""
        print(f"removed theme_coder '{args.theme_coder_id}'{suffix}")
        return 0
    print(f"no theme_coder with id '{args.theme_coder_id}'", file=sys.stderr)
    return 1


def _cmd_list_theme_coders(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    coders = store.list_theme_coders(conn)
    if not coders:
        print("(no theme coders)")
        return 0
    for c in coders:
        print(f"{c.theme_coder_id}\t{c.identity}")
    return 0


# theme-code ------------------------------------------------------------------


def _resolve_codebook_version(conn, version_arg: int | None) -> int | None:
    """Return the specified version, or the latest if None. Prints an error
    and returns None if no codebook exists."""
    if version_arg is not None:
        return version_arg
    cv = store.latest_codebook_version(conn)
    if cv is None:
        print("no codebook version found; run ta-stage1 init first", file=sys.stderr)
        return None
    return cv.version


def _cmd_theme_code(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)

    version = _resolve_codebook_version(conn, args.codebook_version)
    if version is None:
        return 1

    if args.retry_failed:
        for tc in store.list_theme_coders(conn):
            n = store.reset_unfinished_theme_coder_runs(
                conn, tc.theme_coder_id, version
            )
            if n:
                print(
                    f"[theme-code] cleared {n} failed/running run(s) "
                    f"for {tc.theme_coder_id}"
                )

    pending = store.theme_coders_to_run(conn, version)
    todo = len(pending)
    print(
        f"[theme-code] codebook=v{version} todo={todo} workers={args.workers}"
        + (f" limit={args.limit}" if args.limit else "")
    )
    if todo == 0:
        print("[theme-code] nothing to do")
        return 0

    def on_event(res: dict, c: dict) -> None:
        n = c["done"] + c["failed"]
        if res["ok"]:
            print(
                f"[theme-code] coder={res['theme_coder_id']} "
                f"themes={res['n_themes']} v={res['codebook_version']} "
                f"({n}/{todo} ok={c['done']} failed={c['failed']} "
                f"{res['elapsed']:.1f}s)"
            )
        else:
            print(
                f"[theme-code] coder={res['theme_coder_id']} FAILED "
                f"v={res['codebook_version']}: {res['error']}",
                file=sys.stderr,
            )

    counters = asyncio.run(
        workers.drain_theme_code_async(
            conn,
            version,
            workers=args.workers,
            limit=args.limit,
            use_mock_embeddings=args.mock_embeddings,
            on_event=on_event,
        )
    )
    print(
        f"[theme-code] done: {counters['done']} ok, {counters['failed']} failed"
    )
    return 0


# theme-aggregate -------------------------------------------------------------


def _cmd_theme_aggregate(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)

    version = _resolve_codebook_version(conn, args.codebook_version)
    if version is None:
        return 1

    if args.retry_failed:
        n = store.reset_unfinished_theme_aggregations(conn, version)
        if n:
            print(f"[theme-aggregate] cleared {n} failed/running aggregation(s)")

    print(f"[theme-aggregate] codebook=v{version}")

    res = workers.theme_aggregate_one(
        conn,
        version,
        use_mock_embeddings=args.mock_embeddings,
    )

    if res is None:
        # Check why: either not all coders done, or aggregation already exists.
        if not store.all_theme_coders_done(conn, version):
            print(
                "[theme-aggregate] not all theme coders have finished for "
                f"v{version}; run theme-code first",
                file=sys.stderr,
            )
            return 1
        existing = store.latest_theme_aggregation(conn, version)
        if existing is not None and existing["status"] == "done":
            print(
                f"[theme-aggregate] already done (aggregation id={existing['id']}); "
                "use --retry-failed to re-run"
            )
            return 0
        print(
            "[theme-aggregate] nothing to do (no theme coders registered?)",
            file=sys.stderr,
        )
        return 1

    if res["ok"]:
        print(
            f"[theme-aggregate] aggregation_id={res['aggregation_id']} "
            f"inputs={res['n_input_results']} themes={res['n_themes']} "
            f"{res['elapsed']:.1f}s"
        )
        return 0
    else:
        print(
            f"[theme-aggregate] FAILED: {res['error']}",
            file=sys.stderr,
        )
        return 1


# status / export -------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    version = _resolve_codebook_version(conn, args.codebook_version)
    if version is None:
        return 1
    print(store.stage2_status_counts(conn, version).format())
    return 0


def _cmd_export_themes(args: argparse.Namespace) -> int:
    conn = store.connect(args.db)
    version = _resolve_codebook_version(conn, args.codebook_version)
    if version is None:
        return 1

    agg = store.latest_theme_aggregation(conn, version)
    if agg is None or agg["status"] != "done" or not agg["result_json"]:
        print(
            f"no completed theme aggregation for codebook v{version}; "
            "run theme-aggregate first",
            file=sys.stderr,
        )
        return 1

    if args.output == "-" or args.output is None:
        sys.stdout.write(agg["result_json"])
        if not agg["result_json"].endswith("\n"):
            sys.stdout.write("\n")
    else:
        Path(args.output).write_text(agg["result_json"], encoding="utf-8")
        n_themes = len(json.loads(agg["result_json"]).get("themes", []))
        print(
            f"wrote {n_themes} theme(s) from codebook v{version} "
            f"to {args.output}"
        )
    return 0


# parser ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ta-stage2", description=__doc__)
    p.add_argument("--db", required=True, help="path to the SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser(
        "init", help="ensure schema is initialised (safe to re-run)"
    )
    p_init.set_defaults(func=_cmd_init)

    p_atc = sub.add_parser("add-theme-coder", help="register a theme coder")
    p_atc.add_argument("theme_coder_id")
    p_atc.add_argument(
        "identity", help="free-text identity/persona shown to the agent"
    )
    p_atc.set_defaults(func=_cmd_add_theme_coder)

    p_rtc = sub.add_parser(
        "rm-theme-coder",
        help="remove a theme coder (--force also drops their runs)",
    )
    p_rtc.add_argument("theme_coder_id")
    p_rtc.add_argument("--force", action="store_true")
    p_rtc.set_defaults(func=_cmd_rm_theme_coder)

    p_ltc = sub.add_parser("list-theme-coders", help="list registered theme coders")
    p_ltc.set_defaults(func=_cmd_list_theme_coders)

    p_tc = sub.add_parser(
        "theme-code",
        help="run all pending theme coders against a codebook version",
    )
    p_tc.add_argument(
        "--codebook-version",
        type=int,
        default=None,
        help="codebook version to use (default: latest)",
    )
    p_tc.add_argument("--limit", type=int, default=None)
    p_tc.add_argument("--workers", type=int, default=1)
    p_tc.add_argument(
        "--retry-failed",
        action="store_true",
        help="delete failed/running runs before starting",
    )
    p_tc.add_argument(
        "--mock-embeddings",
        action="store_true",
        help="use deterministic mock embeddings (testing / no-network)",
    )
    p_tc.set_defaults(func=_cmd_theme_code)

    p_ta = sub.add_parser(
        "theme-aggregate",
        help="aggregate theme results into a final theme set",
    )
    p_ta.add_argument(
        "--codebook-version",
        type=int,
        default=None,
        help="codebook version to aggregate (default: latest)",
    )
    p_ta.add_argument(
        "--retry-failed",
        action="store_true",
        help="delete failed/running aggregation before starting",
    )
    p_ta.add_argument(
        "--mock-embeddings",
        action="store_true",
        help="use deterministic mock embeddings (testing / no-network)",
    )
    p_ta.set_defaults(func=_cmd_theme_aggregate)

    p_st = sub.add_parser("status", help="print stage-2 pipeline counts")
    p_st.add_argument(
        "--codebook-version",
        type=int,
        default=None,
        help="codebook version to report on (default: latest)",
    )
    p_st.set_defaults(func=_cmd_status)

    p_ex = sub.add_parser("export-themes", help="write theme aggregation result")
    p_ex.add_argument(
        "--codebook-version",
        type=int,
        default=None,
        help="codebook version (default: latest)",
    )
    p_ex.add_argument("-o", "--output", default="-")
    p_ex.set_defaults(func=_cmd_export_themes)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
