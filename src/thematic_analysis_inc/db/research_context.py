"""Versioned research-context CRUD.

The research context is no longer a singleton: each ``set_research_context``
inserts a new row keyed by an auto-incrementing
``research_context_version``. Other tables that capture the context active
at a given moment (``codes``, ``coding_queue``, ``theme_coder_runs``,
``theme_aggregations``) carry an FK back to this version.

The on-disk representation is a set of explicit columns rather than a JSON
blob: one ``description`` column and one column per agent role
(``coder_prompt``, ``coding_critic_prompt``, ``reviewer_prompt``,
``theme_coder_prompt``, ``theme_aggregator_prompt``) that maps to the role
names exposed by :mod:`thematic_analysis.research_context.AGENT_ROLES`.
"""

from __future__ import annotations

import sqlite3

from thematic_analysis.research_context import AGENT_ROLES, ResearchContext

from thematic_analysis_inc.db.connection import now


# Re-exported so callers in ``db/`` don't need to import from the
# top-level ``thematic_analysis`` package directly.
RC_AGENT_ROLES: tuple[str, ...] = AGENT_ROLES


# Per-role -> DB column. Kept in sync with AGENT_ROLES.
_ROLE_TO_COLUMN: dict[str, str] = {
    "coder": "coder_prompt",
    "coding_critic": "coding_critic_prompt",
    "reviewer": "reviewer_prompt",
    "theme_coder": "theme_coder_prompt",
    "theme_aggregator": "theme_aggregator_prompt",
}


def _row_to_context(row: sqlite3.Row) -> ResearchContext:
    tailored: dict[str, str] = {}
    for role, col in _ROLE_TO_COLUMN.items():
        val = row[col]
        if val:
            tailored[role] = str(val)
    return ResearchContext(
        description=row["description"] or "",
        tailored_prompts=tailored,
    )


def set_research_context(
    conn: sqlite3.Connection, context: ResearchContext
) -> int:
    """Insert a new research-context version. Returns the new version id.

    History is preserved: previous versions stay in the table so codes /
    queue rows / theme runs that reference them remain valid.
    """
    cur = conn.execute(
        "INSERT INTO research_context "
        "(description, coder_prompt, coding_critic_prompt, reviewer_prompt, "
        " theme_coder_prompt, theme_aggregator_prompt, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            context.description or "",
            context.tailored_prompts.get("coder") or None,
            context.tailored_prompts.get("coding_critic") or None,
            context.tailored_prompts.get("reviewer") or None,
            context.tailored_prompts.get("theme_coder") or None,
            context.tailored_prompts.get("theme_aggregator") or None,
            now(),
        ),
    )
    return int(cur.lastrowid)


def get_research_context(
    conn: sqlite3.Connection, version: int | None = None
) -> tuple[int, ResearchContext] | None:
    """Return ``(version, ResearchContext)`` for ``version`` (or latest).

    Returns ``None`` if the table is empty (or the requested version is
    missing).
    """
    if version is None:
        row = conn.execute(
            "SELECT research_context_version, description, coder_prompt, "
            "  coding_critic_prompt, reviewer_prompt, theme_coder_prompt, "
            "  theme_aggregator_prompt, created_at "
            "FROM research_context "
            "ORDER BY research_context_version DESC LIMIT 1"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT research_context_version, description, coder_prompt, "
            "  coding_critic_prompt, reviewer_prompt, theme_coder_prompt, "
            "  theme_aggregator_prompt, created_at "
            "FROM research_context WHERE research_context_version = ?",
            (version,),
        ).fetchone()
    if row is None:
        return None
    return int(row["research_context_version"]), _row_to_context(row)


def latest_research_context_version(
    conn: sqlite3.Connection,
) -> int | None:
    row = conn.execute(
        "SELECT MAX(research_context_version) AS v FROM research_context"
    ).fetchone()
    if row is None or row["v"] is None:
        return None
    return int(row["v"])


def list_research_context_versions(
    conn: sqlite3.Connection,
) -> list[dict]:
    rows = conn.execute(
        "SELECT research_context_version, description, created_at "
        "FROM research_context "
        "ORDER BY research_context_version ASC"
    ).fetchall()
    return [
        {
            "research_context_version": int(r["research_context_version"]),
            "description": r["description"] or "",
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def clear_research_context(conn: sqlite3.Connection) -> bool:
    """Wipe research-context history. Returns True if rows were removed.

    NB: fresh-DB policy — codes / queue rows / theme runs that reference a
    research_context row will still carry their (now-dangling) FK value.
    Callers should only use this on a DB with no dependent rows yet.
    """
    cur = conn.execute("DELETE FROM research_context")
    return cur.rowcount > 0
