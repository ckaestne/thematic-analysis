"""Backward-compatibility shim.

The Stage 1 and Stage 2 CLIs are now a single `ta` command implemented in
:mod:`thematic_analysis_inc.cli`. This module re-exports ``main`` so older
callers that imported ``cli_stage2.main`` keep working.
"""

from __future__ import annotations

from thematic_analysis_inc.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
