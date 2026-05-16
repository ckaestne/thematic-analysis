"""Backward-compatibility shim.

The previous `ta`, `ta-stage1`, and `ta-stage2` entry points have been
merged into a single `ta` CLI implemented in
:mod:`thematic_analysis_inc.cli`. This module re-exports ``main`` so
callers that imported ``ta_cli.main`` keep working.
"""

from __future__ import annotations

from thematic_analysis_inc.cli import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
