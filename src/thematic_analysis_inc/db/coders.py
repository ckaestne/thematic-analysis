"""Coder CRUD, backed by SQLModel.

User-supplied coders auto-assign integer ids ≥ 1. The two reserved
system rows (``0`` aggregator, ``-1`` reviewer) are seeded by
:func:`db.connection._create_sqlmodel_tables`.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlmodel import select

from thematic_analysis_inc.db.connection import session
from thematic_analysis_inc.db.models import Coder


SYSTEM_AGGREGATOR_ID = 0
SYSTEM_REVIEWER_ID = -1


def add_coder(identity: str) -> Coder:
    """Insert a new real coder (id ≥ 1). Returns the persisted Coder."""
    with session() as s:
        row = s.exec(
            select(func.coalesce(func.max(Coder.coder_id), 0)).where(
                Coder.coder_id >= 1
            )
        ).one()
        next_id = int(row) + 1
        c = Coder(coder_id=next_id, identity=identity)
        s.add(c)
        s.commit()
        s.refresh(c)
        s.expunge(c)
        return c


def get_coder(coder_id: int) -> Coder | None:
    with session() as s:
        c = s.get(Coder, coder_id)
        if c is not None:
            s.expunge(c)
        return c


def list_coders() -> list[Coder]:
    """Real coders (id ≥ 1) ordered by id."""
    with session() as s:
        rows = list(
            s.exec(
                select(Coder)
                .where(Coder.coder_id >= 1)
                .order_by(Coder.coder_id)
            ).all()
        )
        for r in rows:
            s.expunge(r)
        return rows
