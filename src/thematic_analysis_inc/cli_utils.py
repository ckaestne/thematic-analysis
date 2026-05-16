"""Shared helpers for the `ta` CLI entry point.

Provides:
    - `setup_logging`: configure the root logger (`-v` / `--debug`).
    - `run_cli`: parse argv and dispatch to the chosen subcommand with
      uniform handling of expected errors (file not found, JSON errors,
      sqlite errors, keyboard interrupt) and unexpected exceptions.
    - `require_db`: ensure a SQLite database file exists before running
      commands that read from it.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

log = logging.getLogger("ta")


def setup_logging(verbose: bool = False, debug: bool = False) -> None:
    """Configure the root logger.

    The default level is WARNING so subcommands that print structured
    progress to stdout stay readable; `-v` raises it to INFO and
    `--debug` to DEBUG (and unlocks tracebacks for unexpected errors).
    """
    level = logging.DEBUG if debug else (logging.INFO if verbose else logging.WARNING)
    root = logging.getLogger()
    # Don't stack handlers when main() is called multiple times (tests).
    for h in list(root.handlers):
        if getattr(h, "_ta_cli_handler", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler._ta_cli_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level)


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach `-v/--verbose` and `--debug` to a top-level parser."""
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose logging (INFO level)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="debug logging and full tracebacks on unexpected errors",
    )


def require_db(path: str | Path) -> int:
    """Verify a SQLite DB exists before opening it.

    Returns 0 on success, or 1 after printing an error to stderr.
    Skip this check for `init` (which is allowed to create the DB).
    """
    p = Path(path)
    if not p.exists():
        print(
            f"database not found: {p}\n"
            f"hint: run 'ta --db {p} init' to create it",
            file=sys.stderr,
        )
        return 1
    if p.is_dir():
        print(f"database path is a directory, not a file: {p}", file=sys.stderr)
        return 1
    return 0


def _error(msg: str, *, debug: bool = False, exc: BaseException | None = None) -> None:
    print(f"error: {msg}", file=sys.stderr)
    if debug and exc is not None:
        import traceback

        traceback.print_exception(exc, file=sys.stderr)


def run_cli(
    parser: argparse.ArgumentParser,
    argv: list[str] | None,
    *,
    requires_existing_db: set[str] | None = None,
) -> int:
    """Parse argv and dispatch to the chosen subcommand with error handling.

    `requires_existing_db` is the set of subcommand names that must run
    against an already-initialised DB; for those we verify the path before
    calling the handler so users get a clear message instead of an empty
    schema being silently created.
    """
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse already printed usage; preserve its exit code.
        return int(e.code) if isinstance(e.code, int) else 2

    setup_logging(
        verbose=getattr(args, "verbose", False),
        debug=getattr(args, "debug", False),
    )

    cmd = getattr(args, "cmd", None)
    needs_db = bool(requires_existing_db and cmd in requires_existing_db)
    db = getattr(args, "db", None)
    if (cmd == "init" or needs_db) and not db:
        print(
            f"--db is required for subcommand '{cmd}'",
            file=sys.stderr,
        )
        return 2
    if needs_db and db:
        rc = require_db(db)
        if rc:
            return rc

    func = getattr(args, "func", None)
    if func is None:
        parser.print_help(sys.stderr)
        return 2

    debug = getattr(args, "debug", False)
    try:
        return int(func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except FileNotFoundError as e:
        _error(f"file not found: {e.filename or e}", debug=debug, exc=e)
        return 1
    except PermissionError as e:
        _error(f"permission denied: {e.filename or e}", debug=debug, exc=e)
        return 1
    except IsADirectoryError as e:
        _error(f"expected a file, got a directory: {e.filename or e}")
        return 1
    except json.JSONDecodeError as e:
        _error(
            f"invalid JSON: {e.msg} (line {e.lineno}, column {e.colno})",
            debug=debug,
            exc=e,
        )
        return 1
    except sqlite3.DatabaseError as e:
        _error(f"database error: {e}", debug=debug, exc=e)
        return 1
    except ValueError as e:
        _error(str(e), debug=debug, exc=e)
        return 1
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else 2
    except Exception as e:  # noqa: BLE001
        if debug:
            _error(f"unexpected error: {type(e).__name__}: {e}", debug=True, exc=e)
        else:
            _error(
                f"unexpected error: {type(e).__name__}: {e} "
                "(run with --debug for a full traceback)",
            )
        return 1
