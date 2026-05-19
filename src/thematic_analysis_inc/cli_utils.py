"""Shared helpers for the `ta` CLI entry point.

Provides:
    - `setup_logging`: configure the root logger (`-v` / `--debug`).
    - `require_db`: ensure a SQLite database file exists before running
      commands that read from it.
"""

from __future__ import annotations

import logging
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
    for name in (
        "sqlalchemy",
        "sqlalchemy.engine",
        "sqlalchemy.pool",
        "sqlalchemy.orm",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


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
