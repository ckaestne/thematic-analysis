"""Research context CRUD."""

from __future__ import annotations

import json
import sqlite3

from thematic_analysis.research_context import ResearchContext

from thematic_analysis_inc.db.connection import now


_LEGACY_FIELD_LABELS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("aim", "Aim"),
    ("research_questions", "Research questions"),
    ("theoretical_framework", "Theoretical framework"),
    ("paradigm", "Paradigm"),
    ("domain", "Domain"),
    ("background", "Background"),
    ("keywords", "Keywords"),
]


def _to_json(ctx: ResearchContext) -> str:
    return json.dumps(
        {
            "description": ctx.description,
            "tailored_prompts": dict(ctx.tailored_prompts),
        },
        indent=2,
    )


def _legacy_to_description(data: dict) -> str:
    parts: list[str] = []
    for key, label in _LEGACY_FIELD_LABELS:
        val = data.get(key)
        if not val:
            continue
        if isinstance(val, list):
            cleaned = [str(v).strip() for v in val if str(v).strip()]
            if not cleaned:
                continue
            if key == "research_questions":
                rendered = "\n".join(
                    f"{i + 1}. {v}" for i, v in enumerate(cleaned)
                )
            else:
                rendered = ", ".join(cleaned)
        else:
            rendered = str(val).strip()
            if not rendered:
                continue
        parts.append(f"**{label}:** {rendered}")
    return "\n\n".join(parts)


def _from_json(raw: str) -> ResearchContext:
    data = json.loads(raw)
    if "description" in data or "tailored_prompts" in data:
        prompts = data.get("tailored_prompts") or {}
        if not isinstance(prompts, dict):
            prompts = {}
        return ResearchContext(
            description=str(data.get("description", "")),
            tailored_prompts={
                str(k): str(v) for k, v in prompts.items() if v
            },
        )
    return ResearchContext(description=_legacy_to_description(data))


def set_research_context(
    conn: sqlite3.Connection, context: ResearchContext
) -> None:
    conn.execute(
        "INSERT INTO research_context (id, context_json, updated_at) "
        "VALUES (1, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  context_json = excluded.context_json, "
        "  updated_at = excluded.updated_at",
        (_to_json(context), now()),
    )


def get_research_context(conn: sqlite3.Connection) -> ResearchContext | None:
    row = conn.execute(
        "SELECT context_json FROM research_context WHERE id = 1"
    ).fetchone()
    if row is None:
        return None
    return _from_json(row["context_json"])


def clear_research_context(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("DELETE FROM research_context WHERE id = 1")
    return cur.rowcount > 0
